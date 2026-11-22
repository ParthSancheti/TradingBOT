import asyncio
import base64
import time
import re
from .desktop import LocalBridge, open_whatsapp, download_whatsapp_avatar, save_owner_avatar_bytes, runtime_directory
from .pipeline import IntakePipeline
from .gemini import GeminiClient
from .onboarding import BusinessSetup
from .intelligence import match_opportunities, settings as load_desk_settings
from .trading_engine import evaluate_inbound_trade, handle_agent_command

_router = None


def __getattr__(name):
    global _router
    if name != "router":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    if _router is not None:
        return _router
    from fastapi import APIRouter, HTTPException, Request
    from .security import require_admin, require_write

    r = APIRouter()

    @r.post("/api/runner/start")
    async def start(request: Request):
        require_write(request, fresh=True)
        if not await request.app.state.business.repository.get("configuration", "business"):
            raise HTTPException(409, "Complete onboarding first")
        try:
            request.app.state.runner.bridge.start()
            open_whatsapp()
            request.app.state.runner.begin_session()
            request.app.state.runner.active = True
        except Exception as error:
            request.app.state.runner.bridge.stop()
            raise HTTPException(409, str(error) if isinstance(error, RuntimeError) else "Local bridge could not start") from None
        return request.app.state.runner.status()

    @r.post("/api/runner/stop")
    async def stop(request: Request):
        require_write(request)
        request.app.state.runner.end_session()
        return request.app.state.runner.status()

    @r.get("/api/runner")
    async def status(request: Request):
        require_admin(request)
        return request.app.state.runner.status()

    _router = r
    return _router


