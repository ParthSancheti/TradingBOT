"""OS-account desktop broker, reachable only through its parent process pipes.

The Electron main process owns this child. There is deliberately no HTTP listener
and no browser-accessible authentication shortcut. Business data stays in Sheets;
signing/provider/device secrets remain in the current OS account's vault.
"""
import asyncio
import base64
import hashlib
import inspect
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, StrictInt, ValidationError, model_validator

from .business import CachedRepository, VaultRepository
from .desktop import open_whatsapp, delete_owner_avatar, owner_avatar_path, runtime_directory
from .diagnostics import DiagnosticsLog
from .local_repo import SQLiteSyncedRepository
from .onboarding import BusinessSetup, Source
from .runner import Runner
from .secrets import SecretVault
from .sheets import AppsScript, SheetsError, validate_script_url
from .gemini import GeminiClient, GeminiError, DEFAULT_MODEL
from .workspace import AgentToolbox, AssistantContext, InventoryEntry, insights, assistant, voice_turn
from .pipeline import IntakePipeline
from .market_watch import MarketWatch
from .intelligence import DeskSettings,settings,match_opportunities
from .fx import FXUnavailable, calculate_dynamic_quote, convert_reference, pair_history, preferred_currency, world_currencies, world_rates
from .trading_engine import compose_dealer_quote, parse_inventory_any_format, handle_agent_command

MAX_REQUEST = 32_768
MAX_AUDIO_REQUEST = 4 * 1024 * 1024


class DesktopError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message


class Params(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ConnectParams(Params):
    script_url: str = Field(max_length=2048)
    signing_secret: SecretStr | None = Field(default=None, max_length=1024)


class SetupParams(Params):
    owner_phone: str = Field(max_length=30)
    stock_phones: list[str] = Field(min_length=1, max_length=100)
    database_phone: str | None = Field(default=None, max_length=30)
    lead_capture: bool = True
    scan_all: bool | None = None
    classifier_daily_limit: int = Field(default=500, ge=10, le=10000)


class ActivityParams(Params):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)
    limit: int = Field(default=30, ge=1, le=100)
    offset: int = Field(default=0, ge=0)
    refresh: bool = False
    category: str = Field(default="ALL", pattern=r"^(ALL|WORK|BUY|SELL|STOCK|REVIEW|SOURCE|SOCIAL|OUTGOING)$")
    person: str = Field(default="", max_length=160)
    query: str = Field(default="", max_length=160)
    include_people: bool = False


class RefreshParams(Params):
    refresh: bool = False


class DiscoverParams(Params):
    auto_select: bool = True


class ProviderParams(Params):
    keys: list[SecretStr | StrictInt] = Field(min_length=1, max_length=5)
    model: str = Field(default=DEFAULT_MODEL,pattern=r"^gemini-[a-zA-Z0-9.\-]{1,90}$")


class ChatParams(Params):
    question: str=Field(min_length=1,max_length=4000)
    request_id: str=Field(pattern=r"^[a-zA-Z0-9_-]{16,80}$")
    page: str=Field(default="desk",max_length=40)
    chat_id: str=Field(default="legacy",pattern=r"^[a-zA-Z0-9_-]{1,80}$")


class VoiceTurnParams(Params):
    audio_wav_b64: str | None = Field(default=None, max_length=1_800_000)
    text: str | None = Field(default=None, max_length=4000)
    request_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{16,80}$")
    page: str = Field(default="assistant", max_length=40)
    chat_id: str = Field(default="voice", pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    voice_id: str = Field(default="default", max_length=320)
    rate: int = Field(default=1, ge=-6, le=6)
    speak: bool = True


class SpeakParams(Params):
    text: str = Field(min_length=1, max_length=2500)
    voice_id: str = Field(default="default", max_length=320)
    rate: int = Field(default=1, ge=-6, le=6)


class HistoryParams(Params):
    refresh: bool=False


class MessageTraceParams(Params):
    message_id: str = Field(min_length=1, max_length=120)
    refresh: bool = False


class ConvertParams(Params):
    amount: Decimal=Field(gt=0,le=1000000000000,max_digits=18,decimal_places=6)
    base: str=Field(default="USD",pattern=r"^[A-Z]{3}$")
    quote: str=Field(pattern=r"^[A-Z]{3}$")


class WorldFxParams(Params):
    base: str=Field(default="USD",pattern=r"^[A-Z]{3}$")
    symbols: list[str]=Field(default_factory=list,max_length=24)
    compare_quotes: list[str]=Field(default_factory=list,max_length=8)
    days: int=Field(default=30,ge=7,le=365)
    refresh: bool=False


class PairHistoryParams(Params):
    base: str=Field(default="USD",pattern=r"^[A-Z]{3}$")
    quote: str=Field(default="INR",pattern=r"^[A-Z]{3}$")
    days: int=Field(default=90,ge=7,le=365)
    refresh: bool=False


class MarketParams(Params):
    fx_key: SecretStr | None=Field(default=None,max_length=1024)
    news_key: SecretStr | None=Field(default=None,max_length=1024)


class InventoryBulk(Params):
    rows: list[InventoryEntry] = Field(min_length=1, max_length=500)
    request_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{16,80}$")
    mode: Literal["add", "overwrite"] = "add"


class InventoryParseParams(Params):
    raw_text: str = Field(default="", max_length=60000)
    file_b64: str | None = Field(default=None, max_length=2_000_000)
    file_name: str = Field(default="", max_length=160)
    file_mime: str = Field(default="", max_length=80)


class OutboundListParams(Params):
    statuses: list[str] | None = None
    limit: int | None = Field(default=50, ge=1, le=500)


class OutboundActionParams(Params):
    id: str = Field(min_length=4, max_length=120)
    action: str = Field(pattern=r"^(approve|dismiss)$")
    expected_updated_at: float | None = None


class OutboundDraftParams(Params):
    recipient_phone: str = Field(default="", max_length=30)
    text: str = Field(default="", max_length=3000)
    approve_now: bool = False
    conversation_id: str | None = None
    client_request_id: str | None = None
    attachment_path: str | None = None
    caption: str | None = Field(default=None, max_length=3000)
    attachment_name: str | None = Field(default=None, max_length=255)
    attachment_mime: str | None = Field(default=None, max_length=120)
    attachment_type: str | None = Field(default=None, max_length=40)
    attachment_size: int | None = None

    @model_validator(mode="after")
    def validate_content_presence(self):
        has_text = bool(self.text and self.text.strip())
        has_att = bool(self.attachment_path and self.attachment_path.strip())
        if not has_text and not has_att:
            raise ValueError("Either message text or an attachment must be provided.")
        return self


class ContactTagParams(Params):
    conversation_id: str = Field(min_length=5, max_length=120)
    category: str = Field(pattern=r"^(VIP_BUYER|SUPPLIER_DEALER|WHOLESALE_GROUP|FAMILY_PERSONAL|IGNORED|UNKNOWN)$")


class ContactUnmuteParams(Params):
    conversation_id: str = Field(min_length=3, max_length=120)


class WaScanModeParams(Params):
    mode: Literal["new_only", "all"] = "new_only"


class StartParams(Params):
    mode: Literal["new_only", "all"] | None = None


class Request(Params):
    id: str | int
    method: str = Field(max_length=60)
    params: dict = Field(default_factory=dict)


def _default_desktop_cache_path():
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) / "JarvisTrade" if base else (Path.home() / ".jarvis")
    return root / "workspace-cache.json"


