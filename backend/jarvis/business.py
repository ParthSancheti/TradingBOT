import asyncio
import copy
import time
import uuid

from .sheets import AppsScript, SheetsError, SheetsRepository, UnconfiguredRepository


class BusinessService:
    """All business records are read/written through the Sheets repository."""
    def __init__(self, repository): self.repository=repository

    async def overview(self):
        system=await self.repository.get("configuration","control")
        records=await self.repository.list("tasks",50)
        events=await self.repository.list("audit",30)
        return {"control": {"id":1,"paused":True,"revision":system["revision"] if system else 0},
            "tasks":[{**r["data"],"id":r["id"],"revision":r["revision"]} for r in records],
            "audit":[{**r["data"],"id":r["id"]} for r in events]}

    async def create_task(self,title,request_key,principal):
        old=await self.repository.get("tasks",request_key)
        if old:
            if old["data"]["title"]!=title or old["data"]["principal"]!=principal: raise SheetsError("IDEMPOTENCY_CONFLICT")
            return old,False
        data={"title":title,"principal":principal,"state":"WAITING_FOR_REVIEW","created":time.time(),"request_key":request_key}
        return await self.repository.put("tasks",request_key,data,0,request_key),True

    async def cancel_task(self,task_id,revision):
        record=await self.repository.get("tasks",task_id)
        if not record or record["revision"]!=revision or record["data"]["state"]=="CANCELLED": raise SheetsError("REVISION_CONFLICT")
        return await self.repository.put("tasks",task_id,{**record["data"],"state":"CANCELLED"},revision)

    async def pause(self,revision):
        return await self.repository.put("configuration","control",{"paused":True},revision)


class VaultRepository:
    def __init__(self, vault):
        self.vault = vault
        self._cached_raw = None
        self._cached_repo = None

    def repository(self):
        try:
            import json
            raw = self.vault.get("sheets_connection") or ""
            if raw == self._cached_raw and self._cached_repo is not None:
                return self._cached_repo
            bundle = json.loads(raw or "{}")
            url, secret = bundle.get("url"), bundle.get("secret")
            if not url or not secret:
                self._cached_raw, self._cached_repo = None, None
                return UnconfiguredRepository()
            repo = SheetsRepository(AppsScript(url, secret))
            self._cached_raw, self._cached_repo = raw, repo
            return repo
        except (RuntimeError, ValueError):
            self._cached_raw, self._cached_repo = None, None
            return UnconfiguredRepository()

    async def get(self, *args, **kwargs): return await self.repository().get(*args, **kwargs)
    async def list(self, *args, **kwargs): return await self.repository().list(*args, **kwargs)
    async def put(self, *args, **kwargs): return await self.repository().put(*args, **kwargs)
    async def health(self): return await self.repository().health()
    async def batch(self, changes, request_id=None):
        return await self.repository().batch(changes, request_id)


class CachedRepository:
    """In-memory stale-while-revalidate read cache over an authoritative Sheets repo."""

    def __init__(self, inner, fresh_ttl=25.0, stale_ttl=120.0, on_update=None):
        self.inner = inner
        self.fresh_ttl = fresh_ttl
        self.stale_ttl = stale_ttl
        self.on_update = on_update
        self._entries = {}
        self._inflight = {}
        self._bg_tasks = set()

    def invalidate(self, *collections):
        if not collections:
            self._entries.clear()
            return
        targets = set(collections)
        for key in [k for k in self._entries if k[1] in targets]:
            self._entries.pop(key, None)

    def close(self):
        for task in list(self._bg_tasks):
            task.cancel()
        self._bg_tasks.clear()

    async def _fetch(self, key, loader):
        existing = self._inflight.get(key)
        if existing and not existing.done():
            return copy.deepcopy(await asyncio.shield(existing))
        task = asyncio.create_task(loader())
        self._inflight[key] = task
        try:
            result = await asyncio.shield(task)
            now = time.monotonic()
            if self.stale_ttl > 0:
                self._entries[key] = (now + self.fresh_ttl, now + self.stale_ttl, copy.deepcopy(result))
            return copy.deepcopy(result)
        finally:
            if self._inflight.get(key) is task:
                self._inflight.pop(key, None)

    def _schedule_revalidation(self, key, loader, collection):
        if any(getattr(t, "_cache_key", None) == key and not t.done() for t in self._bg_tasks):
            return

        async def run():
            previous = self._entries.get(key)
            try:
                fresh = await self._fetch(key, loader)
                if self.on_update and (previous is None or previous[2] != fresh):
                    self.on_update(collection)
            except Exception:
                pass

        task = asyncio.create_task(run())
        task._cache_key = key
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def get(self, collection, record_id, *, force=False):
        if self.stale_ttl <= 0:
            return await self.inner.get(collection, record_id)
        key = ("get", collection, record_id)
        loader = lambda: self.inner.get(collection, record_id)
        if not force:
            now = time.monotonic()
            entry = self._entries.get(key)
            if entry:
                fresh_until, stale_until, value = entry
                if now < fresh_until:
                    return copy.deepcopy(value)
                if now < stale_until:
                    self._schedule_revalidation(key, loader, collection)
                    return copy.deepcopy(value)
            # Reuse full-collection cached list read if available and non-truncated (or matching id found)
            for (k_kind, k_col, k_lim), (fresh_until, stale_until, rows) in self._entries.items():
                if k_kind == "list" and k_col == collection and now < stale_until:
                    match = next((r for r in rows if r.get("id") == record_id), None)
                    if match is not None or len(rows) < k_lim:
                        if now >= fresh_until:
                            self._schedule_revalidation(key, loader, collection)
                        return copy.deepcopy(match)
        return await self._fetch(key, loader)

    async def list(self, collection, limit=100, *, force=False):
        if self.stale_ttl <= 0:
            return await self.inner.list(collection, limit)
        key = ("list", collection, limit)
        loader = lambda: self.inner.list(collection, limit)
        if not force:
            now = time.monotonic()
            entry = self._entries.get(key)
            if not entry:
                # Satisfy smaller list reads (e.g. activity limit=50) from larger cached slices (messages limit=500).
                superset = next(
                    ((k, v) for k, v in self._entries.items() if k[0] == "list" and k[1] == collection and k[2] >= limit and now < v[1]),
                    None,
                )
                if superset:
                    sup_key, (fresh_until, stale_until, value) = superset
                    if now >= fresh_until:
                        self._schedule_revalidation(sup_key, lambda l=sup_key[2]: self.inner.list(collection, l), collection)
                    return copy.deepcopy(value[:limit])
            else:
                fresh_until, stale_until, value = entry
                if now < fresh_until:
                    return copy.deepcopy(value)
                if now < stale_until:
                    self._schedule_revalidation(key, loader, collection)
                    return copy.deepcopy(value)
        return await self._fetch(key, loader)

    async def put(self, collection, record_id, data, expected_revision, request_id=None):
        result = await self.inner.put(collection, record_id, data, expected_revision, request_id)
        self.invalidate(collection)
        if self.stale_ttl > 0 and result is not None:
            now = time.monotonic()
            self._entries[("get", collection, record_id)] = (now + self.fresh_ttl, now + self.stale_ttl, copy.deepcopy(result))
        return result

    async def batch(self, changes, request_id=None):
        result = await self.inner.batch(changes, request_id)
        self.invalidate(*(c["collection"] for c in changes if isinstance(c, dict) and "collection" in c))
        return result

    async def health(self):
        return await self.inner.health()

    def __getattr__(self, name):
        return getattr(self.inner, name)
