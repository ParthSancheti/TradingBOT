"""Signed Apps Script transport and Sheets-only business repository."""
import asyncio
import base64
import hashlib
import hmac
import json
import secrets
import time
import uuid
import weakref
from urllib.parse import urlsplit

_LOOP_CLIENTS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def _pooled_client():
    import httpx
    loop = asyncio.get_running_loop()
    client = _LOOP_CLIENTS.get(loop)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(
            timeout=25,
            limits=httpx.Limits(max_keepalive_connections=12, max_connections=24, keepalive_expiry=60),
            follow_redirects=False,
        )
        _LOOP_CLIENTS[loop] = client
    return client


class SheetsError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def signature(secret, value):
    return hmac.new(secret.encode(), canonical(value).encode(), hashlib.sha256).hexdigest()


def validate_script_url(url):
    u = urlsplit(url)
    if u.scheme != "https" or u.netloc != "script.google.com" or not u.path.startswith("/macros/s/") or not u.path.endswith("/exec") or u.query or u.fragment or u.username:
        raise ValueError("Use the deployed HTTPS script.google.com /macros/s/.../exec URL")
    return url


class AppsScript:
    def __init__(self, url, secret, transport=None):
        self.url = validate_script_url(url)
        if len(secret) < 32:
            raise ValueError("Apps Script signing secret must contain at least 32 characters")
        self.secret, self.transport = secret, transport

    async def call(self, action, payload=None, request_id=None):
        import httpx
        body = {"version":1,"request_id":request_id or str(uuid.uuid4()),"timestamp":int(time.time()),"nonce":secrets.token_urlsafe(24),"action":action,"payload":payload or {}}
        encoded=base64.b64encode(canonical(body).encode()).decode("ascii")
        envelope={"body":encoded,"signature":hmac.new(self.secret.encode(),encoded.encode(),hashlib.sha256).hexdigest()}

        async def send(client):
            for attempt in range(3):
                try:
                    response = await client.post(self.url,json=envelope)
                    # Apps Script ContentService returns a one-time Google download URL.
                    if response.status_code in (302,303):
                        location=response.headers.get("location", "")
                        u=urlsplit(location)
                        if u.scheme!="https" or u.netloc!="script.googleusercontent.com" or u.username:
                            raise SheetsError("UNTRUSTED_REDIRECT")
                        response=await client.get(location)
                    if len(response.content)>2_000_000:
                        raise SheetsError("RESPONSE_TOO_LARGE")
                    if response.status_code in (429,502,503,504):
                        raise httpx.ReadTimeout("Retryable Sheets transport")
                    response.raise_for_status()
                    wrapper=response.json();encoded_response=wrapper.get("body", "")
                    proof=wrapper.get("signature", "")
                    expected=hmac.new(self.secret.encode(),encoded_response.encode(),hashlib.sha256).hexdigest()
                    if not hmac.compare_digest(proof,expected): raise SheetsError("RESPONSE_NOT_VERIFIED")
                    data=json.loads(base64.b64decode(encoded_response,validate=True))
                    if data.get("request_id")!=body["request_id"] or data.get("nonce")!=body["nonce"]:
                        raise SheetsError("RESPONSE_NOT_VERIFIED")
                    if not data.get("ok"):
                        if data.get("code")=="BUSY_RETRY": raise httpx.ReadTimeout("Script busy")
                        raise SheetsError(data.get("code", "SHEETS_REJECTED"))
                    return data["result"]
                except (httpx.TimeoutException,httpx.NetworkError,httpx.ProtocolError):
                    if attempt==2:
                        raise SheetsError("SHEETS_UNAVAILABLE") from None
                    # Same signed request ID on retries. The server recovers prepared work.
                    await asyncio.sleep(.15*(2**attempt))
                except (httpx.HTTPStatusError,ValueError,TypeError,KeyError,AttributeError):
                    raise SheetsError("SHEETS_RESPONSE_INVALID") from None

        if self.transport is not None:
            async with httpx.AsyncClient(timeout=25, transport=self.transport, follow_redirects=False) as client:
                return await send(client)
        return await send(_pooled_client())


class SheetsRepository:
    def __init__(self, client): self.client=client
    async def get(self, collection, record_id):
        return (await self.client.call("get",{"collection":collection,"id":record_id})).get("record")
    async def list(self, collection, limit=100, offset=0):
        resp = await self.client.call("list", {"collection": collection, "limit": limit, "offset": offset})
        if isinstance(resp, dict):
            return resp.get("records", [])
        return resp or []

    async def list_all(self, collection, page_size=500):
        """Fetch complete collection across pages stably."""
        all_records = []
        offset = 0
        limit = min(max(1, page_size), 500)
        while True:
            resp = await self.client.call("list", {"collection": collection, "limit": limit, "offset": offset})
            records = resp.get("records", []) if isinstance(resp, dict) else (resp or [])
            if not records:
                break
            all_records.extend(records)
            has_more = resp.get("has_more") if isinstance(resp, dict) else None
            total = resp.get("total") if isinstance(resp, dict) else None
            if has_more is False:
                break
            if len(records) < limit:
                break
            if total is not None and len(all_records) >= total:
                break
            offset += len(records)
        return all_records
    async def put(self, collection, record_id, data, expected_revision, request_id=None):
        result=await self.client.call("mutate",{"changes":[{"collection":collection,"id":record_id,"expected_revision":expected_revision,"data":data}]},request_id)
        return result["records"][0]
    async def batch(self, changes, request_id=None):
        return await self.client.call("mutate",{"changes":changes},request_id)
    async def health(self): return await self.client.call("health")


class UnconfiguredRepository:
    async def get(self,*args,**kwargs): raise SheetsError("SHEETS_NOT_CONFIGURED")
    list = list_all = put = health = batch = get