class DesktopService:
    def __init__(self, vault=None, repository=None, runner=None, opener=None, *, cache_ttl=None, cache_path=None):
        self.vault = vault or SecretVault()
        self.diagnostics = DiagnosticsLog(Path(cache_path).with_name("diagnostics.db") if cache_path else None)
        if repository is None:
            raw_repo = VaultRepository(self.vault)
            self.repo = SQLiteSyncedRepository(
                raw_repo, db_path=runtime_directory() / "workspace.db",
                on_sync_error=lambda error: self.diagnostics.record("worker/sheets", level="warning", code=error.code),
            )
        else:
            raw_repo = repository
            use_cache = (repository is None) if cache_ttl is None else (cache_ttl > 0)
            ttl = (25.0 if cache_ttl is None else cache_ttl) if use_cache else 0.0
            self.repo = raw_repo if isinstance(raw_repo, CachedRepository) else CachedRepository(
                raw_repo, fresh_ttl=ttl, stale_ttl=(ttl * 5 if ttl > 0 else 0.0), on_update=self._notify_collection_update,
            )
        self.runner = runner or Runner(self.vault, self.repo, on_change=self._notify_runner_change)
        self.runner.diagnostic_event = self.diagnostics.record
        if getattr(self.runner, "on_change", None) is None:
            self.runner.on_change = self._notify_runner_change
        self.opener = opener or open_whatsapp
        self.connected = False
        self.configuration = None
        self.checked_at = None
        self.connection_error = None
        self.bridge_error = None
        self.initializing=True
        self.market = MarketWatch(self.repo,self.vault)
        self.emit=lambda event,data:None
        self._loop=None
        self._agent_command_lock = asyncio.Lock()
        self.runner.on_agent_command = self._schedule_agent_command
        self._agent_command_tasks = set()
        self.assistant_context=AssistantContext(self.repo,lambda request_id,stage:self.emit(
            "assistant",{"request_id":request_id,"stage":stage},
        ))
        self.assistant_lock=asyncio.Lock()
        self.diagnostic_lock=asyncio.Lock()
        self._active_voice_tasks: dict[str, asyncio.Task] = {}
        self._live = None
        self._live_model = None
        self._warm_task = None
        self._bg_diag_task = None
        self._cache_path = Path(cache_path) if cache_path else None
        self._cached_bootstrap = None
        self._load_runtime_cache()

    def _load_runtime_cache(self):
        data = {}
        if self._cache_path:
            try:
                raw = self._cache_path.read_text(encoding="utf-8")
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    data = parsed
            except Exception:
                data = {}
        cfg = data.get("configuration")
        if isinstance(cfg, dict) and isinstance(cfg.get("data"), dict):
            try:
                validated = BusinessSetup.model_validate(cfg["data"]).model_dump()
                self.configuration = {"id": str(cfg.get("id") or "business"), "data": validated}
            except ValidationError:
                pass
        snap = data.get("snapshot")
        if isinstance(snap, dict):
            self.assistant_context.snapshot = snap
            # Keep warm briefly so first assistant/voice turn works immediately before Sheets finishes.
            self.assistant_context.expires = time.monotonic() + 15
        mkt = data.get("market")
        if isinstance(mkt, dict):
            restored_mkt = {
                k: v for k, v in mkt.items()
                if k in ("fx", "news", "forecast") and isinstance(v, dict)
            }
            if restored_mkt:
                self.assistant_context.market = restored_mkt
                self.assistant_context.market_expires = time.monotonic() + 20
        turns = data.get("history_turns")
        if isinstance(turns, list) and turns:
            clean_turns = [t for t in turns[-60:] if isinstance(t, dict) and t.get("request_id")]
            if clean_turns:
                self.assistant_context.history_record = {"data": {"turns": clean_turns}}
                # Keep history_expires = 0 so next assistant/history call still syncs fresh turns from Sheets.
                self.assistant_context.history_expires = 0
        boot = data.get("bootstrap")
        if isinstance(boot, dict):
            self._cached_bootstrap = {
                k: boot.get(k) for k in ("settings", "activity", "dashboard", "market", "alerts")
                if boot.get(k) is not None
            } or None
            if isinstance(boot.get("settings"), dict) and boot["settings"].get("agent_name"):
                self.runner.agent_name = str(boot["settings"]["agent_name"])[:40]
        prof = data.get("profile")
        if (not isinstance(prof, dict) or not prof.get("name")) and hasattr(self.repo, "get_local_state"):
            try:
                prof = self.repo.get_local_state("owner_profile")
            except Exception:
                pass
        expected_owner = self.configuration["data"].get("owner_phone") if self.configuration else None
        if isinstance(prof, dict) and prof.get("name"):
            prof_phone = prof.get("phone") or prof.get("owner_phone")
            expected_digits = re.sub(r"\D", "", str(expected_owner or ""))
            prof_digits = re.sub(r"\D", "", str(prof_phone or ""))
            if not expected_digits or not prof_digits or prof_digits == expected_digits:
                avatar = prof.get("avatar")
                if not avatar:
                    try:
                        p = owner_avatar_path()
                        if p.is_file():
                            avatar = "data:image/jpeg;base64," + base64.b64encode(p.read_bytes()).decode("ascii")
                    except OSError:
                        pass
                self.runner.profile = {
                    "phone": prof_phone or expected_owner,
                    "owner_phone": prof.get("owner_phone") or expected_owner or prof_phone,
                    "name": str(prof["name"])[:100],
                    "avatar": avatar,
                    "avatar_checked": bool(avatar or prof.get("avatar_checked", False)),
                    "verified_at": prof.get("verified_at"),
                }
        cursor = data.get("wa_cursor")
        if isinstance(cursor, dict):
            if cursor.get("scan_mode") in ("new_only", "all"):
                self.runner.scan_mode = cursor["scan_mode"]
            if isinstance(cursor.get("immutable_floor_ts"), (int, float)):
                self.runner.immutable_floor_ts = float(cursor["immutable_floor_ts"])
            if isinstance(cursor.get("scan_since_ts"), (int, float)):
                self.runner.scan_since_ts = float(cursor["scan_since_ts"])
            if isinstance(cursor.get("last_stopped_at"), (int, float)):
                self.runner.last_stopped_at = float(cursor["last_stopped_at"])
            if isinstance(cursor.get("last_event_ts"), (int, float)):
                self.runner.last_event_ts = float(cursor["last_event_ts"])
            # Always boot stopped: cached running=True or crash never starts active session.
            # Startup lock requires explicit Start to begin a new epoch.
            self.runner.active = False
            self.runner.always_start_stopped = True

    def _save_runtime_cache(self):
        if not self._cache_path:
            return
        try:
            existing = {}
            if self._cache_path.exists():
                try:
                    loaded = json.loads(self._cache_path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        existing = loaded
                except Exception:
                    existing = {}
            cfg = None
            if isinstance(self.configuration, dict) and isinstance(self.configuration.get("data"), dict):
                cfg = {
                    "id": str(self.configuration.get("id") or "business"),
                    "data": BusinessSetup.model_validate(self.configuration["data"]).model_dump(),
                }
            snap = self.assistant_context.snapshot if isinstance(self.assistant_context.snapshot, dict) else None
            mkt = {
                k: v for k, v in (self.assistant_context.market or {}).items()
                if k in ("fx", "news", "forecast") and isinstance(v, dict)
            }
            turns = self.assistant_context.local_turns()[-60:]
            prof = getattr(self.runner, "profile", None)
            safe_prof = None
            if isinstance(prof, dict) and prof.get("name"):
                safe_prof = {
                    "phone": prof.get("phone"),
                    "owner_phone": prof.get("owner_phone"),
                    "name": prof.get("name"),
                    "avatar": prof.get("avatar"),
                    "avatar_checked": bool(prof.get("avatar") or prof.get("avatar_checked", False)),
                    "verified_at": prof.get("verified_at"),
                }
            payload = {
                "version": 1,
                "saved_at": time.time(),
                "configuration": cfg if cfg is not None else existing.get("configuration"),
                "profile": safe_prof if safe_prof is not None else existing.get("profile"),
                "snapshot": snap if snap is not None else existing.get("snapshot"),
                "market": mkt or existing.get("market") or {},
                "history_turns": turns or existing.get("history_turns") or [],
                "bootstrap": self._cached_bootstrap if self._cached_bootstrap is not None else existing.get("bootstrap"),
                "wa_cursor": {
                    "scan_mode": getattr(self.runner, "scan_mode", "new_only"),
                    "immutable_floor_ts": getattr(self.runner, "immutable_floor_ts", None),
                    "scan_since_ts": getattr(self.runner, "scan_since_ts", None),
                    "last_stopped_at": getattr(self.runner, "last_stopped_at", None),
                    "last_event_ts": getattr(self.runner, "last_event_ts", None),
                    "running": bool(self.runner.active),
                },
            }
            if not any((payload["configuration"], payload["profile"], payload["snapshot"], payload["market"], payload["history_turns"], payload["bootstrap"])):
                return
            encoded = json.dumps(payload, ensure_ascii=False)
            if len(encoded) > 1_500_000:
                payload["history_turns"] = (payload["history_turns"] or [])[-25:]
                encoded = json.dumps(payload, ensure_ascii=False)
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._cache_path.with_suffix(".tmp")
            tmp.write_text(encoded, encoding="utf-8")
            os.replace(tmp, self._cache_path)
        except Exception:
            pass

    @property
    def live(self):
        if self._live is None:
            from .live import LiveSession
            self._live = LiveSession(self.repo, self.vault, lambda e, d: self.emit(e, d), self.assistant_context)
            if self._live_model:
                self._live.remember_model(self._live_model)
        return self._live

    def _notify_collection_update(self, collection):
        if collection == "messages":
            self.assistant_context.expires = 0
        self.emit("data-updated", {"collections": [collection]})

    def _notify_runner_change(self, scope):
        def apply():
            if scope == "messages":
                self.repo.invalidate("messages", "requests", "offers")
                self.assistant_context.expires = 0
                self._save_runtime_cache()
                self.emit("data-updated", {"collections": ["messages"]})
            elif scope == "profile":
                self._save_runtime_cache()
                self.emit("profile-updated", getattr(self.runner, "profile", None))
                self.emit("refresh", {})
                self.emit("data-updated", {"collections": ["profile"]})
            elif scope == "bridge":
                self.emit("bridge-status", {**self.runner.status(), "error": self.bridge_error, "running": self.runner.active})
            elif scope == "outbound":
                self.emit("data-updated", {"collections": ["outbound"]})
            elif scope == "inventory":
                self.emit("data-updated", {"collections": ["inventory"]})
        loop = self._loop
        if loop and loop.is_running():
            try:
                if asyncio.get_running_loop() is loop:
                    apply()
                    return
            except RuntimeError:
                pass
            loop.call_soon_threadsafe(apply)
        else:
            apply()

    def _schedule_agent_command(self, command):
        if self._loop is None:
            return
        def schedule():
            task = asyncio.create_task(self._process_agent_command(command))
            self._agent_command_tasks.add(task)
            task.add_done_callback(self._agent_command_tasks.discard)
        self._loop.call_soon_threadsafe(schedule)

    async def _process_agent_command(self, command):
        async with self._agent_command_lock:
            if not self.runner.active:
                return
            message_id = command["message_id"]
            job_id = "alert-wa-agent-" + hashlib.sha256(message_id.encode()).hexdigest()[:32]
            started_at = time.time()
            data = {
                "kind": "task", "category": "whatsapp_agent_command",
                "title": "WhatsApp command received", "body": "Jarvis is processing your request.",
                "state": "PROCESSING", "created_at": datetime.now(timezone.utc).isoformat(), "started_at": started_at,
            }
            job = None
            try:
                previous = self.repo.get_sync("tasks", job_id) if hasattr(self.repo, "get_sync") else await self.repo.get("tasks", job_id)
                if previous and previous["data"].get("state") in ("COMPLETED", "PROCESSING"):
                    return
                job = await self.repo.put("tasks", job_id, data, previous["revision"] if previous else 0)
                self.emit("data-updated", {"collections": ["tasks"]})
            except Exception:
                pass
            # Routine command-started is tracked as a task without emitting OS popup
            owner_phone = None
            if hasattr(self.runner, "profile") and isinstance(self.runner.profile, dict):
                owner_phone = self.runner.profile.get("owner_phone") or self.runner.profile.get("phone")
            if not owner_phone and self.configuration:
                owner_phone = self.configuration.get("data", {}).get("owner_phone")
            if not owner_phone and hasattr(self.repo, "get_local_state"):
                prof = self.repo.get_local_state("owner_profile")
                if isinstance(prof, dict):
                    owner_phone = prof.get("owner_phone") or prof.get("phone")

            def reply_to_self(text, suffix="working"):
                if command.get("self_chat") is True and hasattr(self.repo, "queue_outbound"):
                    try:
                        self.repo.queue_outbound(
                            command["conversation_id"],
                            [text],
                            kind="agent_reply",
                            status="APPROVED",
                            account_phone=owner_phone,
                            recipient_phone=owner_phone,
                            dedupe_key=f"cmd-ack:{message_id}:{suffix}",
                        )
                    except Exception:
                        pass

            reply_to_self("Jarvis received your request and is working on it.", "working")
            try:
                result = await handle_agent_command(
                    self.repo, self.vault, command.get("text", ""), command.get("conversation_id", ""),
                    runtime_dir=runtime_directory(), agent_name=self.runner.agent_name,
                    owner_phone=owner_phone,
                )
            except Exception:
                data.update(state="FAILED", reason="COMMAND_FAILED", title="WhatsApp command failed", body="Check the Gemini connection and resend the command.")
                self.emit("notification", {"id": f"{job_id}-failed", "title": "WhatsApp command failed", "body": "Check the Gemini connection and resend the command."})
                reply_to_self("Jarvis couldn't complete your request. Check the desktop app and try again.")
            else:
                data.update(state="COMPLETED", title="WhatsApp response ready", body="Jarvis prepared a reply for WhatsApp.")
                # Routine command-ready is recorded in task state without popup noise
            if job:
                try:
                    await self.repo.put("tasks", job_id, {**data, "finished_at": time.time()}, job["revision"])
                    self.emit("data-updated", {"collections": ["tasks"]})
                except Exception:
                    pass

    async def _warm_cache(self):
        if not self.connected:
            return
        try:
            # Stage 1: light Overview slices first (tasks 300 also serves desktop-assistant)
            await asyncio.gather(
                self.repo.list("messages", 50),
                self.repo.list("tasks", 300),
                self.repo.get("fx", "latest"),
                return_exceptions=True,
            )
            self.emit("data-updated", {"collections": ["messages", "tasks", "fx"]})
            # Stage 2: remaining analytics, inventory & news smoothly in background
            await asyncio.gather(
                self.repo.list("inventory", 500),
                self.repo.get("news", "latest"),
                self.repo.list("messages", 500),
                return_exceptions=True,
            )
            self.emit("data-updated", {"collections": ["inventory", "messages", "news"]})
        except Exception:
            pass

    async def initialize(self):
        self._loop = asyncio.get_running_loop()
        self.runner.service_loop = self._loop
        if hasattr(self.repo, "revoke_outbound"):
            self.repo.revoke_outbound()
        if not self.configuration and hasattr(self.repo, "get_sync"):
            try:
                local_cfg = self.repo.get_sync("configuration", "business")
                if isinstance(local_cfg, dict) and isinstance(local_cfg.get("data"), dict):
                    BusinessSetup.model_validate(local_cfg["data"])
                    self.configuration = local_cfg
            except Exception:
                pass
        if isinstance(self.configuration, dict) and isinstance(self.configuration.get("data"), dict):
            self.runner.cached_setup = self.configuration["data"]
        self.start_bridge()
        try:
            await self.settings_get({})
        except Exception:
            pass
        has_sheet = False
        try:
            has_sheet = bool(json.loads(self.vault.get("sheets_connection") or "{}").get("url"))
        except (ValueError, TypeError):
            pass
        if self.configuration and has_sheet:
            self.connected = True
            self.initializing = False
            self.emit("refresh", {})
            self._warm_task = asyncio.create_task(self._warm_cache())
            async def _bg_diag():
                try:
                    await self.diagnostic({})
                finally:
                    self.emit("refresh", {})
            self._bg_diag_task = asyncio.create_task(_bg_diag())
            return
        self.emit("refresh", {})
        try:
            await self.diagnostic({})
        finally:
            self.initializing = False
            self.emit("refresh", {})
        if self.connected:
            self._warm_task = asyncio.create_task(self._warm_cache())

    async def bootstrap(self, params):
        value = RefreshParams.model_validate(params)
        if value.refresh:
            self.repo.invalidate()
        status = await self.status({})
        if hasattr(self.repo, "message_page"):
            cached = self._cached_bootstrap or {}
            # Local-first: the dashboard and alerts come from SQLite and are
            # cheap. Serving the old JSON snapshot showed stale stock (often 0)
            # until the next refresh replaced it.
            dash, alerts = await asyncio.gather(self.dashboard({}), self.alerts_list({}), return_exceptions=True)
            dash = cached.get("dashboard") if isinstance(dash, BaseException) else dash
            alerts = cached.get("alerts") if isinstance(alerts, BaseException) else alerts
            if self._cache_path and (dash is not None or alerts is not None):
                prev = dict(cached)
                if dash is not None: prev["dashboard"] = dash
                if alerts is not None: prev["alerts"] = alerts
                self._cached_bootstrap = prev
            return {
                "status": status,
                "settings": await self.settings_get({}),
                "activity": await self.activity({"limit": 30}),
                "dashboard": dash,
                "market": cached.get("market"),
                "alerts": alerts,
                "history": None,
            }
        if not self.connected:
            cached = self._cached_bootstrap if (self._cache_path and self._cached_bootstrap) else {}
            cached_turns = self.assistant_context.local_turns() if self._cache_path else []
            return {
                "status": status,
                "settings": await self.settings_get({}) if hasattr(self.repo, "get_local_state") else cached.get("settings"),
                "activity": cached.get("activity"),
                "dashboard": cached.get("dashboard"),
                "market": cached.get("market"),
                "alerts": cached.get("alerts"),
                "history": {"turns": cached_turns, "synced": False, "pending_ids": list(self.assistant_context.pending)} if cached_turns else None,
            }
        # Preload full slices once so list/get helpers share cached rows (0 duplicate Sheet reads)
        await asyncio.gather(
            self.repo.list("messages", 500),
            self.repo.list("inventory", 500),
            self.repo.list("tasks", 300),
            self.repo.list("configuration", 50),
            return_exceptions=True,
        )
        cfg, act, dash, mkt, alerts, hist = await asyncio.gather(
            self.settings_get({}),
            self.activity({"limit": 30}),
            self.dashboard({}),
            self.market_snapshot({}),
            self.alerts_list({}),
            self.assistant_history({}),
            return_exceptions=True,
        )
        def ok(val):
            return None if isinstance(val, BaseException) else val
        result = {
            "status": status,
            "settings": ok(cfg),
            "activity": ok(act),
            "dashboard": ok(dash),
            "market": ok(mkt),
            "alerts": ok(alerts),
            "history": ok(hist),
        }
        if self._cache_path:
            prev = dict(self._cached_bootstrap or {})
            for key in ("settings", "activity", "dashboard", "market", "alerts"):
                if result[key] is not None:
                    prev[key] = result[key]
            self._cached_bootstrap = prev or None
            self._save_runtime_cache()
        return result

    def start_bridge(self):
        try:
            from .desktop import sync_native_registration
            sync_native_registration(self.vault)
        except Exception:
            pass
        try:
            if not self.vault.get("bridge_secret"):
                self.bridge_error = "Install the Jarvis Chrome connection to pair this device."
                return
            self.runner.bridge.start()
            self.bridge_error = None
        except RuntimeError:
            self.bridge_error = "Another Jarvis process is using the local connection. Close it and retry."
        except Exception:
            self.bridge_error = "The local connection could not start. Retry connection checks."

    async def extension_pair(self, params):
        if not isinstance(params, dict):
            raise DesktopError("INVALID_INPUT", "Invalid extension pair request.")
        explicit_id = str(params.get("extension_id") or "").strip()
        from .desktop import sync_native_registration, register_native_host, find_native_executable
        if explicit_id:
            import re
            if not re.fullmatch(r"[a-p]{32}", explicit_id):
                raise DesktopError("INVALID_INPUT", "Invalid 32-character Chrome extension ID.")
            exe = find_native_executable()
            if not exe:
                raise DesktopError("NATIVE_HELPER_MISSING", "Jarvis native helper binary not found.")
            existing = self.vault.get("extension_id") or ""
            current_ids = [s.strip() for s in str(existing).replace(",", " ").split() if s.strip()]
            if explicit_id not in current_ids:
                current_ids.append(explicit_id)
            manifest = register_native_host(current_ids, exe, vault=self.vault)
            self.start_bridge()
            self.emit("bridge-status", {**self.runner.status(), "error": self.bridge_error, "running": self.runner.active})
            return {"ok": True, "paired_ids": current_ids, "manifest": manifest, "manual": True}
        else:
            sync_res = sync_native_registration(vault=self.vault)
            self.start_bridge()
            self.emit("bridge-status", {**self.runner.status(), "error": self.bridge_error, "running": self.runner.active})
            return {"ok": True, **sync_res}

    def _invoke_opener(self, *, force_url=False):
        try:
            sig = inspect.signature(self.opener)
            if "force_url" in sig.parameters:
                return self.opener(force_url=force_url)
        except Exception:
            pass
        return self.opener()

    async def status(self, params):
        Params.model_validate(params)
        setup = self.configuration["data"] if self.configuration else None
        gemini=GeminiClient(self.vault).status()
        classifier = gemini["configured"]
        has_sheet = False
        try:
            has_sheet = bool(json.loads(self.vault.get("sheets_connection") or "{}").get("url"))
        except (ValueError, TypeError):
            pass
        return {
            "running": self.runner.active,
            "initializing":self.initializing,
            "sheets_configured": has_sheet,
            "sheets_connected": self.connected,
            "sheets_sync": self.repo.sheet_sync_status() if hasattr(self.repo, "sheet_sync_status") else None,
            "bridge": {**self.runner.status(), "error": self.bridge_error},
            "direct_wa": getattr(self.runner, "direct_wa_status", None),
            "agent_name": getattr(self.runner, "agent_name", "Jarvis"),
            "scan_since_ts": getattr(self.runner, "scan_since_ts", None),
            "setup": {"owner_phone": setup["owner_phone"], "stock_phones": [s["phone"] for s in setup["sources"] if s.get("active", True)], "database_phone": setup.get("database_phone")} if setup else None,
            "capabilities": {"sending": self.runner.can_send, "autonomy_mode": self.runner.autonomy_mode, "classifier": classifier, "scan_all": bool(setup and setup.get("scan_all")), "coverage": "rendered_recent_chats"},
            "connection_error": self.connection_error,
            "checked_at": self.checked_at,
            "gemini":gemini,
            "profile":getattr(self.runner,"profile",None),
            "extension": {
                "paired_ids": [s.strip() for s in str(self.vault.get("extension_id") or "").replace(",", " ").split() if s.strip()],
                "has_secret": bool(self.vault.get("bridge_secret")),
            },
        }

    async def diagnostic(self, params):
        Params.model_validate(params)
        async with self.diagnostic_lock:
            return await self.check_connections()

    async def check_connections(self):
        self.start_bridge()
        try:
            self.repo.invalidate("configuration")
            async with asyncio.timeout(16):
                health, configs = await asyncio.gather(
                    self.repo.health(),
                    self.repo.list("configuration", 50),
                )
                if health.get("schema_version") != 1 or health.get("storage") != "google_sheets":
                    raise SheetsError("UNSUPPORTED_WORKBOOK")
                record = next((r for r in configs if r.get("id") == "business"), None)
            if health.get("workbook_url"):
                bundle=json.loads(self.vault.get("sheets_connection") or "{}")
                bundle["workbook_url"]=DeskSettings(sheet_url=health["workbook_url"]).sheet_url
                self.vault.set("sheets_connection",json.dumps(bundle))
            if record:
                BusinessSetup.model_validate(record["data"])
                self.runner.cached_setup = record["data"]
            self.configuration, self.connected, self.connection_error = record, True, None
            self.assistant_context.sync_history()
            if hasattr(self.repo, "hydrate_from_remote"):
                asyncio.create_task(self.repo.hydrate_from_remote())
        except TimeoutError:
            self.connected = False
            self.connection_error = "Google Sheets timed out. Jarvis will check again automatically."
        except SheetsError as error:
            self.connected = False
            self.connection_error = {
                "SHEETS_NOT_CONFIGURED": "Connect your Google Sheet in Connections.",
                "SHEETS_UNAVAILABLE": "Google Sheets is temporarily unreachable. Jarvis will check again automatically.",
                "RESPONSE_NOT_VERIFIED": "Google Sheets returned an unverified response. Check the Apps Script connection.",
                "UNSUPPORTED_WORKBOOK": "The Apps Script deployment returned an unsupported workbook.",
            }.get(error.code,"Google Sheet could not be verified. Check the Apps Script connection.")
        except ValidationError:
            self.configuration = None
            self.connected = True
            self.connection_error = "Saved business settings need correction. Save your numbers again."
        except Exception:
            self.connected = False
            self.connection_error = "Connection check failed. Retry when the network and OS credential store are available."
        self.checked_at = time.time()
        if self.connected:
            self._save_runtime_cache()
        return await self.status({})

    async def connect(self, params):
        value = ConnectParams.model_validate(params)
        url = validate_script_url(value.script_url)
        existing = json.loads(self.vault.get("sheets_connection") or "{}")
        secret = value.signing_secret.get_secret_value() if value.signing_secret else None
        if not secret:
            secret = existing.get("secret") if existing.get("url") == url else self.vault.get("sheets_signing_secret_pending")
        if not secret:
            raise DesktopError("SIGNING_SECRET_REQUIRED", "Finish the Apps Script connection instructions first.")
        if isinstance(self.repo, SQLiteSyncedRepository):
            if existing.get("url") and existing["url"] != url:
                raise DesktopError("WORKBOOK_SWITCH_BLOCKED", "Use a separate workspace to connect a different workbook.")
            if not existing.get("url") and self.repo.has_pending_sheet_writes():
                raise DesktopError("WORKBOOK_BINDING_REQUIRED", "Existing local writes need review before connecting a workbook.")
        result = await AppsScript(url, secret).call("health")
        if result.get("schema_version") != 1 or result.get("storage") != "google_sheets":
            raise DesktopError("UNSUPPORTED_WORKBOOK", "Deploy the Jarvis Apps Script before connecting.")
        async with self.diagnostic_lock, self.assistant_lock:
            self.assistant_context.clear()
            self.repo.invalidate()
            if self._live is not None:
                await self._live.stop()
            self.configuration=None
            self.vault.set("sheets_connection", json.dumps({"url": url, "secret": secret}))
            return await self.check_connections()

    async def save_setup(self, params):
        value = SetupParams.model_validate(params)
        owner = Source.normalize(value.owner_phone)
        phones = [Source.normalize(p) for p in value.stock_phones]
        database_phone = Source.normalize(value.database_phone) if value.database_phone else None
        if database_phone == owner:
            raise DesktopError("INVALID_DATABASE_SENDER", "Choose a database sender other than your linked account.")
        if len(set(phones)) != len(phones):
            raise DesktopError("DUPLICATE_NUMBER", "Each stock source number should appear once.")
        current = await self.repo.get("configuration", "business")
        previous_data = (current or {}).get("data") or (self.configuration or {}).get("data") or {}
        previous_owner = previous_data.get("owner_phone")
        previous_sources = [s.get("phone") for s in (previous_data.get("sources") or []) if isinstance(s, dict) and s.get("phone")]
        owner_changed = bool(previous_owner and previous_owner != owner)
        if owner_changed and isinstance(self.repo, SQLiteSyncedRepository):
            raise DesktopError("ACCOUNT_SWITCH_BLOCKED", "Use a separate workspace to change the WhatsApp account.")
        sources_changed = bool(previous_data) and (set(previous_sources) != set(phones) or previous_data.get("database_phone") != database_phone)
        reset_stored_cache = owner_changed or sources_changed

        data = dict(current["data"]) if current else {"business_name": "Jarvis-Trade", "classifier_model": "not-configured", "reply_mode": "observe"}
        scan_all_flag = value.scan_all if getattr(value, "scan_all", None) is not None else (current["data"].get("scan_all", False) if current and "data" in current else False)
        data.update(owner_phone=owner, database_phone=database_phone, sources=[{"phone": p, "label": "", "purpose": "inventory", "active": True} for p in phones], scan_all=scan_all_flag, lead_capture=value.lead_capture, classifier_daily_limit=value.classifier_daily_limit)
        setup = BusinessSetup.model_validate(data)
        saved = await self.repo.put("configuration", "business", setup.model_dump(), current["revision"] if current else 0)
        self.configuration, self.connected, self.connection_error = saved, True, None
        self.runner.cached_setup = setup.model_dump()

        if owner_changed:
            self.runner.profile = None
            if hasattr(self.runner, "reset_scan_cursor"):
                self.runner.reset_scan_cursor()
            if hasattr(self.repo, "delete_local_state"):
                try:
                    self.repo.delete_local_state("owner_profile")
                except Exception:
                    pass
            if self._cache_path:
                delete_owner_avatar()
            self.runner.profile_request_until = time.time() + 180
        if reset_stored_cache:
            self.runner.config_version = getattr(self.runner, "config_version", 1) + 1
            self._cached_bootstrap = None
            self.assistant_context.clear()
            self.repo.invalidate()
            if self._cache_path:
                try:
                    self._cache_path.unlink(missing_ok=True)
                except OSError:
                    pass
            self._save_runtime_cache()
            cols = ["configuration", "messages", "profile", "cache-reset"] + (["wa-reset"] if owner_changed else [])
            self.emit("data-updated", {"collections": cols})
        elif self._cache_path:
            self._save_runtime_cache()

        st = await self.status({})
        return {**st, "reset_stored_cache": reset_stored_cache}

    async def start(self, params):
        val = StartParams.model_validate(params)
        self.start_bridge()
        if not self.configuration and hasattr(self.repo, "get_sync"):
            try:
                local_cfg = self.repo.get_sync("configuration", "business")
                if isinstance(local_cfg, dict) and isinstance(local_cfg.get("data"), dict):
                    BusinessSetup.model_validate(local_cfg["data"])
                    self.configuration = local_cfg
                    self.runner.cached_setup = local_cfg["data"]
            except Exception:
                pass

        if not self.configuration:
            raise DesktopError("SETUP_REQUIRED", "Configure your verified WhatsApp phone number in Setup before starting.")

        owner_phone = self.configuration["data"].get("owner_phone") if self.configuration else None
        has_synced = (
            self.runner.has_synced_profile(owner_phone)
            if hasattr(self.runner, "has_synced_profile")
            else bool(isinstance(getattr(self.runner, "profile", None), dict) and self.runner.profile.get("name"))
        )
        if not has_synced:
            self.runner.profile_request_until = time.time() + 180

        direct_connected = bool((getattr(self.runner, "direct_wa_status", None) or {}).get("connected"))
        if self.runner.status()["verified"]:
            self.runner.open_request_until = time.time() + 10
        elif not direct_connected:
            try:
                self._invoke_opener(force_url=True)
            except Exception:
                pass

        if hasattr(self.runner, "begin_session"):
            if val.mode:
                self.runner.begin_session(mode=val.mode)
            else:
                self.runner.begin_session()
        self.runner.active = True
        self._save_runtime_cache()
        return await self.status({})

    async def stop(self, params):
        Params.model_validate(params)
        if hasattr(self.runner, "end_session"):
            self.runner.end_session()
        else:
            self.runner.active = False
        if hasattr(self.repo, "revoke_outbound"):
            self.repo.revoke_outbound()
            self.emit("data-updated", {"collections": ["outbound"]})
        self._save_runtime_cache()
        return await self.status({})

    async def activity(self, params):
        value = ActivityParams.model_validate(params)
        if value.refresh:
            self.repo.invalidate("messages")
        people = []
        recent = []
        if hasattr(self.repo, "message_page"):
            records, has_more = self.repo.message_page(value.limit, value.offset, category=value.category,
                                                       person=value.person, query=value.query)
            if value.include_people:
                people = self.repo.message_people()
                recent, _ = self.repo.message_page(4, 0, category="IMPORTANT")
            counts = self.repo.message_counts()
            if not counts["captured"] and not value.offset:
                hydrate = getattr(self.repo, "hydrate_from_remote", None)
                if hydrate and self.connected and not (self._warm_task and not self._warm_task.done()):
                    self._warm_task = asyncio.create_task(self._hydrate_messages())
        else:
            all_rows = await self.repo.list("messages", max(100, value.offset + value.limit + 1))
            matching = []
            contacts = {}
            for row in all_rows:
                data = row["data"]
                triage = data.get("triage") or {}
                person = data.get("phone") or data.get("sender_id") or data.get("conversation_id")
                if person:
                    contacts.setdefault(person, data.get("sender_name") or data.get("sender_label") or data.get("chat_name"))
                category = triage.get("category")
                if data.get("direction") == "outgoing" or data.get("state") == "OUTGOING":
                    category = "OUTGOING"
                elif data.get("state") == "IGNORED_SOCIAL" or category == "OTHER":
                    category = "SOCIAL"
                categories = {"ALL", category}
                if category != "SOCIAL":
                    categories.add("WORK")
                if data.get("state") in ("NEEDS_REVIEW", "RECEIVED") or triage.get("needs_review"):
                    categories.add("REVIEW")
                if data.get("stock_source"):
                    categories.add("SOURCE")
                if value.category not in categories or (value.person and person != value.person):
                    continue
                searchable = " ".join(str(data.get(field) or "") for field in ("sender_name", "sender_label", "chat_name", "phone", "text"))
                if value.query and value.query.casefold() not in (searchable + " " + str(category or "")).casefold():
                    continue
                matching.append(row)
            if value.include_people:
                people = [{"person": person, "name": name} for person, name in contacts.items()]
            records = matching[value.offset:value.offset + value.limit]
            has_more = value.offset + len(records) < len(matching)
            counts = {
                "captured": len(all_rows),
                "classified": sum(row["data"].get("state") == "CLASSIFIED" for row in all_rows),
                "needs_review": sum(row["data"].get("state") in ("NEEDS_REVIEW", "RECEIVED")
                                    or bool((row["data"].get("triage") or {}).get("needs_review")) for row in all_rows),
            }
        messages = []
        for row in [*records, *recent]:
            data = {key: val for key, val in row["data"].items()
                    if key not in ("media_b64", "source_text", "stock_file_b64")}
            triage = data.get("triage") or {}
            if data.get("state") == "CLASSIFIED" and triage.get("category") == "BUY" and not data.get("buyer_pricing"):
                data = {**data, "buyer_pricing": preferred_currency(data.get("phone"), triage.get("reply_currency"))}
            messages.append({**row, "data": data})
        return {"messages": messages[:len(records)], "counts": {**counts, "visible": len(records)}, "total": counts["captured"],
                "offset": value.offset, "limit": value.limit, "limited": has_more, "has_more": has_more,
                **({"people": people, "recent": messages[len(records):]} if value.include_people else {})}

    async def _hydrate_messages(self):
        try:
            await self.repo.hydrate_from_remote(("messages",))
        except Exception:
            return
        self.emit("data-updated", {"collections": ["messages"]})

    async def message_trace(self, params):
        val = MessageTraceParams.model_validate(params)
        if hasattr(self.repo, "get_message_trace"):
            return self.repo.get_message_trace(val.message_id)
        row = await self.repo.get("messages", val.message_id)
        if not row:
            return {"schema_version": 1, "found": False, "message_id": val.message_id}
        data = row.get("data", {})
        return {
            "schema_version": 1,
            "found": True,
            "id": row.get("id"),
            "message_id": data.get("message_id") or val.message_id,
            "conversation_id": data.get("conversation_id"),
            "phone": data.get("phone"),
            "direction": data.get("direction", "incoming"),
            "text": data.get("text", ""),
            "state": data.get("state"),
            "current_stage": data.get("current_stage") or ("AI_CLASSIFICATION" if data.get("state") == "CLASSIFIED" else "CAPTURE"),
            "stage_outcome": data.get("stage_outcome") or "SUCCESS",
            "reason": data.get("reason"),
            "processing_stage": data.get("processing_stage"),
            "last_progress_at": data.get("last_progress_at") or float(data.get("received") or 0.0),
            "classification_source": data.get("classification_source"),
            "model_requested": data.get("model_requested"),
            "model_served": data.get("model_served"),
            "triage": data.get("triage"),
            "events": [],
            "decisions": [],
            "outbound_attempts": [],
        }

    async def save_provider(self, params):
        value = ProviderParams.model_validate(params)
        with GeminiClient._state_lock:
            previous = GeminiClient(self.vault).config()
            saved_keys = previous.get("keys", [])
            keys = []
            for entry in value.keys:
                if isinstance(entry, int):
                    if not 0 <= entry < len(saved_keys):
                        raise DesktopError("INVALID_KEY_SLOT", "This saved key slot is no longer available. Reopen Connections and try again.")
                    keys.append(saved_keys[entry])
                else:
                    keys.append(entry.get_secret_value().strip())
            if any(len(key) < 16 or len(key) > 1024 for key in keys) or len(set(keys)) != len(keys):
                raise DesktopError("INVALID_KEYS", "Enter one to five different Gemini API keys.")
            if keys != saved_keys or value.model != previous.get("model"):
                self.vault.set("gemini_connection", json.dumps({
                    "model": value.model, "keys": keys, "paused_until": previous.get("paused_until", 0),
                }))
        return await self.status({})

    async def test_provider(self,params):
        Params.model_validate(params)
        client = GeminiClient(self.vault)
        status=client.status()
        if not status["configured"]:
            raise GeminiError("MODEL_KEY_REQUIRED", "Save a Gemini API key first.")
        if status["paused_until"]>time.time() and not status.get("active_key_count"):
            raise GeminiError("MODEL_COOLDOWN","Gemini quota cooldown is active.")
        client.reserve_manual_call()
        started=time.monotonic()
        try:
            result = await client.classify("WTB 10 iPhone 17 256GB")
            return {"verified":True,"sample":"WTB 10 iPhone 17 256GB","output":result.model_dump(),
                    "model":client.status().get("last_model") or status["model"],
                    "latency_ms":round((time.monotonic()-started)*1000),"status":await self.status({})}
        except GeminiError as err:
            if err.code in ("MODEL_OVERLOADED", "MODEL_NOT_FOUND", "MODEL_UNAVAILABLE"):
                probe = await client.discover_and_probe_models(auto_select=True)
                working = [p for p in probe["probes"] if p["ok"]]
                if working:
                    result = await client.classify("WTB 10 iPhone 17 256GB")
                    return {"verified":True,"sample":"WTB 10 iPhone 17 256GB","output":result.model_dump(),
                            "model":probe["selected_model"],"auto_switched":True,"probes":probe["probes"],
                            "available_live_models":probe["available_live_models"],
                            "latency_ms":round((time.monotonic()-started)*1000),"status":await self.status({})}
            raise

    async def discover_provider(self, params):
        value = DiscoverParams.model_validate(params)
        client = GeminiClient(self.vault)
        if not client.status()["configured"]:
            raise GeminiError("MODEL_KEY_REQUIRED", "Save a Gemini API key first.")
        probe = await client.discover_and_probe_models(auto_select=value.auto_select)
        return {**probe, "status": await self.status({})}

    async def reveal_provider_key(self, params):
        index = params.get("index")
        keys = GeminiClient(self.vault).config().get("keys", [])
        if type(index) is not int or not 0 <= index < len(keys):
            raise ValueError("Invalid key slot")
        return {"key": keys[index]}

    async def sync_profile(self, params):
        Params.model_validate(params)
        self.start_bridge()
        self.runner.profile_request_until = time.time() + 120
        if not self.runner.status()["verified"]:
            self._invoke_opener(force_url=True)
        return {"requested": True}

    async def open_browser(self, params):
        Params.model_validate(params)
        self.start_bridge()
        self.runner.open_request_until=time.time()+120
        if not self.runner.status()["verified"]:
            self._invoke_opener(force_url=True)
        return {"requested": True}

    async def wa_sync(self, params):
        if not isinstance(params, dict):
            raise DesktopError("INVALID_INPUT", "Invalid direct WhatsApp sync payload.")
        if isinstance(params.get("status"), dict):
            st = params["status"]
            self.runner.direct_wa_status = {
                "state": str(st.get("state", "disconnected"))[:32],
                "connected": bool(st.get("connected")),
                "has_session": bool(st.get("has_session")),
                "error": str(st.get("error", ""))[:200],
            }
        prof = params.get("profile")
        if isinstance(prof, dict) and prof.get("verified") is True:
            owner = self.configuration["data"].get("owner_phone") if self.configuration else None
            await self.runner._store_profile(prof, owner_phone=owner)
        human_replies = params.get("human_replies")
        if isinstance(human_replies, list) and hasattr(self.repo, "upsert_contact"):
            cfg = await settings(self.repo)
            mute_sec = float(cfg.get("human_mute_hours") or self.runner.human_mute_hours or 4.0) * 3600.0
            now = time.time()
            for hr in human_replies:
                cid = hr.get("conversation_id") if isinstance(hr, dict) else None
                if not cid:
                    continue
                message_id = hr.get("message_id")
                if hasattr(self.repo, "has_outbound_receipt") and self.repo.has_outbound_receipt(message_id):
                    continue
                stamp = hr.get("timestamp")
                remaining = mute_sec if type(stamp) not in (int, float) else min(mute_sec, stamp + mute_sec - now)
                if remaining > 0:
                    self.repo.upsert_contact(cid, manual_mute_seconds=remaining)
            self.emit("data-updated", {"collections": ["contacts", "outbound"]})
        events = params.get("events")
        saved_count = 0
        results = []
        if isinstance(events, list) and events:
            if not self.runner.active:
                for ev in events:
                    if isinstance(ev, dict):
                        results.append({"message_id": ev.get("message_id"), "conversation_id": ev.get("conversation_id"), "status": "RETRY_REQUIRED", "reason": "RUNNER_INACTIVE"})
            elif not self.configuration:
                for ev in events:
                    if isinstance(ev, dict):
                        results.append({"message_id": ev.get("message_id"), "conversation_id": ev.get("conversation_id"), "status": "RETRY_REQUIRED", "reason": "ONBOARDING_REQUIRED"})
            else:
                setup = BusinessSetup.model_validate(self.configuration["data"]).model_dump()
                gemini = GeminiClient(self.vault)
                setup["classifier_configured"] = bool(gemini.status().get("configured"))
                pipeline = IntakePipeline(self.repo, None, local_first=hasattr(self.repo, "message_counts"))
                for ev in events:
                    if not isinstance(ev, dict):
                        continue
                    mid = ev.get("message_id")
                    cid = ev.get("conversation_id")
                    accepted, reason = self.runner.accept_event_ts(ev) if hasattr(self.runner, "accept_event_ts") else (True, None)
                    if not accepted:
                        results.append({"message_id": mid, "conversation_id": cid, "status": reason or "IGNORED_OLD"})
                        continue
                    try:
                        res = await pipeline.process(ev, setup)
                        self.runner.observations += 1
                        saved_count += 1
                        results.append({"message_id": mid, "conversation_id": cid, "id": res.get("id"), "status": res.get("status", "SAVED")})
                    except Exception as exc:
                        results.append({"message_id": mid, "conversation_id": cid, "status": "RETRY_REQUIRED", "reason": str(exc)})
            if saved_count:
                self.runner.last_batch_at = time.time()
                self.repo.invalidate("messages", "requests", "offers")
                self.assistant_context.expires = 0
                self._save_runtime_cache()
                self.emit("data-updated", {"collections": ["messages"]})
        return {"ok": True, "saved": saved_count, "results": results, "profile": getattr(self.runner, "profile", None), "scan_since_ts": getattr(self.runner, "scan_since_ts", None)}

    async def wa_scan_mode(self, params):
        val = WaScanModeParams.model_validate(params)
        if hasattr(self.runner, "begin_session"):
            self.runner.begin_session(mode=val.mode)
        elif val.mode == "new_only":
            self.runner.scan_since_ts = time.time()
        else:
            self.runner.reset_scan_cursor()
        self._save_runtime_cache()
        self.emit("data-updated", {"collections": ["messages"]})
        return {"ok": True, "mode": val.mode, "scan_since_ts": self.runner.scan_since_ts}

    async def prepare_sheet(self, params):
        Params.model_validate(params)
        health = await self.repo.health()
        if health.get("storage") != "google_sheets":
            raise DesktopError("SHEET_INVALID", "Connect the Jarvis Google Sheet first.")
        collections = ("configuration", "messages", "inventory", "requests", "offers", "tasks", "fx", "news")
        self.repo.invalidate(*collections)
        if hasattr(self.repo, "flush_outbox"):
            await self.repo.flush_outbox()
        populated = {}
        for collection in collections:
            rows = await self.repo.list(collection, 1, force=True)
            populated[collection] = bool(rows)
        return {"tabs": populated}

    async def dashboard(self, params):
        value = RefreshParams.model_validate(params)
        if value.refresh:
            self.repo.invalidate("messages", "inventory")
            if hasattr(self.repo, "hydrate_from_remote"):
                try:
                    await self.repo.hydrate_from_remote(("inventory", "offers"))
                except Exception:
                    pass
        return await insights(self.repo)

    async def inventory_save(self,params):
        value=InventoryEntry.model_validate(params)
        data=value.model_dump(exclude={"id","expected_revision"}, exclude_none=True)
        data.update(owned_stock=True,verified_by="local_operator")
        result=await self.repo.put("inventory",value.id,data,value.expected_revision)
        self.assistant_context.expires=0
        return result

    async def inventory_bulk(self,params):
        value=InventoryBulk.model_validate(params)
        if len({r.id for r in value.rows})!=len(value.rows):raise ValueError("Duplicate inventory row IDs")
        changes=[]
        for row in value.rows:
            if value.mode == "add" and row.expected_revision != 0:
                continue
            data=row.model_dump(exclude={"id","expected_revision"}, exclude_none=True);data.update(owned_stock=True,verified_by="local_operator")
            changes.append({"collection":"inventory","id":row.id,"expected_revision":row.expected_revision,"data":data})
        if changes:
            result=await self.repo.batch(changes,value.request_id)
        else:
            result={"records":[]}
        self.assistant_context.expires=0
        return result

    async def inventory_parse(self, params):
        value = InventoryParseParams.model_validate(params)
        gemini = GeminiClient(self.vault)
        st = gemini.status()
        if not st.get("configured"):
            raise GeminiError("MODEL_KEY_REQUIRED", "Save a Gemini API key in Connections to convert files with AI.")
        existing = await self.repo.list("inventory", 500)
        parsed = await parse_inventory_any_format(
            gemini,
            raw_text=value.raw_text,
            file_b64=value.file_b64,
            file_name=value.file_name,
            file_mime=value.file_mime,
            existing_inventory=existing,
        )
        return parsed

    async def daily_stock_status(self, params):
        Params.model_validate(params)
        record = await self.repo.get("configuration", "daily-stock")
        if not record:
            return {"snapshot": None}
        stamp = record["data"]["source_timestamp"]
        return {"snapshot": {"source_timestamp": stamp, "row_count": len(record["data"]["items"]),
                             "fresh": 0 <= time.time() - stamp < 86400}}

    async def market_snapshot(self, params):
        value = RefreshParams.model_validate(params)
        if value.refresh:
            self.repo.invalidate("fx", "news")
        scope = self.assistant_context.scope
        result = await self.market.snapshot()
        if scope == self.assistant_context.scope:
            self.assistant_context.remember_market(result)
        return result

    async def fx_convert(self,params):
        value=ConvertParams.model_validate(params)
        try:
            return await convert_reference(value.amount,value.base,value.quote)
        except FXUnavailable as error:
            raise DesktopError("FX_UNAVAILABLE",str(error)) from None

    async def fx_currencies(self, params):
        value = RefreshParams.model_validate(params)
        try:
            return {"currencies": await world_currencies(include_metals=True, refresh=value.refresh)}
        except FXUnavailable as error:
            raise DesktopError("FX_UNAVAILABLE", str(error)) from None

    async def fx_world(self, params):
        value = WorldFxParams.model_validate(params)
        try:
            return await world_rates(
                value.base,
                symbols=value.symbols or None,
                days=value.days,
                compare_quotes=value.compare_quotes or None,
                refresh=value.refresh,
            )
        except FXUnavailable as error:
            raise DesktopError("FX_UNAVAILABLE", str(error)) from None

    async def fx_history(self, params):
        value = PairHistoryParams.model_validate(params)
        try:
            return await pair_history(value.base, value.quote, days=value.days, refresh=value.refresh)
        except FXUnavailable as error:
            raise DesktopError("FX_UNAVAILABLE", str(error)) from None

    async def market_refresh(self,params):
        Params.model_validate(params)
        scope=self.assistant_context.scope
        result=await self.market.refresh(force=True)
        if scope==self.assistant_context.scope:
            self.assistant_context.remember_market(result)
        return result

    async def market_save(self,params):
        value=MarketParams.model_validate(params)
        for name in ("fx_key","news_key"):
            key=getattr(value,name)
            if key and key.get_secret_value(): self.vault.set(name,key.get_secret_value())
        return {"saved":True}

    async def assistant_ask(self,params):
        value=ChatParams.model_validate(params)
        def progress(stage,text=""):
            self.emit("assistant",{"request_id":value.request_id,"stage":stage,"text":text})
        toolbox = AgentToolbox(self.repo, self.vault, service=self, workspace=self.assistant_context)
        async with self.assistant_lock:
            result = await assistant(self.repo,self.vault,value.question,value.request_id,page=value.page,
                                   workspace=self.assistant_context,progress=progress,chat_id=value.chat_id,
                                   toolbox=toolbox)
        self._save_runtime_cache()
        return result

    async def voice_voices(self, params):
        Params.model_validate(params)
        from .voice import list_voices
        return {"voices": await list_voices()}

    async def voice_arm(self, params):
        Params.model_validate(params)
        return {"armed": True}

    async def voice_disarm(self, params):
        Params.model_validate(params)
        for req_id, task in list(self._active_voice_tasks.items()):
            if not task.done():
                task.cancel()
        self._active_voice_tasks.clear()
        return {"armed": False}

    async def voice_speak(self, params):
        value = SpeakParams.model_validate(params)
        from .voice import speak_audio_base64
        audio_b64, mime_type = await speak_audio_base64(value.text, voice_id=value.voice_id, rate=value.rate, vault=self.vault)
        return {"wav_b64": audio_b64, "mime_type": mime_type, "sample_rate": 24000}

    async def voice_turn(self, params):
        value = VoiceTurnParams.model_validate(params)
        if not value.audio_wav_b64 and not (value.text and value.text.strip()):
            raise DesktopError("INVALID_INPUT", "Provide spoken audio or a question for voice/turn.")
        def progress(stage, text=""):
            self.emit("assistant", {"request_id": value.request_id, "stage": stage, "text": text})
        def on_voice(event):
            self.emit("voice", {"request_id": value.request_id, **event})
        toolbox = AgentToolbox(self.repo, self.vault, service=self, workspace=self.assistant_context)
        current_t = asyncio.current_task()
        if current_t:
            self._active_voice_tasks[value.request_id] = current_t
        try:
            async with self.assistant_lock:
                result = await voice_turn(
                    self.repo, self.vault, audio_wav_b64=value.audio_wav_b64, text=value.text,
                    request_id=value.request_id, page=value.page, chat_id=value.chat_id,
                    workspace=self.assistant_context, toolbox=toolbox, progress=progress,
                    speak=value.speak, voice_id=value.voice_id, rate=value.rate, on_voice=on_voice,
                )
        finally:
            self._active_voice_tasks.pop(value.request_id, None)
        audio_b64 = None
        mime_type = None
        if value.speak and result.get("answer") and not result.get("streamed_audio"):
            from .voice import speak_audio_base64
            audio_b64, mime_type = await speak_audio_base64(result["answer"], voice_id=value.voice_id, rate=value.rate, vault=self.vault)
        self._save_runtime_cache()
        return {**result, "wav_b64": audio_b64, "mime_type": mime_type, "sample_rate": 24000}

    async def settings_get(self, params):
        value = RefreshParams.model_validate(params)
        if value.refresh:
            self.repo.invalidate("configuration")
        cfg = await settings(self.repo)
        self._live_model = cfg["live_model"]
        self.runner.agent_name = cfg.get("agent_name") or "Jarvis"
        self.runner.autonomy_mode = cfg.get("autonomy_mode") or "observe"
        self.runner.human_mute_hours = float(cfg.get("human_mute_hours") or 4.0)
        if self._live is not None:
            self._live.remember_model(cfg["live_model"])
        return cfg

    async def settings_save(self, params):
        if not isinstance(params, dict):
            raise DesktopError("INVALID_INPUT", "Settings must be an object.")
        existing = await settings(self.repo)
        merged = dict(existing)
        merged.update(params)
        value = DeskSettings.model_validate(merged)
        if hasattr(self.repo, "set_local_state"):
            self.repo.set_local_state("desktop-settings", value.model_dump())
        else:
            old=await self.repo.get("configuration","desktop-settings")
            await self.repo.put("configuration","desktop-settings",value.model_dump(),old["revision"] if old else 0)
        self._live_model = value.live_model
        self.runner.agent_name = value.agent_name or "Jarvis"
        previous_mode = self.runner.autonomy_mode
        self.runner.autonomy_mode = value.autonomy_mode or "observe"
        if previous_mode != self.runner.autonomy_mode and hasattr(self.repo, "revoke_outbound"):
            self.repo.revoke_outbound()
        self.emit("data-updated", {"collections": ["outbound"]})
        self.runner.human_mute_hours = float(value.human_mute_hours or 4.0)
        if self._live is not None:
            self._live.remember_model(value.live_model)
        if self._cache_path:
            prev = dict(self._cached_bootstrap or {})
            prev["settings"] = value.model_dump()
            self._cached_bootstrap = prev
            self._save_runtime_cache()
        return value.model_dump()

    async def alerts_list(self, params):
        if not isinstance(params, dict):
            params = {}
        refresh = params.get("refresh", False)
        if refresh:
            self.repo.invalidate("tasks")
        limit = int(params.get("limit") or 50)
        offset = int(params.get("offset") or 0)
        state_filter = params.get("state")
        category_filter = params.get("category")

        if hasattr(self.repo, "list_notifications_paginated"):
            result = self.repo.list_notifications_paginated(
                limit=limit,
                offset=offset,
                state=state_filter,
                category=category_filter,
            )
            alerts = result["items"]
            total = result["total"]
            unread_count = result["unread_count"]
        else:
            rows = await self.repo.list("tasks", 300)
            alerts = [r for r in rows if r["data"].get("kind") == "notification"]
            total = len(alerts)
            unread_count = sum(1 for r in alerts if r["data"].get("state") == "NEW")

        # Dynamically refresh truth-based sync status and send evidence
        for row in alerts:
            d = row["data"]
            # Sourcing sheet status check against sheets_outbox
            sourcing_id = d.get("sourcing_task")
            if sourcing_id and hasattr(self.repo, "_conn"):
                with self.repo._lock:
                    out_row = self.repo._conn.execute(
                        "SELECT attempts, last_error FROM sheets_outbox WHERE collection='tasks' AND id=?",
                        (sourcing_id,),
                    ).fetchone()
                    if out_row:
                        if out_row["last_error"]:
                            d["sheet_status"] = "failed"
                            d["sheet_error"] = out_row["last_error"]
                        else:
                            d["sheet_status"] = "pending"
                    elif d.get("sheet_status") == "pending" or d.get("state") == "SAVED_FOR_REVIEW":
                        d["sheet_status"] = "synced"
                        if not d.get("sheet_synced_at"):
                            d["sheet_synced_at"] = time.time()

            # Buyer availability notification status check against outbound_queue
            outbound_id = d.get("availability_outbound_id")
            if outbound_id:
                out_rec = self.repo.get_outbound(outbound_id) if hasattr(self.repo, "get_outbound") else (
                    self.repo.get_sync("outbound", outbound_id)["data"] if hasattr(self.repo, "get_sync") and self.repo.get_sync("outbound", outbound_id) else None
                )
                if out_rec:
                    out_status = out_rec.get("status")
                    if out_status in ("ACCEPTED", "SENT", "DELIVERED", "READ"):
                        d["availability_status"] = "NOTIFIED"
                        d["buyer_notified"] = True
                        if not d.get("buyer_notified_at"):
                            d["buyer_notified_at"] = out_rec.get("updated_at") or time.time()
                    elif out_status == "UNKNOWN":
                        d["availability_status"] = "UNKNOWN"
                        d["buyer_notified"] = False
                    elif out_status in ("FAILED_BEFORE_SEND", "EXPIRED", "CANCELLED"):
                        d["availability_status"] = "FAILED"
                        d["buyer_notified"] = False
                    elif out_status in ("DRAFT", "READY", "CLAIMED", "IN_FLIGHT"):
                        d["availability_status"] = "QUEUED"
                        d["buyer_notified"] = False

        return {
            "alerts": alerts,
            "total": total,
            "unread_count": unread_count,
            "has_more": (offset + len(alerts)) < total,
            "limit": limit,
            "offset": offset,
        }

    async def alert_action(self, params):
        if not isinstance(params, dict):
            raise ValueError("Invalid notification action parameters")
        action = params.get("action")
        rid = params.get("id")

        if action in ("clear_all", "archive_all"):
            count = self.repo.archive_all_notifications() if hasattr(self.repo, "archive_all_notifications") else 0
            self.emit("data-updated", {"collections": ["tasks"]})
            return {"archived_count": count}

        if action == "mark_all_read":
            count = self.repo.mark_all_notifications_read() if hasattr(self.repo, "mark_all_notifications_read") else 0
            self.emit("data-updated", {"collections": ["tasks"]})
            return {"read_count": count}

        if not isinstance(rid, str) or not (rid.startswith("alert-") or rid.startswith("sourcing-")):
            raise ValueError("Invalid notification action ID")
        row = await self.repo.get("tasks", rid)
        if not row or row["data"].get("kind") != "notification":
            raise ValueError("Unknown notification")
        d = dict(row["data"])
        now = time.time()

        if action == "ack":
            d["notified_at"] = now
            d["read_at"] = now
            if d.get("state") == "NEW":
                d["state"] = "READ"
            saved = await self.repo.put("tasks", rid, d, row["revision"])
            self.emit("data-updated", {"collections": ["tasks"]})
            return saved

        elif action == "dismiss":
            d["dismissed_at"] = now
            d["state"] = "DISMISSED"
            saved = await self.repo.put("tasks", rid, d, row["revision"])
            self.emit("data-updated", {"collections": ["tasks"]})
            return saved

        elif action in ("clear", "archive"):
            d["archived_at"] = now
            d["state"] = "ARCHIVED"
            saved = await self.repo.put("tasks", rid, d, row["revision"])
            self.emit("data-updated", {"collections": ["tasks"]})
            return saved

        elif action in ("save_task", "add_to_review_sheet"):
            task_id = "sourcing-" + (rid[6:] if rid.startswith("alert-") else rid)
            existing_task = await self.repo.get("tasks", task_id)
            if existing_task:
                sourcing_data = dict(existing_task["data"])
                task_rev = existing_task["revision"]
            else:
                sourcing_data = {
                    "kind": "sourcing_review",
                    "state": "MANUAL_REVIEW",
                    "opportunity": d.get("details", {}),
                    "created_at": now,
                    "purchase_executed": False,
                    "source_alert_id": rid,
                    "sheet_status": "pending",
                    "sheet_synced_at": None,
                    "sheet_error": None,
                }
                task_rev = 0

            await self.repo.put("tasks", task_id, sourcing_data, task_rev, task_id)

            sheet_status = "pending"
            sheet_error = None
            sheet_synced_at = None
            try:
                if hasattr(self.repo, "flush_outbox"):
                    flush_res = await self.repo.flush_outbox()
                    is_in_outbox = False
                    if hasattr(self.repo, "_conn") and hasattr(self.repo, "_lock"):
                        with self.repo._lock:
                            check_row = self.repo._conn.execute(
                                "SELECT last_error FROM sheets_outbox WHERE collection='tasks' AND id=?",
                                (task_id,),
                            ).fetchone()
                            if check_row:
                                is_in_outbox = True
                                if check_row["last_error"]:
                                    sheet_status = "failed"
                                    sheet_error = check_row["last_error"]
                    if not is_in_outbox:
                        sheet_status = "synced"
                        sheet_synced_at = time.time()
                    elif sheet_status != "failed":
                        sheet_status = "pending"
            except Exception as e:
                sheet_status = "failed"
                sheet_error = str(e)

            sourcing_task_current = await self.repo.get("tasks", task_id)
            if sourcing_task_current:
                updated_sourcing = dict(sourcing_task_current["data"])
                updated_sourcing["sheet_status"] = sheet_status
                updated_sourcing["sheet_synced_at"] = sheet_synced_at
                updated_sourcing["sheet_error"] = sheet_error
                now_iso = datetime.now(timezone.utc).isoformat()
                if hasattr(self.repo, "_upsert_local"):
                    self.repo._upsert_local("tasks", task_id, updated_sourcing, sourcing_task_current["revision"] + 1, now_iso)
                else:
                    await self.repo.put("tasks", task_id, updated_sourcing, sourcing_task_current["revision"])

            d.update(
                state="SAVED_FOR_REVIEW",
                sourcing_task=task_id,
                sheet_status=sheet_status,
                sheet_synced_at=sheet_synced_at,
                sheet_error=sheet_error,
            )
            saved = await self.repo.put("tasks", rid, d, row["revision"])
            if hasattr(self.repo, "record_stage_event"):
                self.repo.record_stage_event(
                    d.get("details", {}).get("buyer_message") or rid,
                    "NOTIFICATION",
                    "SUCCESS",
                    "SAVED_TO_REVIEW_SHEET",
                    attempt_id=f"sheet-{task_id}",
                    metadata={"sheet_status": sheet_status, "task_id": task_id},
                )
            self.emit("data-updated", {"collections": ["tasks"]})
            return saved

        elif action == "send_availability_update":
            if d.get("availability_outbound_id"):
                return row

            details = d.get("details", {})
            buyer_item = details.get("buyer_item") or details.get("buy_item") or {}
            buyer_msg_id = details.get("buyer_message") or buyer_item.get("source_id") or buyer_item.get("message_id")
            buyer_phone = details.get("buyer_phone") or buyer_item.get("phone")
            buyer_jid = details.get("buyer_jid") or buyer_item.get("conversation_id")

            if buyer_msg_id and (not buyer_phone or not buyer_jid) and hasattr(self.repo, "get_sync"):
                bmsg = self.repo.get_sync("messages", buyer_msg_id)
                if bmsg:
                    buyer_phone = buyer_phone or bmsg["data"].get("phone")
                    buyer_jid = buyer_jid or bmsg["data"].get("conversation_id")

            if not buyer_jid and buyer_phone:
                clean_digits = re.sub(r"[^\d]", "", str(buyer_phone))
                buyer_jid = f"{clean_digits}@s.whatsapp.net"

            if not buyer_jid:
                raise ValueError("No buyer destination found for availability update")

            if buyer_msg_id and hasattr(self.repo, "get_sync"):
                bmsg = self.repo.get_sync("messages", buyer_msg_id)
                if bmsg and bmsg["data"].get("outbound_id"):
                    auto_oid = bmsg["data"]["outbound_id"]
                    auto_rec = self.repo.get_outbound(auto_oid) if hasattr(self.repo, "get_outbound") else (
                        self.repo.get_sync("outbound", auto_oid)["data"] if hasattr(self.repo, "get_sync") and self.repo.get_sync("outbound", auto_oid) else None
                    )
                    if auto_rec and auto_rec.get("status") in ("READY", "CLAIMED", "IN_FLIGHT", "ACCEPTED", "SENT"):
                        d["race_prevented"] = True
                        d["availability_status"] = "SKIPPED_AUTO_QUOTED"
                        d["buyer_notified"] = True if auto_rec.get("status") in ("ACCEPTED", "SENT") else False
                        saved = await self.repo.put("tasks", rid, d, row["revision"])
                        self.emit("data-updated", {"collections": ["tasks"]})
                        return saved

            device_name = details.get("device") or buyer_item.get("device") or buyer_item.get("model") or "the requested item"
            text = params.get("custom_text") or f"Update regarding your inquiry for {device_name}: Stock is now available. Let us know if you would like to proceed with your order."

            owner_phone = None
            if hasattr(self.runner, "profile") and isinstance(self.runner.profile, dict):
                owner_phone = self.runner.profile.get("owner_phone") or self.runner.profile.get("phone")
            if not owner_phone and self.configuration:
                owner_phone = self.configuration.get("data", {}).get("owner_phone")
            if not owner_phone and hasattr(self.repo, "get_local_state"):
                prof = self.repo.get_local_state("owner_profile")
                if isinstance(prof, dict):
                    owner_phone = prof.get("owner_phone") or prof.get("phone")

            dedupe_key = f"avail-update:{rid}"
            outbound_id = self.repo.queue_outbound(
                buyer_jid,
                [text],
                kind="availability_update",
                status="READY" if self.runner.autonomy_mode in ("turbo", "draft") else "DRAFT",
                account_phone=owner_phone,
                recipient_phone=buyer_phone,
                dedupe_key=dedupe_key,
            )

            d["availability_outbound_id"] = outbound_id
            d["availability_status"] = "QUEUED"
            d["buyer_notified"] = False
            d["buyer_notified_at"] = None

            saved = await self.repo.put("tasks", rid, d, row["revision"])
            if hasattr(self.repo, "record_stage_event"):
                self.repo.record_stage_event(
                    buyer_msg_id or rid,
                    "NOTIFICATION",
                    "SUCCESS",
                    "AVAILABILITY_UPDATE_QUEUED",
                    attempt_id=f"outbound-{outbound_id}",
                    metadata={"outbound_id": outbound_id, "buyer_jid": buyer_jid},
                )
            self.emit("data-updated", {"collections": ["tasks", "outbound"]})
            return saved

        else:
            raise ValueError(f"Unknown alert action: {action}")

    async def sheet_link(self,params):
        Params.model_validate(params)
        cfg=await settings(self.repo)
        bundle=json.loads(self.vault.get("sheets_connection") or "{}")
        url=cfg["sheet_url"] or bundle.get("workbook_url","")
        if not url:raise DesktopError("SHEET_LINK_REQUIRED","Add the connected spreadsheet URL in Settings once. New Apps Script deployments can discover it automatically.")
        DeskSettings(sheet_url=url)
        return {"url":url}

    async def live_start(self,params):
        page=params.get("page","desk")
        if not isinstance(page,str) or len(page)>40:raise ValueError("Invalid screen")
        voice=params.get("voice")
        if voice is not None and (not isinstance(voice,str) or len(voice)>24):raise ValueError("Invalid voice")
        return await self.live.start(page,voice_name=voice)

    async def live_audio(self,params):
        if not isinstance(params.get("data"),str) or len(params["data"])>18000:raise ValueError("Invalid audio")
        return await self.live.audio(params["data"])

    async def live_text(self,params):
        text=params.get("text")
        if not isinstance(text,str) or not 1<=len(text)<=4000:raise ValueError("Invalid live message")
        return await self.live.text(text)

    async def live_mic_end(self,params):
        Params.model_validate(params)
        return await self.live.mic_end()

    async def live_stop(self,params):
        Params.model_validate(params);return await self.live.stop()

    async def notification_tick(self, *, match=True):
        cfg = await settings(self.repo)
        if match and cfg.get("match_alerts") and self.runner.active:
            await match_opportunities(self.repo)
        if cfg.get("notifications"):
            alerts_res = await self.alerts_list({"state": "NEW", "limit": 10})
            rows = alerts_res.get("alerts", [])
            pending = [
                r for r in rows
                if r["data"].get("state") == "NEW"
                and not r["data"].get("notified_at")
                and r["data"].get("delivery_state") not in ("DELIVERED", "FAILED")
            ]
            for row in pending[:3]:
                d = dict(row["data"])
                now = time.time()
                d["notified_at"] = now
                d["delivery_state"] = "DELIVERED"
                try:
                    await self.repo.put("tasks", row["id"], d, row["revision"])
                except Exception:
                    pass
                self.emit("notification", {"id": row["id"], "title": d["title"], "body": d["body"]})

    async def assistant_history(self,params):
        value=HistoryParams.model_validate(params)
        self.assistant_context.sync_history()
        synced=True
        try:
            await self.assistant_context.history(refresh=value.refresh)
        except (SheetsError,TimeoutError):
            synced=False
        if synced and self.connected:
            self.assistant_context.warm()
            self._save_runtime_cache()
        return {"turns":self.assistant_context.local_turns(),"synced":synced,
                "pending_ids":list(self.assistant_context.pending)}

    async def outbound_list(self, params):
        val = OutboundListParams.model_validate(params)
        if not hasattr(self.repo, "list_outbound"):
            return {"items": [], "scheduled": []}
        if hasattr(self.repo, "ready_outbound_ids"):
            self.repo.ready_outbound_ids((self.runner.cached_setup or {}).get("owner_phone"))
        query_statuses = tuple(val.statuses) if val.statuses else (
            "DRAFT", "NEEDS_PRICE_REVIEW", "READY", "CLAIMED", "IN_FLIGHT", "ACCEPTED", "UNKNOWN", "FAILED_BEFORE_SEND", "EXPIRED", "CANCELLED"
        )
        limit = val.limit or 50
        items = self.repo.list_outbound(query_statuses, limit=limit, newest_first=True)
        scheduled = self.repo.list_scheduled_jobs("PENDING", limit=50) if hasattr(self.repo, "list_scheduled_jobs") else []
        return {"items": items, "scheduled": scheduled, "can_send": self.runner.can_send,
                "autonomy_mode": self.runner.autonomy_mode}

    def _outbound_scope(self):
        setup = self.runner.cached_setup or {}
        return setup.get("owner_phone"), {source["phone"] for source in setup.get("sources", []) if source.get("active", True)}

    async def outbound_create(self, params):
        value = OutboundDraftParams.model_validate(params)
        if not hasattr(self.repo, "queue_outbound"):
            raise DesktopError("UNSUPPORTED", "The WhatsApp outbox requires local workspace storage.")
        phone = None
        if value.recipient_phone:
            try:
                phone = Source.normalize(value.recipient_phone)
            except Exception:
                phone = value.recipient_phone
        setup = self.runner.cached_setup or {}
        owner = setup.get("owner_phone")
        if not owner:
            raise DesktopError("SETUP_REQUIRED", "Configure your verified WhatsApp phone number in Setup.")
        if phone and phone == owner:
            raise DesktopError("SELF_SEND_BLOCKED", "You cannot send outbound messages to your own workspace account.")
        recipient_jid = value.conversation_id if (value.conversation_id and "@" in value.conversation_id) else ((phone[1:] + "@c.us") if phone else None)
        if not recipient_jid:
            raise DesktopError("INVALID_RECIPIENT", "Valid recipient phone or conversation ID is required.")
        if hasattr(self.repo, "get_contact"):
            cprof = self.repo.get_contact(recipient_jid)
            if cprof and cprof.get("category") in ("FAMILY_PERSONAL", "IGNORED"):
                raise DesktopError("RECIPIENT_BLOCKED", "Cannot send messages to personal/family or ignored contacts.")
        dedupe_key = f"manual:{owner}:{recipient_jid}:{value.client_request_id}" if value.client_request_id else None
        expected_parts = [value.text] if value.text else []
        if dedupe_key and hasattr(self.repo, "get_outbound"):
            import uuid
            prospective_oid = "out-" + uuid.uuid5(uuid.NAMESPACE_OID, dedupe_key).hex[:16]
            existing_item = self.repo.get_outbound(prospective_oid)
            if existing_item:
                existing_parts = existing_item.get("text_parts") or []
                existing_att = existing_item.get("attachment_path")
                if existing_parts != expected_parts or existing_att != value.attachment_path:
                    raise DesktopError("CLIENT_REQUEST_CONFLICT", "This request ID was already used for different message content.")
                status = existing_item["status"]
                if status in ("READY", "CLAIMED", "IN_FLIGHT", "ACCEPTED"):
                    return {
                        "id": prospective_oid,
                        "approved": True,
                        "status": status,
                        "reason": None,
                        "message": "Message queued for delivery." if status == "READY" else f"Message is {status}.",
                    }
        att_meta = {
            k: v for k, v in {
                "attachment_name": value.attachment_name,
                "attachment_mime": value.attachment_mime,
                "attachment_type": value.attachment_type,
                "attachment_size": value.attachment_size,
            }.items() if v is not None
        }
        oid = self.repo.queue_outbound(
            recipient_jid,
            expected_parts,
            kind="manual",
            recipient_phone=phone,
            account_phone=owner,
            attachment_path=value.attachment_path,
            caption=value.caption,
            meta=att_meta,
            dedupe_key=dedupe_key,
        )
        approved = False
        reason = None
        if value.approve_now:
            if not self.runner.active:
                reason = "WORKSPACE_STOPPED"
            elif not self.runner.can_send:
                reason = "DISPATCH_DISABLED"
            else:
                existing_item = self.repo.get_outbound(oid) if hasattr(self.repo, "get_outbound") else None
                if existing_item and existing_item["status"] in ("READY", "CLAIMED", "IN_FLIGHT", "ACCEPTED"):
                    approved = True
                else:
                    reason = self.repo.approve_outbound(oid, None, owner, allowed_phones=None, manual_only=False)
                    if reason == "RECIPIENT_MUTED":
                        # Human composer follow-up is explicitly permitted by handoff contract
                        import time as _t
                        now = _t.time()
                        with self.repo._lock, self.repo._conn:
                            self.repo._conn.execute(
                                "INSERT OR REPLACE INTO outbound_dispatch(id, account_phone, expires_at) VALUES(?, ?, ?)",
                                (oid, owner, now + 120),
                            )
                            self.repo._conn.execute("UPDATE outbound_queue SET status='READY', updated_at=? WHERE id=?", (now, oid))
                        reason = None
                    approved = (reason is None)
        curr_item = self.repo.get_outbound(oid) if hasattr(self.repo, "get_outbound") else None
        current_status = curr_item["status"] if curr_item else ("READY" if approved else "DRAFT")
        self.emit("data-updated", {"collections": ["outbound"]})
        return {
            "id": oid,
            "approved": approved or (current_status in ("READY", "CLAIMED", "IN_FLIGHT", "ACCEPTED")),
            "status": current_status,
            "reason": reason if not approved else None,
            "message": "Message queued for delivery." if current_status == "READY" else (
                "Message saved as draft because the workspace is stopped. Click Start to enable sending." if reason == "WORKSPACE_STOPPED" else
                (f"Message saved as draft: {reason}." if reason else ("Message queued for delivery." if approved else "Message saved as draft."))
            ),
        }

    async def outbound_action(self, params):
        value = OutboundActionParams.model_validate(params)
        if not hasattr(self.repo, "approve_outbound"):
            raise DesktopError("UNSUPPORTED", "Local outbound queue is unavailable in this mode.")
        if value.action == "approve":
            if not self.runner.can_send:
                raise DesktopError("DISPATCH_DISABLED", "Start the workspace in supervised mode before approving a draft.")
            setup = self.runner.cached_setup or {}
            owner = setup.get("owner_phone")
            if not owner:
                raise DesktopError("SETUP_REQUIRED", "Configure your verified WhatsApp phone number in Setup.")
            reason = self.repo.approve_outbound(
                value.id,
                value.expected_updated_at,
                owner,
                allowed_phones=None,
                manual_only=self.runner.autonomy_mode == "send",
            )
            if reason:
                draft_row = self.repo.get_outbound(value.id) if hasattr(self.repo, "get_outbound") else None
                recip_phone = draft_row.get("recipient_phone") if draft_row else ""

                def _digits(p):
                    return re.sub(r"\D", "", str(p or ""))

                if reason in ("SELF_SEND_BLOCKED", "RECIPIENT_NOT_ALLOWED"):
                    if recip_phone and _digits(recip_phone) == _digits(owner):
                        msg = f"Cannot send automated quotes to your own registered phone number ({owner}). WhatsApp will drop or echo self-sent messages. Please test with a client/buyer contact number."
                    elif not recip_phone:
                        msg = "Recipient phone number is missing. Verify the contact has a valid WhatsApp phone number."
                    else:
                        msg = f"Recipient {recip_phone} is not permitted. Verify the contact's phone number format or check allowed sources in Setup."
                elif reason == "RECIPIENT_MUTED":
                    msg = "This chat is temporarily muted because staff recently messaged them directly on WhatsApp. Mute will expire shortly, or you can clear it."
                elif reason == "RECIPIENT_BLOCKED":
                    msg = "This contact is marked as Family/Personal or Ignored. Automated trading messages are blocked for this contact."
                elif reason == "DRAFT_CHANGED":
                    msg = "Draft was modified, sent, or dismissed by another process. Please refresh the alerts list."
                elif reason == "MANUAL_SEND_ONLY":
                    msg = "Autonomy is currently in 'Manual Send Only' mode. Switch autonomy mode to Send or Draft in Setup to approve."
                elif reason == "QUOTE_REQUIRES_REVIEW":
                    msg = "Quote pricing expired (older than 10 mins). Please review pricing and click Approve to confirm."
                elif reason == "UNSUPPORTED_MESSAGE":
                    msg = "Draft text is empty, exceeds 3,000 characters, or contains unsupported media attachments."
                else:
                    msg = f"Draft approval failed ({reason}). Please check recipient, contents, and status."
                raise DesktopError(reason, msg)
        elif not self.repo.dismiss_outbound(value.id):
            raise DesktopError("DRAFT_CHANGED", "The message is already in flight or closed. Refresh its status.")
        self.emit("data-updated", {"collections": ["outbound"]})
        return await self.outbound_list({})

    async def outbound_dispatch(self, params):
        if not hasattr(self.repo, "dispatch_outbound"):
            raise DesktopError("UNSUPPORTED", "Outbound dispatch is not supported.")
        operation = params.get("operation")
        if operation not in ("dispatch_claim", "dispatch_authorize", "dispatch_result"):
            raise DesktopError("INVALID_INPUT", "Invalid dispatch operation.")
        setup = self.runner.cached_setup or {}
        owner = setup.get("owner_phone")
        if not owner and params.get("account_phone"):
            owner = params.get("account_phone")
        allowed = {s["phone"] for s in setup.get("sources", []) if s.get("active", True)}
        manual_only = (self.runner.autonomy_mode == "send")
        result = self.repo.dispatch_outbound(
            operation, params, owner, allowed, self.runner.can_send, manual_only=manual_only
        )
        if self.runner.on_change:
            self.runner.on_change("outbound")
        self.emit("data-updated", {"collections": ["outbound"]})
        return result

    async def contacts_list(self, params):
        Params.model_validate(params)
        if not hasattr(self.repo, "list_contacts"):
            return {"contacts": []}
        return {"contacts": self.repo.list_contacts()}

    async def contacts_tag(self, params):
        value = ContactTagParams.model_validate(params)
        if not hasattr(self.repo, "upsert_contact"):
            raise DesktopError("UNSUPPORTED", "Contact categorization requires local store.")
        self.repo.upsert_contact(value.conversation_id, category=value.category)
        return {"ok": True, "conversation_id": value.conversation_id, "category": value.category}

    async def contacts_unmute(self, params):
        value = ContactUnmuteParams.model_validate(params)
        if hasattr(self.repo, "unmute_chat"):
            self.repo.unmute_chat(value.conversation_id)
        self.emit("data-updated", {"collections": ["contacts", "outbound"]})
        return {"ok": True, "conversation_id": value.conversation_id}

    async def contacts_mute_status(self, params):
        value = ContactUnmuteParams.model_validate(params)
        if hasattr(self.repo, "get_chat_mute_status"):
            st = self.repo.get_chat_mute_status(value.conversation_id)
            return {"conversation_id": value.conversation_id, **st}
        return {"conversation_id": value.conversation_id, "muted": False, "remaining_seconds": 0.0}

    async def scheduler_list(self, params):
        Params.model_validate(params)
        if not hasattr(self.repo, "list_scheduled_jobs"):
            return {"jobs": []}
        return {"jobs": self.repo.list_scheduled_jobs(None, limit=50)}

    async def run_scheduler_tick(self):
        if not self.runner.active or self.runner.autonomy_mode == "send" or not hasattr(self.repo, "due_scheduled_jobs"):
            return 0
        due = self.repo.due_scheduled_jobs()
        if not due:
            return 0
        cfg = await settings(self.repo)
        news_rec = await self.repo.get("news", "latest")
        aa_pct = float((news_rec or {}).get("data", {}).get("aa_pct") or 0.0)
        processed = 0
        for job in due:
            cid = job["recipient_jid"]
            if hasattr(self.repo, "is_chat_muted") and self.repo.is_chat_muted(cid):
                self.repo.update_scheduled_job(job["id"], "CANCELLED_HUMAN_ACTIVE")
                continue
            payload = job.get("payload") or {}
            match = payload.get("match") or {}
            snapshot_revision = match.get("snapshot_revision")
            if snapshot_revision is not None:
                snapshot = await self.repo.get("configuration", "daily-stock")
                if (not snapshot or snapshot["revision"] != snapshot_revision
                        or not 0 <= time.time() - snapshot["data"]["source_timestamp"] < 86400):
                    self.repo.update_scheduled_job(job["id"], "NEEDS_REVIEW", "STOCK_SNAPSHOT_CHANGED")
                    processed += 1
                    continue
            text_parts = payload.get("text_parts") or []
            pricing = None
            repricing_failed = False
            total_usd = 0.0
            evaluations = payload.get("evaluations")

            from . import trading_engine as _engine

            if job.get("recalculate_fx"):
                if evaluations and isinstance(evaluations, list) and len(evaluations) > 1:
                    re_evals = []
                    for ev in evaluations:
                        b_item = ev.get("buy_item") or {}
                        m_item = (ev.get("match") or {}).get("item") or {}
                        cost = m_item.get("unit_cost")
                        curr = m_item.get("currency") or "INR"
                        p_res = None
                        if cost:
                            try:
                                p_res = await _engine.calculate_dynamic_quote(
                                    cost,
                                    curr,
                                    job.get("buyer_phone"),
                                    margin_pct=cfg.get("default_margin_pct", 3.5),
                                    aa_pct=aa_pct,
                                    explicit_currency=payload.get("explicit_currency"),
                                )
                                q_usd = float(p_res.get("quote_usd") or 0.0)
                                q_qty = int(b_item.get("quantity") or 1)
                                total_usd += q_usd * q_qty
                            except Exception:
                                repricing_failed = True
                        else:
                            repricing_failed = True
                        re_evals.append({"buy_item": b_item, "match": ev.get("match"), "pricing": p_res})
                    if not repricing_failed:
                        text_parts = _engine.compose_multi_item_quote(re_evals)
                elif payload.get("match") and payload.get("buy_item"):
                    stock = payload["match"].get("item") or {}
                    if stock.get("unit_cost") and stock.get("currency"):
                        try:
                            pricing = await _engine.calculate_dynamic_quote(
                                stock["unit_cost"],
                                stock.get("currency") or "INR",
                                job.get("buyer_phone"),
                                margin_pct=cfg.get("default_margin_pct", 3.5),
                                aa_pct=aa_pct,
                                explicit_currency=payload.get("explicit_currency"),
                            )
                            text_parts = _engine.compose_dealer_quote(payload["buy_item"], payload["match"], pricing)
                            item_qty = int(payload.get("buy_item", {}).get("quantity") or 1)
                            total_usd = float((pricing or {}).get("quote_usd") or 0.0) * item_qty
                        except Exception:
                            pricing = None
                            repricing_failed = True

            if text_parts and hasattr(self.repo, "queue_outbound"):
                status = "DRAFT"
                if self.runner.autonomy_mode in ("auto", "approved_auto", "turbo") and not repricing_failed:
                    max_usd = float(cfg.get("auto_reply_max_usd", 5000.0))
                    if 0.0 < total_usd <= max_usd:
                        status = "READY"
                owner_phone = (self.runner.cached_setup or {}).get("owner_phone")
                if not owner_phone and self.configuration:
                    owner_phone = self.configuration.get("data", {}).get("owner_phone")
                self.repo.queue_outbound(
                    cid,
                    text_parts,
                    kind="scheduled_quote",
                    recipient_phone=job.get("buyer_phone"),
                    account_phone=owner_phone,
                    status=status,
                    meta={
                        "scheduled_job_id": job["id"],
                        "aa_pct": aa_pct,
                        "pricing": pricing,
                        "total_usd": total_usd,
                        "snapshot_revision": snapshot_revision,
                        **({"needs_price_review": True, "reason": "REPRICING_FAILED"} if repricing_failed else {}),
                    },
                    dedupe_key=job["id"],
                )
            elif repricing_failed:
                self.repo.update_scheduled_job(job["id"], "NEEDS_REVIEW", "REPRICING_FAILED")
                processed += 1
                continue
            self.repo.update_scheduled_job(job["id"], "QUEUED")
            processed += 1
        if processed:
            self.emit("data-updated", {"collections": ["outbound", "alerts"]})
        return processed

    async def diagnostics_logs(self, params):
        Params.model_validate(params)
        try:
            return self.diagnostics.snapshot()
        except (OSError, sqlite3.Error):
            raise DesktopError("LOG_UNAVAILABLE", "Diagnostic logs could not be read.") from None

    async def diagnostics_clear(self, params):
        Params.model_validate(params)
        try:
            self.diagnostics.clear()
        except (OSError, sqlite3.Error):
            raise DesktopError("LOG_UNAVAILABLE", "Diagnostic logs could not be cleared.") from None
        return {"cleared": True}

    async def dispatch(self, raw):
        started = time.monotonic()
        response = await self._dispatch(raw)
        method = raw.get("method") if isinstance(raw, dict) else None
        if isinstance(method, str) and method != "wa/sync":
            error = response.get("error")
            self.diagnostics.record(method, level="warning" if error else "info",
                                    code=error.get("code") if error else None,
                                    duration_ms=round((time.monotonic() - started) * 1000))
        return response

    async def _dispatch(self, raw):
        request_id = None
        try:
            request = Request.model_validate(raw)
            request_id = request.id
            routes = {"status": self.status, "bootstrap": self.bootstrap, "diagnostic": self.diagnostic, "connect": self.connect, "setup/save": self.save_setup, "start": self.start, "stop": self.stop, "activity": self.activity, "provider/save": self.save_provider,"provider/test":self.test_provider,"dashboard":self.dashboard,"inventory/save":self.inventory_save,"market/snapshot":self.market_snapshot,"market/refresh":self.market_refresh,"market/save":self.market_save,"assistant/ask":self.assistant_ask,"assistant/history":self.assistant_history}
            routes.update({"settings/get":self.settings_get,"settings/save":self.settings_save,"alerts/list":self.alerts_list,"alerts/action":self.alert_action,"sheet/link":self.sheet_link})
            routes.update({"live/start":self.live_start,"live/audio":self.live_audio,"live/text":self.live_text,"live/stop":self.live_stop,"live/mic-end":self.live_mic_end})
            routes.update({"voice/voices": self.voice_voices, "voice/arm": self.voice_arm, "voice/disarm": self.voice_disarm, "voice/speak": self.voice_speak, "voice/turn": self.voice_turn})
            routes["inventory/bulk"]=self.inventory_bulk
            routes["inventory/parse"]=self.inventory_parse
            routes.update({"diagnostics/logs": self.diagnostics_logs, "diagnostics/clear": self.diagnostics_clear})
            routes.update({"fx/convert":self.fx_convert,"fx/currencies":self.fx_currencies,"fx/world":self.fx_world,"fx/history":self.fx_history})
            routes.update({"provider/reveal":self.reveal_provider_key,"provider/discover":self.discover_provider,"profile/sync":self.sync_profile,"sheet/prepare":self.prepare_sheet,"bridge/open":self.open_browser,"extension/pair":self.extension_pair,"bridge/pair":self.extension_pair,"wa/sync":self.wa_sync,"wa/scan-mode":self.wa_scan_mode})
            routes.update({"outbound/list":self.outbound_list,"outbound/create":self.outbound_create,"outbound/action":self.outbound_action,"outbound/dispatch":self.outbound_dispatch,"contacts/list":self.contacts_list,"contacts/tag":self.contacts_tag,"contacts/unmute":self.contacts_unmute,"contacts/mute-status":self.contacts_mute_status,"scheduler/list":self.scheduler_list})
            routes["inventory/daily-stock"] = self.daily_stock_status
            routes["message/trace"] = self.message_trace
            if request.method not in routes:
                raise DesktopError("UNKNOWN_METHOD", "This action is not available.")
            if request.method not in ("voice/turn", "inventory/parse") and len(json.dumps(request.params, ensure_ascii=False).encode("utf-8")) > MAX_REQUEST:
                raise DesktopError("INVALID_INPUT", "Desktop request is too large.")
            return {"id": request_id, "result": await routes[request.method](request.params)}
        except DesktopError as error:
            return {"id": request_id, "error": {"code": error.code, "message": error.message}}
        except GeminiError as error:
            return {"id":request_id,"error":{"code":error.code,"message":str(error)}}
        except ValidationError as error:
            fields=", ".join(".".join(str(p) for p in item["loc"]) for item in error.errors()[:4])
            return {"id":request_id,"error":{"code":"INVALID_INPUT","message":"Check these fields: "+fields}}
        except (ValueError, TypeError, KeyError):
            return {"id": request_id, "error": {"code": "INVALID_INPUT", "message": "Check the entered values and try again."}}
        except SheetsError as error:
            safe_codes = {"REVISION_CONFLICT", "SHEETS_UNAVAILABLE", "SHEETS_NOT_CONFIGURED", "RESPONSE_NOT_VERIFIED"}
            code = error.code if error.code in safe_codes else "SHEETS_CONNECTION_ERROR"
            return {"id": request_id, "error": {"code": code, "message": "The Sheet operation could not be verified. Retry connection checks."}}
        except Exception:
            return {"id": request_id, "error": {"code": "ACTION_FAILED", "message": "The action could not finish. Retry connection checks."}}

    def close(self):
        for task in self._agent_command_tasks:
            task.cancel()
        if self._bg_diag_task and not self._bg_diag_task.done():
            self._bg_diag_task.cancel()
        if self._warm_task and not self._warm_task.done():
            self._warm_task.cancel()
        self.runner.active = False
        self._save_runtime_cache()
        self.runner.bridge.stop()
        self.repo.close()
        self.diagnostics.close()


async def serve():
    service = DesktopService(cache_path=_default_desktop_cache_path())
    service.diagnostics.record("engine/start")
    async def market_loop():
        while True:
            wait=60
            if not service.connected:
                if not service.initializing and not service.diagnostic_lock.locked() and time.time()-(service.checked_at or 0)>=30:
                    try:
                        if (json.loads(service.vault.get("sheets_connection") or "{}")).get("url"):
                            await service.diagnostic({})
                            service.emit("refresh",{})
                    except (ValueError, RuntimeError):
                        service.diagnostics.record("worker/connections", level="warning", code="ACTION_FAILED")
                await asyncio.sleep(5)
                continue
            service.assistant_context.sync_history()
            try:
                cfg=await service.settings_get({});wait=cfg["fx_poll_seconds"]
                if service.connected and cfg["market_enabled"]:
                    await service.market.refresh()
                    service.emit("data-updated", {"collections": ["fx", "news"]})
            except Exception:
                service.diagnostics.record("worker/market", level="warning", code="ACTION_FAILED")
            await asyncio.sleep(wait)
    async def classifier_loop():
        backoff = 2.0
        while True:
            try:
                processed = await service.runner.classify_pending()
                if processed:
                    backoff = 1.0
                else:
                    backoff = min(backoff * 1.5, 6.0)
            except Exception:
                service.diagnostics.record("worker/classifier", level="warning", code="ACTION_FAILED")
                backoff = min(backoff * 2.0, 15.0)
            await asyncio.sleep(backoff)
    async def notification_loop():
        last_match = 0.0
        while True:
            try:
                bridge = service.runner.bridge
                if getattr(bridge, "ready", False) and hasattr(bridge, "ensure_running") and not bridge.alive:
                    if bridge.ensure_running():
                        service.diagnostics.record("bridge/restarted", level="warning", code="BRIDGE_RESTARTED")
                elif not getattr(bridge, "ready", False) and service.vault.get("bridge_secret") and service.bridge_error is None:
                    service.start_bridge()
            except Exception:
                service.diagnostics.record("worker/notifications", level="warning", code="ACTION_FAILED")
            if service.runner.active:
                try:
                    now = time.time()
                    should_match = (now - last_match) >= 30.0
                    await service.notification_tick(match=should_match)
                    if should_match:
                        last_match = now
                except Exception:
                    service.diagnostics.record("worker/notifications", level="warning", code="ACTION_FAILED")
            await asyncio.sleep(5)
    async def sheets_sync_loop():
        pull_tick = 0
        while True:
            try:
                if service.connected and hasattr(service.repo, "flush_outbox"):
                    outcome = await service.repo.flush_outbox()
                    if outcome.get("error"):
                        service.diagnostics.record("worker/sheets", level="warning", code=outcome["error"])
                pull_tick += 1
                if pull_tick >= 15 and service.connected and hasattr(service.repo, "hydrate_from_remote"):
                    pull_tick = 0
                    try:
                        await service.repo.hydrate_from_remote(("inventory", "offers"))
                    except Exception:
                        pass
            except Exception:
                service.diagnostics.record("worker/sheets", level="warning", code="ACTION_FAILED")
            await asyncio.sleep(4)
    async def scheduler_loop():
        while True:
            try:
                await service.run_scheduler_tick()
            except Exception:
                service.diagnostics.record("worker/scheduler", level="warning", code="ACTION_FAILED")
            await asyncio.sleep(15)
    market_task=classifier_task=initial_task=sync_task=sched_task=notify_task=None
    requests=set()
    async def respond(raw):
        response=await service.dispatch(raw)
        sys.stdout.buffer.write((json.dumps(response,ensure_ascii=False,allow_nan=False)+"\n").encode("utf-8"));sys.stdout.buffer.flush()
    def emit(event,data):
        sys.stdout.buffer.write((json.dumps({"event":event,"data":data},ensure_ascii=False,allow_nan=False)+"\n").encode("utf-8"));sys.stdout.buffer.flush()
    service.emit=emit
    try:
        initial_task=asyncio.create_task(service.initialize())
        market_task=asyncio.create_task(market_loop())
        classifier_task=asyncio.create_task(classifier_loop())
        notify_task=asyncio.create_task(notification_loop())
        sync_task=asyncio.create_task(sheets_sync_loop())
        sched_task=asyncio.create_task(scheduler_loop())
        while True:
            line = await asyncio.to_thread(sys.stdin.buffer.readline, MAX_AUDIO_REQUEST + 1)
            if not line:
                await asyncio.gather(*requests,return_exceptions=True)
                break
            if len(line) > MAX_AUDIO_REQUEST:
                response = {"id": None, "error": {"code": "REQUEST_TOO_LARGE", "message": "Request payload exceeded size limit."}}
                sys.stdout.buffer.write((json.dumps(response)+"\n").encode());sys.stdout.buffer.flush()
                continue
            try:
                raw = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                response = {"id": None, "error": {"code": "INVALID_REQUEST", "message": "Invalid desktop request."}}
                sys.stdout.buffer.write((json.dumps(response)+"\n").encode());sys.stdout.buffer.flush()
                continue
            if len(requests)>=24:
                sys.stdout.buffer.write((json.dumps({"id":raw.get("id") if isinstance(raw,dict) else None,"error":{"code":"DESKTOP_BUSY","message":"The workspace is busy. Retry shortly."}})+"\n").encode());sys.stdout.buffer.flush();continue
            task=asyncio.create_task(respond(raw));requests.add(task);task.add_done_callback(requests.discard)
    finally:
        if initial_task and not initial_task.done():
            initial_task.cancel()
            await asyncio.gather(initial_task, return_exceptions=True)
        if sync_task and not sync_task.done():
            sync_task.cancel()
            await asyncio.gather(sync_task, return_exceptions=True)
        if sched_task and not sched_task.done():
            sched_task.cancel()
            await asyncio.gather(sched_task, return_exceptions=True)
        if service._warm_task and not service._warm_task.done():
            service._warm_task.cancel()
            await asyncio.gather(service._warm_task, return_exceptions=True)
        for task in requests:task.cancel()
        await asyncio.gather(*requests,return_exceptions=True)
        if service._live is not None:
            await service._live.stop()
        service.assistant_context.clear()
        if market_task:
            market_task.cancel()
            await asyncio.gather(market_task,return_exceptions=True)
        if classifier_task:
            classifier_task.cancel()
            await asyncio.gather(classifier_task,return_exceptions=True)
        if notify_task:
            notify_task.cancel()
            await asyncio.gather(notify_task, return_exceptions=True)
        service.diagnostics.record("engine/stop")
        service.close()


def main():
    asyncio.run(serve())


if __name__ == "__main__":
    main()