class Runner:
    def __init__(self, vault, repository, on_change=None):
        self.vault, self.repository = vault, repository
        self.on_change = on_change
        self.bridge = LocalBridge(vault, self.handle)
        self.observations = 0
        self.active=False;self.last_error=None;self.last_batch_at=None;self.coverage=None;self.extension_last_seen=0
        self.profile_request_until = 0
        self.open_request_until = 0
        self.config_version = 1
        self.profile = None
        self.agent_name = "Jarvis"
        self.autonomy_mode = "observe"
        self.human_mute_hours = 4.0
        self.direct_wa_status = None
        self.scan_mode = "all"
        self.immutable_floor_ts = None
        self.scan_since_ts = None
        self.last_stopped_at = None
        self.last_event_ts = None
        self.per_chat_cursors = {}
        self.cached_setup = None
        self.last_probe = None
        self._seen_agent_msgs = set()
        self.on_agent_command = None
        self.service_loop = None
        self._last_bridge_sig = None

    @property
    def can_send(self):
        return bool(self.active and self.autonomy_mode in ("draft", "send", "turbo") and hasattr(self.repository, "dispatch_outbound"))

    def _notify_bridge_state(self):
        act = getattr(self, "extension_activity", None) or {}
        fresh = bool(self.bridge.ready and 0 <= time.time() - self.extension_last_seen < 180)
        sig = (fresh, bool(self.active), act.get("phase"), act.get("detail"), self.observations)
        if sig == self._last_bridge_sig:
            return
        self._last_bridge_sig = sig
        if self.on_change:
            try:
                self.on_change("bridge")
            except Exception:
                pass

    def begin_session(self, now=None, mode=None):
        ts_now = float(now if now is not None else time.time())
        if mode == "new_only":
            self.scan_mode = "new_only"
            self.immutable_floor_ts = ts_now
            self.scan_since_ts = ts_now
            return
        elif mode == "all":
            self.scan_mode = "all"
            self.immutable_floor_ts = None
            self.scan_since_ts = None
            return

        # If mode was not explicitly supplied, preserve scan_mode invariants:
        if self.scan_mode == "new_only" and self.immutable_floor_ts is not None:
            # Floor is immutable: reconnect or resume must NEVER widen before immutable_floor_ts.
            if self.last_event_ts is not None:
                self.scan_since_ts = max(self.immutable_floor_ts, float(self.last_event_ts) - 300.0)
            else:
                self.scan_since_ts = self.immutable_floor_ts
        elif now is not None:
            if self.last_event_ts is not None:
                self.scan_since_ts = max(0.0, min(ts_now - 5.0, float(self.last_event_ts) - 300.0))
            else:
                self.scan_since_ts = max(0.0, ts_now - 5.0)
        else:
            if self.scan_since_ts is None:
                if self.last_event_ts is not None:
                    self.scan_since_ts = max(0.0, float(self.last_event_ts) - 300.0)
                else:
                    self.scan_since_ts = max(0.0, ts_now - 86400.0)
            elif self.last_event_ts is not None:
                self.scan_since_ts = max(0.0, min(float(self.scan_since_ts), float(self.last_event_ts) - 300.0))

    def end_session(self, now=None):
        self.active = False
        ts_now = float(now if now is not None else time.time())
        self.last_stopped_at = ts_now
        if self.scan_mode == "new_only" and self.immutable_floor_ts is not None:
            if self.last_event_ts is not None:
                self.scan_since_ts = max(self.immutable_floor_ts, float(self.last_event_ts) - 300.0)
            else:
                self.scan_since_ts = self.immutable_floor_ts
        elif self.last_event_ts is not None:
            self.scan_since_ts = max(0.0, float(self.last_event_ts) - 300.0)

    def reset_scan_cursor(self):
        self.scan_mode = "all"
        self.immutable_floor_ts = None
        self.scan_since_ts = None
        self.last_stopped_at = None
        self.last_event_ts = None
        self.cached_setup = None
        self._last_bridge_sig = None
        self.per_chat_cursors = {}

    def accept_event_ts(self, event):
        """Check whether an incoming WhatsApp event is acceptable given the active session cutoff.

        Precision: Unix epoch seconds (floating point or integer).
        Boundary: Inclusive (timestamp >= scan_since_ts).
        Uncertainty policy: When an immutable new-only floor is active, missing, non-numeric
        or non-positive timestamps are rejected, preventing historical records from failing open.
        Unread bypasses are explicitly disallowed: an old unread message is still old.
        """
        if not isinstance(event, dict):
            return False, "INVALID_EVENT"
        
        # When an immutable new-only floor is active, every event must have a verified
        # timestamp >= immutable_floor_ts. Uncertain or missing timestamps are rejected.
        if self.immutable_floor_ts is not None:
            raw_ts = event.get("timestamp")
            if raw_ts is None or not isinstance(raw_ts, (int, float)) or raw_ts <= 0:
                return False, "UNCERTAIN_TIMESTAMP"
            ts = float(raw_ts) / 1000.0 if float(raw_ts) > 1e12 else float(raw_ts)
            if ts < float(self.immutable_floor_ts):
                return False, "IGNORED_OLD"
            if self.scan_since_ts is not None and ts < float(self.scan_since_ts):
                return False, "IGNORED_OLD"
            return True, None

        if self.scan_since_ts is None:
            return True, None

        raw_ts = event.get("timestamp")
        if isinstance(raw_ts, (int, float)) and raw_ts > 0:
            ts = float(raw_ts) / 1000.0 if float(raw_ts) > 1e12 else float(raw_ts)
            if ts < float(self.scan_since_ts):
                return False, "IGNORED_OLD"
        return True, None

    def has_synced_profile(self, owner_phone=None):
        prof = getattr(self, "profile", None)
        if not isinstance(prof, dict) or not str(prof.get("name") or "").strip():
            return False
        if owner_phone:
            o_digits = re.sub(r"\D", "", str(owner_phone))
            p_digits = re.sub(r"\D", "", str(prof.get("owner_phone") or prof.get("phone") or ""))
            if o_digits and p_digits and o_digits != p_digits:
                return False
        return bool(prof.get("avatar") or prof.get("avatar_checked"))

    async def _store_profile(self, profile, owner_phone=None):
        if not isinstance(profile, dict) or profile.get("verified") is not True:
            return False
        raw_name = profile.get("name")
        if not isinstance(raw_name, str) or not raw_name.strip():
            return False
        phone = profile.get("phone")
        owner_digits = re.sub(r"\D", "", str(owner_phone or ""))
        phone_digits = re.sub(r"\D", "", str(phone or ""))
        if owner_digits and phone_digits and owner_digits != phone_digits:
            return False
        avatar = profile.get("avatar")
        if isinstance(avatar, str) and len(avatar) <= 250000 and re.fullmatch(
            r"data:image/(?:png|jpeg|webp);base64,[A-Za-z0-9+/=]+", avatar
        ):
            try:
                raw_bytes = base64.b64decode(avatar.split(",", 1)[1], validate=True)
                normalized = save_owner_avatar_bytes(raw_bytes)
                if normalized:
                    avatar = normalized
            except Exception:
                pass
        else:
            avatar = None
        if not avatar and isinstance(profile.get("avatar_url"), str):
            try:
                avatar = await asyncio.wait_for(download_whatsapp_avatar(profile["avatar_url"]), timeout=2.0)
            except Exception:
                avatar = None
        if not avatar and isinstance(getattr(self, "profile", None), dict):
            prev_digits = re.sub(r"\D", "", str(self.profile.get("owner_phone") or self.profile.get("phone") or ""))
            if not owner_digits or not prev_digits or prev_digits == owner_digits:
                avatar = self.profile.get("avatar")
        self.profile = {
            "phone": phone or owner_phone,
            "owner_phone": owner_phone or phone,
            "name": raw_name.strip()[:100],
            "avatar": avatar,
            "avatar_checked": bool(avatar or profile.get("avatar_checked", True)),
            "verified_at": time.time(),
        }
        self.profile_request_until = 0
        if hasattr(self.repository, "set_local_state"):
            try:
                persist_prof = {k: v for k, v in self.profile.items() if k != "avatar" or (isinstance(v, str) and len(v) <= 250000)}
                self.repository.set_local_state("owner_profile", persist_prof)
            except Exception:
                pass
        if self.on_change:
            try:
                self.on_change("profile")
            except Exception:
                pass
        return True

    def handle(self,body):
        if isinstance(body.get("session_id"),str) and re.fullmatch(r"[a-zA-Z0-9_-]{16,80}",body["session_id"]):
            self.extension_last_seen=time.time()
        if body.get("operation")=="hello":
            self._notify_bridge_state()
            return {"state":"READY_SUPERVISED" if self.can_send else "READY_READ_ONLY","can_read_chats":True,"can_send":self.can_send}
        if self.service_loop is None:
            res = asyncio.run(self._handle(body))
        else:
            future = asyncio.run_coroutine_threadsafe(self._handle(body), self.service_loop)
            try:
                res = future.result(timeout=30)
            except TimeoutError:
                future.cancel()
                return {"error": "BRIDGE_TIMEOUT", "can_send": False}
        self._notify_bridge_state()
        return res
    async def _handle(self,body):
        if isinstance(body.get("activity"),dict):
            a=body["activity"]
            self.extension_activity={"phase":str(a.get("phase",""))[:24],"detail":str(a.get("detail",""))[:220],"at":time.time()}
        if body.get("operation")=="probe_result" and isinstance(body.get("probe"),dict):
            self.last_probe = body["probe"]
            return {"ok": True, "can_send": self.can_send}
        operation = body.get("operation")
        if operation in ("dispatch_claim", "dispatch_authorize", "dispatch_result"):
            if not hasattr(self.repository, "dispatch_outbound"):
                return {"error": "DISPATCH_DISABLED", "can_send": False}
            setup = self.cached_setup or {}
            allowed = {source["phone"] for source in setup.get("sources", []) if source.get("active", True)}
            manual_only = (self.autonomy_mode == "send")
            result = self.repository.dispatch_outbound(operation, body, setup.get("owner_phone"), allowed, self.can_send,
                                                       manual_only=manual_only)
            if self.on_change:
                self.on_change("outbound")
            log = getattr(self, "diagnostic_event", None)
            if log:
                code = result.get("error") or (body.get("outcome") if operation == "dispatch_result" else "OK")
                log("whatsapp/dispatch", level="warning" if "error" in result or code == "UNKNOWN" else "info", code=code)
            return result
        if body.get("operation")=="poll_config" and self.open_request_until>time.time():
            self.open_request_until=0
            return {"active":self.active,"open_requested":True,"config_version":self.config_version,"agent_name":self.agent_name,"can_send":self.can_send}
        cached_owner = (self.cached_setup or {}).get("owner_phone") if isinstance(self.cached_setup, dict) else None
        if body.get("operation") == "poll_config" and self.profile_request_until > time.time():
            if await self._store_profile(body.get("profile"), owner_phone=cached_owner):
                if not self.active:
                    return {"active":False,"profile_requested":False,"profile_captured":self.has_synced_profile(cached_owner),"owner_phone":cached_owner,"profile_only":True,"config_version":self.config_version,"agent_name":self.agent_name,"can_send":self.can_send}
            elif not self.active:
                return {"active":False,"profile_requested":True,"profile_request_id":self.profile_request_until,"profile_captured":False,"owner_phone":cached_owner,"profile_only":True,"config_version":self.config_version,"agent_name":self.agent_name,"can_send":self.can_send}
        if body.get("operation")=="poll_config" and not self.active:
            if body.get("profile"):
                await self._store_profile(body.get("profile"), owner_phone=cached_owner)
            return {"active":False,"scan_all":True,"poll_seconds":60,"sources":[],"profile_captured":self.has_synced_profile(cached_owner),"owner_phone":cached_owner,"config_version":self.config_version,"agent_name":self.agent_name,"can_send":self.can_send}
        if body.get("operation")=="observations" and not self.active:
            return {"error":"OBSERVATION_PAUSED","can_send":self.can_send}
        record = {"data": self.cached_setup} if isinstance(self.cached_setup, dict) else None
        if record is None and self.repository is not None:
            try:
                record = await asyncio.wait_for(self.repository.get("configuration", "business"), timeout=1.5)
            except Exception:
                record = None
        if not record: return {"error":"ONBOARDING_REQUIRED"}
        # Workbook content is data, even when an operator edits it manually.
        try: setup=BusinessSetup.model_validate(record["data"]).model_dump()
        except ValueError: return {"error":"BUSINESS_SETTINGS_INVALID"}
        self.cached_setup = setup
        if body.get("operation")=="poll_config":
            await self._store_profile(body.get("profile"), owner_phone=setup["owner_phone"])
            missing_profile = not self.has_synced_profile(setup["owner_phone"])
            return {
                "active":self.active,
                "owner_phone":setup["owner_phone"],
                "database_phone":setup.get("database_phone"),
                "scan_all":setup["scan_all"],
                "poll_seconds":60,
                "sources":[s for s in setup["sources"] if s.get("active",True)],
                "capture_profile": missing_profile or (self.profile_request_until > time.time()),
                "profile_requested": self.profile_request_until > time.time(),
                "profile_request_id": self.profile_request_until,
                "profile_captured": not missing_profile,
                "scan_since_ts": self.scan_since_ts,
                "scan_mode": self.scan_mode,
                "immutable_floor_ts": self.immutable_floor_ts,
                "config_version": self.config_version,
                "agent_name": self.agent_name,
                "can_send": self.can_send,
                "outbound": self.repository.ready_outbound_ids(setup["owner_phone"], manual_only=(self.autonomy_mode == "send"))
                if self.can_send and body.get("dispatch_version") == 1 else [],
            }
        if body.get("operation")!="observations" or not isinstance(body.get("events"),list) or len(body["events"])>20: return {"error":"INVALID_OBSERVATION_BATCH"}
        await self._store_profile(body.get("profile"), owner_phone=setup["owner_phone"])
        if isinstance(body.get("lid_map"), dict) and hasattr(self.repository, "save_lid_phone"):
            for lid, phone in body["lid_map"].items():
                if isinstance(lid, str) and lid.endswith("@lid") and isinstance(phone, str) and re.fullmatch(r"\+[1-9][0-9]{6,14}", phone):
                    self.repository.save_lid_phone(lid, phone)
        if isinstance(body.get("human_replies"), list) and hasattr(self.repository, "upsert_contact"):
            mute_sec = float(self.human_mute_hours or 4.0) * 3600.0
            now = time.time()
            for hr in body["human_replies"]:
                cid = hr.get("conversation_id") if isinstance(hr, dict) else None
                if not cid:
                    continue
                message_id = hr.get("message_id")
                if hasattr(self.repository, "has_outbound_receipt") and self.repository.has_outbound_receipt(message_id):
                    continue
                stamp = hr.get("timestamp")
                remaining = mute_sec if type(stamp) not in (int, float) else min(mute_sec, stamp + mute_sec - now)
                if remaining > 0:
                    self.repository.upsert_contact(cid, manual_mute_seconds=remaining)
        self._accept_agent_commands(body.get("agent_commands"))
        coverage=body.get("coverage")
        if isinstance(coverage,dict):
            counts={key:coverage.get(key) for key in ("discovered","scanned","failed")}
            if all(type(value) is int and 0<=value<=100000 for value in counts.values()):
                self.coverage={"scope":"rendered_recent_chats","complete":False,**counts,"reported_at":time.time()}
        # Durable intake is independent of model latency/quota. The desktop
        # worker resumes review records when Gemini becomes available.
        pipeline=IntakePipeline(self.repository,None,local_first=hasattr(self.repository, "message_counts"))
        results=[]
        for event in body["events"]:
            if not isinstance(event, dict):
                continue
            msg_id = event.get("message_id")
            conv_id = event.get("conversation_id")
            if not self.active:
                results.append({
                    "id": msg_id,
                    "message_id": msg_id,
                    "conversation_id": conv_id,
                    "status": "RETRY_REQUIRED",
                    "reason": "RUNNER_INACTIVE",
                    "sent": False,
                })
                continue
            if event.get("phone") is None and isinstance(event.get("sender_id"), str) and event["sender_id"].endswith("@lid") and hasattr(self.repository, "resolve_lid"):
                resolved = self.repository.resolve_lid(event["sender_id"])
                if resolved:
                    event = {**event, "phone": resolved["phone"]}
            accepted, ignore_reason = self.accept_event_ts(event)
            if not accepted:
                results.append({
                    "id": msg_id,
                    "message_id": msg_id,
                    "conversation_id": conv_id,
                    "status": ignore_reason,
                    "sent": False,
                })
                continue
            try:
                proc_res = await pipeline.process(event, setup)
                proc_res = dict(proc_res) if isinstance(proc_res, dict) else {"status": "SAVED"}
                proc_res.setdefault("message_id", msg_id)
                proc_res.setdefault("conversation_id", conv_id)
                if "id" not in proc_res and msg_id:
                    proc_res["id"] = msg_id
                results.append(proc_res)
                self.observations += 1

                # Advance cursor only after durable intake commit
                raw_ts = event.get("timestamp")
                if isinstance(raw_ts, (int, float)) and raw_ts > 0:
                    ts = float(raw_ts) / 1000.0 if float(raw_ts) > 1e12 else float(raw_ts)
                    self.last_event_ts = max(float(self.last_event_ts or 0.0), ts)
                    if conv_id:
                        self.per_chat_cursors[conv_id] = {"timestamp": ts, "message_id": msg_id}
            except Exception as error:
                self.last_error = str(error) if getattr(error, "code", "").startswith("MODEL_") else "A message could not be saved; the extension will retry."
                results.append({
                    "id": msg_id,
                    "message_id": msg_id,
                    "conversation_id": conv_id,
                    "status": "RETRY_REQUIRED",
                    "reason": "INTAKE_ERROR",
                    "sent": False,
                })
        self.last_batch_at=time.time()
        if results and self.on_change:
            try: self.on_change("messages")
            except Exception: pass
        return {"results":results,"can_send":self.can_send}
    def _accept_agent_commands(self, commands):
        """Forward owner "@Jarvis ..." WhatsApp commands to the desktop worker once.

        The extension has always extracted these, but nothing delivered them, so
        WhatsApp commands were silently dropped."""
        if not isinstance(commands, list) or not callable(self.on_agent_command):
            return 0
        accepted = 0
        floor = float(self.scan_since_ts or 0.0)
        for command in commands[:10]:
            if not isinstance(command, dict):
                continue
            message_id = command.get("message_id")
            text = command.get("text")
            conversation = command.get("conversation_id")
            stamp = command.get("timestamp")
            if not (isinstance(message_id, str) and message_id.startswith("true_") and len(message_id) <= 300):
                continue
            if not (isinstance(text, str) and 0 < len(text) <= 4000 and isinstance(conversation, str)):
                continue
            if message_id in self._seen_agent_msgs:
                continue
            if type(stamp) in (int, float) and floor and stamp < floor:
                self._seen_agent_msgs.add(message_id)
                continue
            self._seen_agent_msgs.add(message_id)
            if len(self._seen_agent_msgs) > 2000:
                self._seen_agent_msgs = set(list(self._seen_agent_msgs)[-1000:])
            try:
                self.on_agent_command({
                    "message_id": message_id, "text": text[:4000], "conversation_id": conversation[:120],
                    "self_chat": command.get("self_chat") is True, "timestamp": stamp,
                })
                accepted += 1
            except Exception:
                self._seen_agent_msgs.discard(message_id)
        return accepted

    async def classify_pending(self):
        if not self.active:return
        transport = getattr(self, "gemini_transport", None)
        gemini = GeminiClient(self.vault, transport=transport) if transport is not None else GeminiClient(self.vault)
        state = gemini.status()
        model_ready = state["configured"] and (state["paused_until"] <= time.time() or state.get("active_key_count"))
        local_first = hasattr(self.repository, "message_counts")
        config = {"data": self.cached_setup} if self.cached_setup else await self.repository.get("configuration", "business")
        if not config:return
        setup=BusinessSetup.model_validate(config["data"]).model_dump()
        records = None
        if local_first and hasattr(self.repository, "pending_evaluation_messages"):
            pending_evaluation = self.repository.pending_evaluation_messages(limit=10)
        elif local_first:
            records = self.repository.list_sync("messages", 500)
            pending_evaluation = [row for row in records if row["data"].get("state") == "CLASSIFIED"
                                  and row["data"].get("trade_evaluation_pending")]
        else:
            records = await self.repository.list("messages", 500)
            pending_evaluation = []

        if local_first:
            for row in pending_evaluation:
                if not self.active:
                    return
                data = row["data"]
                try:
                    assessment = await evaluate_inbound_trade(self.repository, self.vault, data, setup, triage=data["triage"])
                    action = assessment.get("action", "")
                    now = time.time()
                    updated_data = {
                        **data,
                        "trade_evaluation_pending": False,
                        "trade_action": action,
                        "last_progress_at": now,
                    }
                    if "outbound_id" in assessment:
                        updated_data["outbound_id"] = assessment["outbound_id"]

                    if action.startswith("QUEUED_") or action == "SCHEDULED_NIGHT_HOLD":
                        if hasattr(self.repository, "record_stage_event"):
                            self.repository.record_stage_event(
                                row["id"], "STOCK_MATCHING", "SUCCESS", "STOCK_MATCHED",
                                conversation_id=data.get("conversation_id"),
                                message_revision=row["revision"],
                                metadata={"evaluations_count": len(assessment.get("evaluations", []))},
                            )
                    if action == "NO_STOCK_MATCH":
                        updated_data["current_stage"] = "STOCK_MATCHING"
                        updated_data["stage_outcome"] = "BLOCKED"
                        updated_data["reason"] = "NO_MATCHING_STOCK"
                        updated_data["processing_stage"] = "Held: No matching stock found"
                        if hasattr(self.repository, "record_stage_event"):
                            self.repository.record_stage_event(
                                row["id"], "STOCK_MATCHING", "BLOCKED", "NO_MATCHING_STOCK",
                                conversation_id=data.get("conversation_id"),
                                message_revision=row["revision"],
                                metadata={"evaluations": assessment.get("evaluations", [])},
                            )
                    elif action == "SCHEDULED_NIGHT_HOLD":
                        updated_data["current_stage"] = "REPLY_GENERATION"
                        updated_data["stage_outcome"] = "PENDING"
                        updated_data["reason"] = "OUT_OF_HOURS"
                        updated_data["processing_stage"] = f"Held: Outside working hours (wakes {assessment.get('wake_local', 'morning')})"
                        if hasattr(self.repository, "record_stage_event"):
                            self.repository.record_stage_event(
                                row["id"], "REPLY_GENERATION", "PENDING", "OUT_OF_HOURS",
                                conversation_id=data.get("conversation_id"),
                                message_revision=row["revision"],
                                metadata={"wake_local": assessment.get("wake_local"), "job_id": assessment.get("job_id")},
                            )
                    elif action == "MUTED_BY_HUMAN_OR_CATEGORY":
                        updated_data["current_stage"] = "STOCK_MATCHING"
                        updated_data["stage_outcome"] = "BLOCKED"
                        updated_data["reason"] = "HUMAN_MUTED"
                        updated_data["processing_stage"] = "Held: Conversation manually muted"
                        if hasattr(self.repository, "record_stage_event"):
                            self.repository.record_stage_event(
                                row["id"], "STOCK_MATCHING", "BLOCKED", "HUMAN_MUTED",
                                conversation_id=data.get("conversation_id"),
                                message_revision=row["revision"],
                            )
                    elif action == "OBSERVE_ONLY":
                        updated_data["current_stage"] = "STOCK_MATCHING"
                        updated_data["stage_outcome"] = "BLOCKED"
                        updated_data["reason"] = "OBSERVE_ONLY"
                        updated_data["processing_stage"] = "Held: Observation mode active"
                        if hasattr(self.repository, "record_stage_event"):
                            self.repository.record_stage_event(
                                row["id"], "STOCK_MATCHING", "BLOCKED", "OBSERVE_ONLY",
                                conversation_id=data.get("conversation_id"),
                                message_revision=row["revision"],
                            )
                    elif action == "MANUAL_SEND_ONLY":
                        updated_data["current_stage"] = "STOCK_MATCHING"
                        updated_data["stage_outcome"] = "BLOCKED"
                        updated_data["reason"] = "MANUAL_SEND_ONLY"
                        updated_data["processing_stage"] = "Held: Manual send mode active"
                        if hasattr(self.repository, "record_stage_event"):
                            self.repository.record_stage_event(
                                row["id"], "STOCK_MATCHING", "BLOCKED", "MANUAL_SEND_ONLY",
                                conversation_id=data.get("conversation_id"),
                                message_revision=row["revision"],
                            )
                    elif action.startswith("QUEUED_"):
                        tgt = assessment.get("target_status", "DRAFT")
                        hold_reason = assessment.get("hold_reason")
                        reply_meta = assessment.get("reply_meta") or {}
                        req_model = reply_meta.get("model_requested")
                        served_model = reply_meta.get("model_served")
                        call_meta = reply_meta.get("call_metadata") or {}
                        if served_model:
                            updated_data["model_served"] = served_model
                        if req_model and not updated_data.get("model_requested"):
                            updated_data["model_requested"] = req_model
                        if tgt == "READY":
                            outcome = "SUCCESS"
                            reason = "DISPATCH_READY"
                            proc_stage = "Reply queued for dispatch"
                        elif hold_reason == "CAP_EXCEEDED":
                            outcome = "BLOCKED"
                            reason = "CAP_EXCEEDED"
                            proc_stage = f"Held: Quote ${assessment.get('total_quote_usd', 0):.2f} exceeds auto cap ${assessment.get('max_usd', 5000):.2f}"
                        elif tgt == "NEEDS_PRICE_REVIEW":
                            outcome = "BLOCKED"
                            reason = "NEEDS_PRICE_REVIEW"
                            proc_stage = "Held: Price review required"
                        else:
                            outcome = "PENDING"
                            reason = "DRAFT_HELD"
                            proc_stage = "Draft quote held for operator review"

                        updated_data["current_stage"] = "REPLY_GENERATION"
                        updated_data["stage_outcome"] = outcome
                        updated_data["reason"] = reason
                        updated_data["processing_stage"] = proc_stage
                        if hasattr(self.repository, "record_stage_event"):
                            self.repository.record_stage_event(
                                row["id"], "REPLY_GENERATION", outcome, reason,
                                conversation_id=data.get("conversation_id"),
                                message_revision=row["revision"],
                                metadata={
                                    "outbound_id": assessment.get("outbound_id"),
                                    "target_status": tgt,
                                    "hold_reason": hold_reason,
                                    "total_quote_usd": assessment.get("total_quote_usd"),
                                    "model_requested": req_model,
                                    "model_served": served_model,
                                    **call_meta,
                                },
                            )

                    await self.repository.put("messages", row["id"], updated_data, row["revision"])
                    if self.on_change and action.startswith(("QUEUED_", "SCHEDULED_")):
                        self.on_change("outbound")
                except Exception:
                    self.last_error = "A trade assessment needs retry."
            if pending_evaluation and (await load_desk_settings(self.repository))["match_alerts"]:
                try:
                    await match_opportunities(self.repository)
                except Exception:
                    self.last_error = "Sourcing alerts need retry."

        if hasattr(self.repository, "pending_media_messages"):
            media_pending = self.repository.pending_media_messages(limit=10)
            now = time.time()
            for r in media_pending:
                data = r["data"]
                job = data.get("media_job") or {}
                deadline = job.get("deadline")
                if deadline and now > float(deadline):
                    if hasattr(self.repository, "update_media_job"):
                        self.repository.update_media_job(r["id"], status="TIMEOUT", last_error="MEDIA_TIMEOUT_EXPIRED", retryable=True)
                    if hasattr(self.repository, "record_stage_event"):
                        self.repository.record_stage_event(
                            r["id"], "MEDIA_EXTRACTION", "BLOCKED", "MEDIA_TIMEOUT",
                            conversation_id=data.get("conversation_id"),
                            message_revision=r["revision"],
                            retryable=True,
                            metadata={"deadline": deadline, "elapsed_s": now - float(job.get("first_requested_at") or now)},
                        )

        if not model_ready:
            return 0

        if hasattr(self.repository, "pending_classification_messages"):
            pending = self.repository.pending_classification_messages(limit=6)
        else:
            if records is None:
                records = self.repository.list_sync("messages", 500) if local_first else await self.repository.list("messages", 500)
            pending = sorted(
                (r for r in records if r["data"].get("state") in ("RECEIVED", "NEEDS_REVIEW")
                 and r["data"].get("reason") not in ("MEDIA_BYTES_REQUIRED", "MEDIA_STOCK_UNCONFIRMED", "MEDIA_TIMEOUT")
                 and r["data"].get("classifier_attempts", 0) < 3),
                key=lambda r: r["updated_at"]
            )
        if not pending:
            return 0
        changed = False
        sem = asyncio.Semaphore(3)

        async def process_one(record):
            nonlocal changed
            if not self.active:
                return
            async with sem:
                try:
                    res = await IntakePipeline(self.repository, gemini, on_change=self.on_change, local_first=local_first).process({**record["data"], "identity_verified": True}, setup)
                    if self.on_change:
                        try: self.on_change("messages")
                        except Exception: pass
                    if res.get("status") == "CLASSIFIED" and not local_first:
                        fresh = await self.repository.get("messages", res.get("id") or record["id"])
                        await evaluate_inbound_trade(self.repository, self.vault, (fresh["data"] if fresh else record["data"]), setup, triage=res.get("triage"))
                    self.last_error = None
                    changed = True
                except Exception as error:
                    code = getattr(error, "code", "")
                    if code.startswith("MODEL_"):
                        self.last_error = f"Gemini {code}: {error}"
                    else:
                        self.last_error = "A queued message needs retry."

        await asyncio.gather(*(process_one(r) for r in pending[:6]), return_exceptions=True)
        if changed and (await load_desk_settings(self.repository))["match_alerts"]:
            try:
                await match_opportunities(self.repository)
            except Exception:
                pass
        if changed and self.on_change:
            try: self.on_change("messages")
            except Exception: pass
        return 1 if changed else 0
    def status(self):
        fresh=self.bridge.ready and 0<=time.time()-self.extension_last_seen<180
        active_state = "ACTIVE_SUPERVISED" if (fresh and self.active and self.can_send) else ("ACTIVE_READ_ONLY" if fresh and self.active else "PAUSED" if fresh else "AWAITING_EXTENSION" if self.bridge.ready else "STOPPED")
        return {"state":active_state,"verified":fresh,"last_seen":(self.extension_last_seen or self.bridge.last_seen or None),"observations":self.observations,"can_send":self.can_send,"autonomy_mode":self.autonomy_mode,"poll_seconds":30,"active":self.active,"last_error":self.last_error,"last_batch_at":self.last_batch_at,"coverage":self.coverage,"activity":getattr(self,"extension_activity",None),"last_probe":self.last_probe}


