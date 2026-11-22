import base64
import hashlib
import io
import re
import time
from datetime import datetime, timezone

from .sheets import SheetsError
from .fx import buyer_pricing
from .providers import Triage
from .trading_engine import extract_spreadsheet_text, is_worth_effort


_SUPPORTED_IMAGE_MIMES = {"image/jpeg", "image/jpg", "image/png", "image/webp", "image/gif", "application/pdf"}


def compress_or_prepare_media(media_b64: str | None, media_mime: str | None = None) -> tuple[str | None, str | None]:
    if not isinstance(media_b64, str) or not media_b64.strip():
        return None, None
    mime = str(media_mime or "").strip().lower().split(";")[0]
    if mime and mime not in _SUPPORTED_IMAGE_MIMES and not mime.startswith("image/"):
        return None, "INVALID_MIME"
    clean_b64 = media_b64.strip()
    if len(clean_b64) > 6_000_000:
        return None, "UNSUPPORTED_SIZE"
    if "pdf" in mime or clean_b64.startswith("JVBERi0"):
        if len(clean_b64) <= 4_500_000:
            return clean_b64, "application/pdf"
        return None, "UNSUPPORTED_SIZE"
    try:
        raw_bytes = base64.b64decode(clean_b64[:4_500_000], validate=False)
        from PIL import Image
        with Image.open(io.BytesIO(raw_bytes)) as img:
            max_dim = 1600
            if img.width > max_dim or img.height > max_dim:
                img.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=85, optimize=True)
            return base64.b64encode(buf.getvalue()).decode("ascii"), "image/jpeg"
    except Exception:
        if len(clean_b64) <= 500_000:
            return clean_b64, media_mime or "image/jpeg"
        return None, "UNSUPPORTED_SIZE"



_LOCAL_INTENT = re.compile(r"^(wtb|wts|ready stock|stock update)\s*[:\-]?\s+", re.I)
_LOCAL_PRE_QTY = re.compile(r"^(?P<qty>\d{1,6})\s*(?:pcs|units?|pieces?)?\s+", re.I)
_LOCAL_PRODUCT = re.compile(r"(?P<device>(?:apple\s+)?iphone\s+\d{1,2}(?:\s+(?:pro max|pro|plus|mini|air|e))?|(?:samsung\s+)?(?:galaxy\s+)?[saz]\d{1,2}(?:\s+(?:ultra|plus|fe))?|(?:google\s+)?pixel\s+\d{1,2}(?:\s+pro(?:\s+xl)?)?)\s+(?P<variant>(?:\d{1,2}/)?\d{2,4}\s*(?:gb|tb)|\d{1,2}/\d{2,4})", re.I)
_LOCAL_TERMS = re.compile(r"(?:\s*[,;]?\s*(?P<qty>\d{1,6})\s*(?:pcs|units?|pieces?))?(?:\s*[,;]?\s*(?P<currency>USD|INR|AED|GBP|EUR)\s*(?P<price>\d+(?:\.\d{1,2})?))?[\s.!]*", re.I)


def classify_local_trade(text):
    intent = _LOCAL_INTENT.match(text)
    if not intent:
        return None
    pos = intent.end()
    pre = _LOCAL_PRE_QTY.match(text[pos:])
    pre_qty = None
    if pre:
        pre_qty = int(pre["qty"])
        pos += pre.end()
    product = _LOCAL_PRODUCT.match(text, pos)
    if not product:
        return None
    terms = _LOCAL_TERMS.fullmatch(text[product.end():])
    if not terms and not pre_qty:
        return None
    category = {"wtb": "BUY", "wts": "SELL", "ready stock": "STOCK", "stock update": "STOCK"}[intent[1].lower()]
    quantity = pre_qty or (int(terms["qty"]) if terms and terms["qty"] else None)
    price = float(terms["price"]) if terms and terms["price"] else None
    currency = (terms["currency"].upper() if terms and terms["currency"] else None) if terms else None
    if quantity == 0 or price == 0 or (quantity is None and price is None):
        return None
    return Triage(category=category, proceed=True, needs_review=False, reason="Explicit trade format",
                  items=[{"device": product["device"], "variant": product["variant"], "quantity": quantity,
                          "unit_price": price, "currency": currency}])


class IntakePipeline:
    def __init__(self,repository,classifier,*,on_change=None,local_first=False):
        self.repository,self.classifier=repository,classifier
        self.on_change=on_change
        self.local_first=local_first

    async def _get_record(self, collection, record_id):
        if self.local_first and hasattr(self.repository, "get_sync"):
            return self.repository.get_sync(collection, record_id)
        return await self.repository.get(collection, record_id)

    def _trace(self, source_id, stage, outcome, reason_code, *, conversation_id=None, account_phone=None, message_revision=0, attempt_id=None, retryable=False, metadata=None):
        if hasattr(self.repository, "record_stage_event"):
            try:
                self.repository.record_stage_event(
                    source_id, stage, outcome, reason_code,
                    conversation_id=conversation_id,
                    account_phone=account_phone,
                    message_revision=message_revision,
                    attempt_id=attempt_id,
                    retryable=retryable,
                    metadata=metadata,
                )
            except Exception:
                pass

    def _is_decision_protected(self, record_data):
        msg_id = (record_data or {}).get("message_id")
        if msg_id and hasattr(self.repository, "has_outbound_receipt") and self.repository.has_outbound_receipt(msg_id):
            return True
        oid = (record_data or {}).get("outbound_id")
        if oid and hasattr(self.repository, "list_outbound"):
            rows = self.repository.list_outbound(statuses=("ACCEPTED", "SENT", "DELIVERED", "READ"))
            if any(r.get("id") == oid for r in rows):
                return True
        return False

    async def process(self,event,setup):
        # The bridge does not authorize an owner command, inventory receipt, or send.
        owner_phone = setup.get("owner_phone")
        phone = event.get("phone")
        if owner_phone and phone == owner_phone and event.get("outgoing") is not True:
            return {"status": "IGNORED_OWNER"}
        sources={x["phone"] for x in setup.get("sources", []) if x.get("active",True)}
        database_phone = setup.get("database_phone")
        from_database = bool(database_phone and phone == database_phone)
        lead_capture = setup.get("lead_capture", setup.get("scan_all", False))
        if not lead_capture and phone not in sources and not from_database: return {"status":"IGNORED_SOURCE"}
        conversation=event.get("conversation_id")
        if from_database and (not isinstance(conversation, str) or conversation.endswith("@g.us")):
            return {"status":"IGNORED_SOURCE"}
        if setup.get("scan_all") and event.get("outgoing") is not True:
            if not isinstance(conversation,str) or not re.fullmatch(r"[0-9-]{5,80}@(?:c\.us|s\.whatsapp\.net|g\.us|lid)",conversation):
                return {"status":"NEEDS_REVIEW","reason":"Conversation identity is not verified"}
            if not isinstance(event.get("message_id"),str) or not event["message_id"].startswith("false_"+conversation+"_"):
                return {"status":"NEEDS_REVIEW","reason":"Message does not belong to this conversation"}
            phone=event.get("phone")
            if phone is not None:
                if not isinstance(phone,str) or not re.fullmatch(r"\+[1-9][0-9]{6,14}",phone):
                    return {"status":"NEEDS_REVIEW","reason":"Sender phone is not verified"}
                phone_identity=event.get("sender_id") if conversation.endswith("@g.us") else conversation
                lid_ok = False
                if isinstance(phone_identity, str) and phone_identity.endswith("@lid") and hasattr(self.repository, "resolve_lid"):
                    try:
                        mapped = self.repository.resolve_lid(phone_identity)
                        lid_ok = bool(mapped and mapped.get("phone") == phone)
                    except Exception:
                        lid_ok = False
                if not lid_ok and phone_identity not in (phone[1:]+"@c.us",phone[1:]+"@s.whatsapp.net"):
                    return {"status":"NEEDS_REVIEW","reason":"Sender phone is not bound to its identity"}
        raw_text = event.get("text", "")
        if not isinstance(raw_text, str):
            return {"status": "NEEDS_REVIEW", "reason": "Unsupported message size/type"}
        has_media = bool(event.get("has_media"))
        media_type = str(event.get("media_type") or "")[:32] or None
        media_mime = str(event.get("media_mime") or "")[:64] or None
        media_b64 = event.get("media_b64")
        if media_b64:
            has_media = True
            media_type = media_type or ("document" if media_mime == "application/pdf" else "image")
        if raw_text and len(raw_text) > 100 and not re.search(r"\s", raw_text) and raw_text.startswith(("/9j/", "iVBORw0KGgo", "R0lGOD", "UklGR", "JVBERi0")):
            if not media_b64:
                media_b64 = raw_text
                has_media = True
                media_type = media_type or ("document" if media_mime == "application/pdf" else "image")
            text = "[Photo / Stock Image]" if media_mime != "application/pdf" else "[Stock Document / PDF]"
        else:
            text = raw_text
        if len(text.encode("utf-8")) > 100000:
            return {"status": "NEEDS_REVIEW", "reason": "Unsupported message size/type"}
        if not isinstance(event.get("message_id"),str) or not event["message_id"] or len(event["message_id"])>500 or event.get("identity_verified") is not True: return {"status":"NEEDS_REVIEW","reason":"Message/source identity is not verified"}
        if conversation and (conversation.endswith("@g.us") or conversation.endswith("@lid")):
            canonical_identity = conversation
        elif event.get("phone"):
            canonical_identity = event.get("phone")
        else:
            canonical_identity = conversation or "unknown"
        event_id = hashlib.sha256((canonical_identity + ":" + event["message_id"]).encode()).hexdigest()

        legacy_identity = conversation if conversation and (conversation.endswith("@g.us") or not event.get("phone")) else event.get("phone")
        legacy_id = hashlib.sha256((legacy_identity + ":" + event["message_id"]).encode()).hexdigest() if legacy_identity and legacy_identity != canonical_identity else None
        contact_cat = "SUPPLIER_DEALER" if event.get("phone") in sources else "UNKNOWN"
        if conversation and hasattr(self.repository, "get_contact"):
            cprof = self.repository.get_contact(conversation)
            if cprof and cprof.get("category") and cprof.get("category") != "UNKNOWN":
                contact_cat = cprof["category"]
            elif hasattr(self.repository, "upsert_contact"):
                self.repository.upsert_contact(
                    conversation,
                    phone=event.get("phone"),
                    name=event.get("sender_name") or event.get("chat_name"),
                    category=contact_cat,
                    last_scanned_ts=event.get("timestamp") or time.time(),
                    last_contacted_at=event.get("timestamp") or time.time(),
                )
        if contact_cat in ("FAMILY_PERSONAL", "IGNORED"):
            return {"status": "IGNORED_FAMILY"}
        if event.get("outgoing") is True:
            phone = event.get("phone")
            if (not isinstance(phone, str) or not re.fullmatch(r"\+[1-9][0-9]{6,14}", phone)
                    or not isinstance(conversation, str)
                    or not event["message_id"].startswith("true_" + conversation + "_")):
                return {"status": "IGNORED_OUTGOING"}
            direct = conversation in (phone[1:] + "@c.us", phone[1:] + "@s.whatsapp.net")
            lid = self.repository.resolve_lid(conversation) if conversation.endswith("@lid") and hasattr(self.repository, "resolve_lid") else None
            if not direct and (not lid or lid.get("phone") != phone):
                return {"status": "IGNORED_OUTGOING"}
            existing_out = await self._get_record("messages", event_id)
            if not existing_out and legacy_id:
                existing_out = await self._get_record("messages", legacy_id)
            if existing_out:
                return {"status": "DUPLICATE", "id": existing_out["id"], "message_id": event["message_id"]}
            # Human staff takeover detected (outgoing message without bot receipt)
            if hasattr(self.repository, "has_outbound_receipt") and not self.repository.has_outbound_receipt(event["message_id"]):
                mute_hours = float(setup.get("human_mute_hours") or 4.0)
                mute_sec = int(mute_hours * 3600)
                if hasattr(self.repository, "upsert_contact"):
                    self.repository.upsert_contact(conversation, manual_mute_seconds=mute_sec)
            ack = int(event.get("ack", 1) or 1)
            deliv_status = event.get("delivery_status")
            if not deliv_status:
                if ack >= 3:
                    deliv_status = "READ"
                elif ack >= 2:
                    deliv_status = "DELIVERED"
                elif ack >= 1:
                    deliv_status = "SENT"
                else:
                    deliv_status = "PENDING"
            await self.repository.put("messages", event_id, {
                "direction": "outgoing", "state": "OUTGOING", "phone": phone, "conversation_id": conversation,
                "chat_name": str(event.get("chat_name") or "")[:160], "message_id": event["message_id"],
                "text": text, "timestamp": event.get("timestamp"), "received": time.time(),
                "delivery_status": deliv_status, "ack": ack,
                "classification_source": "outbound_echo",
                **({"outbound_id": event["outbound_id"]} if event.get("outbound_id") else {}),
            }, 0, event_id)
            return {"status": "OUTGOING", "id": event_id}
        stock_file = from_database and event.get("media_type") == "document" and bool(event.get("stock_file_name"))
        if event.get("stock_file_name") and not stock_file:
            return {"status": "NEEDS_REVIEW", "reason": "INVALID_STOCK_FILE"}
        if stock_file and (not isinstance(event["stock_file_name"], str) or len(event["stock_file_name"]) > 160
                           or not re.search(r"\.(xlsx|csv|tsv|txt|json)$", event["stock_file_name"], re.I)):
            return {"status": "NEEDS_REVIEW", "reason": "INVALID_STOCK_FILE"}
        if stock_file and event.get("stock_file_b64"):
            direct_ids = (database_phone[1:] + "@c.us", database_phone[1:] + "@s.whatsapp.net")
            mapped = self.repository.resolve_lid(conversation) if conversation.endswith("@lid") and hasattr(self.repository, "resolve_lid") else None
            if conversation not in direct_ids and (not mapped or mapped.get("phone") != database_phone):
                return {"status": "IGNORED_SOURCE"}
            try:
                encoded = event["stock_file_b64"]
                if not isinstance(encoded, str) or len(encoded) > 200000:
                    raise ValueError("Stock file too large")
                decoded = base64.b64decode(encoded, validate=True)
                extracted, _ = extract_spreadsheet_text(decoded, file_name=event["stock_file_name"])
                if not extracted.strip() or len(extracted.encode("utf-8")) >= 12000:
                    raise ValueError("Stock data missing or exceeds one import")
                text = extracted
            except (TypeError, ValueError):
                return {"status": "NEEDS_REVIEW", "reason": "INVALID_STOCK_FILE"}
        elif stock_file and not event.get("stock_file_text"):
            return {"status": "NEEDS_REVIEW", "reason": "STOCK_FILE_BYTES_REQUIRED"}
        metadata={
            "phone":event.get("phone"),
            "conversation_id":conversation,
            "sender_id":str(event.get("sender_id") or "")[:160],
            "sender_name":str(event.get("sender_name") or "")[:160],
            "chat_name":str(event.get("chat_name") or "")[:160],
            "chat_kind":"group" if conversation and conversation.endswith("@g.us") else "direct",
            "stock_source":event.get("phone") in sources,
            "contact_category":contact_cat,
            "has_media":has_media,
            "media_type":media_type,
            **({"stock_file_name": event["stock_file_name"], "stock_file_text": True} if stock_file else {}),
        }
        existing=await self._get_record("messages",event_id)
        if not existing and legacy_id:
            existing=await self._get_record("messages",legacy_id)
        if existing and existing["data"].get("phone") is None and event.get("phone"):
            existing_data = {**existing["data"], "phone": event.get("phone")}
            existing = await self.repository.put("messages", existing["id"], existing_data, existing["revision"])
        if existing:
            event_id = existing["id"]
        if existing and existing["data"].get("source_text", existing["data"].get("text"))!=text:
            existing=await self.repository.put("messages",event_id,{**existing["data"],"text":text,"source_text":text,"state":"RECEIVED","buyer_pricing":None,"classifier_attempts":0,"edited_observed_at":time.time()},existing["revision"])
            if hasattr(self.repository, "resolve_superseded_notifications"):
                self.repository.resolve_superseded_notifications(event_id, existing["revision"])
        if existing and existing["data"].get("state")=="CLASSIFIED":
            await self._intent(event_id,existing)
            return {"status":"DUPLICATE","id":event_id,"message_id":event["message_id"]}
        if existing and existing["data"].get("state") in ("IGNORED_SOCIAL","IGNORED_FAMILY"):
            await self._withdraw(event_id)
            return {"status":"DUPLICATE","id":event_id,"message_id":event["message_id"]}
        if media_mime:
            clean_mime_val = str(media_mime).strip().lower().split(";")[0]
            if clean_mime_val and clean_mime_val not in _SUPPORTED_IMAGE_MIMES and not clean_mime_val.startswith("image/"):
                now = time.time()
                job = {
                    "status": "FAILED",
                    "attempts": 1,
                    "first_requested_at": now,
                    "last_attempt_at": now,
                    "deadline": None,
                    "bytes_hash": None,
                    "ocr_status": "FAILED",
                    "last_error": "INVALID_MIME",
                    "retryable": False,
                }
                cur = existing["data"] if existing else metadata
                cur_rev = existing["revision"] if existing else 0
                saved = await self.repository.put("messages", event_id, {
                    **cur,
                    "message_id": event["message_id"],
                    "text": text,
                    "state": "NEEDS_REVIEW",
                    "reason": "INVALID_MEDIA",
                    "current_stage": "MEDIA_EXTRACTION",
                    "stage_outcome": "FAILED",
                    "processing_stage": "Unsupported media MIME format",
                    "last_progress_at": now,
                    "media_job": job,
                }, cur_rev, event_id if not existing else None)
                self._trace(event_id, "MEDIA_EXTRACTION", "FAILED", "INVALID_MEDIA",
                            conversation_id=conversation, message_revision=saved["revision"],
                            metadata={"mime": media_mime})
                return {"status": "NEEDS_REVIEW", "reason": "INVALID_MEDIA"}

        if media_b64:
            clean_b64, clean_mime = compress_or_prepare_media(media_b64, media_mime)
            if clean_mime == "INVALID_MIME":
                now = time.time()
                job = {
                    "status": "FAILED",
                    "attempts": 1,
                    "first_requested_at": now,
                    "last_attempt_at": now,
                    "deadline": None,
                    "bytes_hash": None,
                    "ocr_status": "FAILED",
                    "last_error": "INVALID_MIME",
                    "retryable": False,
                }
                cur = existing["data"] if existing else metadata
                cur_rev = existing["revision"] if existing else 0
                saved = await self.repository.put("messages", event_id, {
                    **cur,
                    "message_id": event["message_id"],
                    "text": text,
                    "state": "NEEDS_REVIEW",
                    "reason": "INVALID_MEDIA",
                    "current_stage": "MEDIA_EXTRACTION",
                    "stage_outcome": "FAILED",
                    "processing_stage": "Unsupported media MIME format",
                    "last_progress_at": now,
                    "media_job": job,
                }, cur_rev, event_id if not existing else None)
                self._trace(event_id, "MEDIA_EXTRACTION", "FAILED", "INVALID_MEDIA",
                            conversation_id=conversation, message_revision=saved["revision"],
                            metadata={"mime": media_mime})
                return {"status": "NEEDS_REVIEW", "reason": "INVALID_MEDIA"}
            elif clean_mime == "UNSUPPORTED_SIZE":
                now = time.time()
                job = {
                    "status": "FAILED",
                    "attempts": 1,
                    "first_requested_at": now,
                    "last_attempt_at": now,
                    "deadline": None,
                    "bytes_hash": None,
                    "ocr_status": "FAILED",
                    "last_error": "UNSUPPORTED_SIZE",
                    "retryable": False,
                }
                cur = existing["data"] if existing else metadata
                cur_rev = existing["revision"] if existing else 0
                saved = await self.repository.put("messages", event_id, {
                    **cur,
                    "message_id": event["message_id"],
                    "text": text,
                    "state": "NEEDS_REVIEW",
                    "reason": "UNSUPPORTED_SIZE",
                    "current_stage": "MEDIA_EXTRACTION",
                    "stage_outcome": "BLOCKED",
                    "processing_stage": "Media bytes exceed maximum size limit",
                    "last_progress_at": now,
                    "media_job": job,
                }, cur_rev, event_id if not existing else None)
                self._trace(event_id, "MEDIA_EXTRACTION", "BLOCKED", "UNSUPPORTED_SIZE",
                            conversation_id=conversation, message_revision=saved["revision"],
                            metadata={"bytes_len": len(media_b64)})
                return {"status": "NEEDS_REVIEW", "reason": "UNSUPPORTED_SIZE"}
            else:
                media_b64 = clean_b64
                media_mime = clean_mime

        bytes_hash = hashlib.sha256(base64.b64decode(media_b64[:4500000], validate=False)).hexdigest() if (media_b64 and media_mime) else None

        has_caption = bool(text.strip() and text.strip() not in ("", "[Photo / Stock Image]", "[Stock Document / PDF]", "[Media attachment]"))
        attachment_only = bool(has_media and not stock_file and not has_caption)

        if not existing:
            now = time.time()
            job = {
                "status": "ENRICHED" if (media_b64 and media_mime) else ("WAITING_BYTES" if has_media else "NONE"),
                "attempts": 1 if has_media else 0,
                "first_requested_at": now if has_media else None,
                "last_attempt_at": now if has_media else None,
                "deadline": (now + 60.0) if (has_media and not (media_b64 and media_mime)) else None,
                "bytes_hash": bytes_hash,
                "has_caption": has_caption,
                "caption_text": text if has_caption else None,
                "ocr_status": "READY" if (media_b64 and media_mime) else ("PENDING" if has_media else "NONE"),
                "last_error": None,
                "retryable": not bool(media_b64 and media_mime),
            }
            rec_payload = {**metadata, "message_id": event["message_id"], "text": text, "state": "RECEIVED", "received": now, "media_job": job}
            sent_at = event.get("timestamp")
            if type(sent_at) in (int, float) and 0 < sent_at <= rec_payload["received"] + 300:
                rec_payload["timestamp"] = sent_at
            if media_b64 and media_mime:
                rec_payload["media_b64"] = media_b64
                rec_payload["media_mime"] = media_mime
            record = await self.repository.put("messages", event_id, rec_payload, 0, event_id)
            self._trace(event_id, "CAPTURE", "SUCCESS", "CAPTURED",
                        conversation_id=conversation, account_phone=setup.get("owner_phone"),
                        message_revision=record["revision"])
        else:
            record = existing
            if media_b64 and media_mime and record["data"].get("reason") == "MEDIA_BYTES_REQUIRED":
                now = time.time()
                job = {
                    **(record["data"].get("media_job") or {}),
                    "status": "ENRICHED",
                    "bytes_hash": bytes_hash,
                    "ocr_status": "READY",
                    "last_attempt_at": now,
                    "retryable": False,
                }
                record = await self.repository.put("messages", event_id, {
                    **record["data"],
                    "state": "RECEIVED",
                    "reason": None,
                    "media_b64": media_b64,
                    "media_mime": media_mime,
                    "media_job": job,
                    "current_stage": "MEDIA_EXTRACTION",
                    "stage_outcome": "SUCCESS",
                    "last_progress_at": now,
                }, record["revision"])
                self._trace(event_id, "MEDIA_EXTRACTION", "SUCCESS", "MEDIA_BYTES_ENRICHED",
                            conversation_id=conversation, message_revision=record["revision"],
                            metadata={"bytes_hash": bytes_hash, "mime": media_mime})
            elif not media_b64 and isinstance(record["data"].get("media_b64"), str):
                media_b64 = record["data"].get("media_b64")
                media_mime = record["data"].get("media_mime") or "image/jpeg"
        worth, worth_reason = is_worth_effort(text, contact_cat, has_media=has_media, media_type=media_type)
        if not worth and not stock_file:
            ignored_state = "IGNORED_FAMILY" if worth_reason == "FAMILY_PERSONAL" else "IGNORED_SOCIAL"
            clean_data = {k: v for k, v in record["data"].items() if k != "media_b64"}
            now = time.time()
            saved = await self.repository.put("messages", event_id, {
                **clean_data, "state": ignored_state, "reason": worth_reason,
                "current_stage": "LOCAL_TRIAGE", "stage_outcome": "IGNORED",
                "processing_stage": f"Filtered: {worth_reason}", "last_progress_at": now,
            }, record["revision"])
            self._trace(event_id, "LOCAL_TRIAGE", "IGNORED", worth_reason,
                        conversation_id=conversation, message_revision=saved["revision"])
            await self._withdraw(event_id)
            return {"status": ignored_state}
        has_caption = bool(text.strip() and text.strip() not in ("", "[Photo / Stock Image]", "[Stock Document / PDF]", "[Media attachment]"))
        attachment_only = bool(has_media and not stock_file and not has_caption)
        prev_job = record["data"].get("media_job") or {}
        media_job_failed = prev_job.get("status") in ("TIMEOUT", "FAILED")
        if has_media and not stock_file and not (media_b64 and media_mime):
            if media_job_failed and has_caption:
                # OCR failed/timed out, but message has caption text. Proceed to classify caption,
                # holding final evaluation in NEEDS_REVIEW.
                pass
            elif media_job_failed:
                return {"status": "NEEDS_REVIEW", "id": event_id, "reason": prev_job.get("last_error") or record["data"].get("reason") or "MEDIA_TIMEOUT", "sent": False}
            else:
                now = time.time()
                job = {
                    "status": "WAITING_BYTES",
                    "attempts": prev_job.get("attempts", 0) + 1 if existing else 1,
                    "first_requested_at": prev_job.get("first_requested_at") or now if existing else now,
                    "last_attempt_at": now,
                    "deadline": now + 60.0,
                    "bytes_hash": None,
                    "has_caption": has_caption,
                    "caption_text": text if has_caption else None,
                    "ocr_status": "PENDING",
                    "last_error": None,
                    "retryable": True,
                }
                if record["data"].get("reason") != "MEDIA_BYTES_REQUIRED":
                    saved = await self.repository.put("messages", event_id, {
                        **record["data"], "state": "NEEDS_REVIEW", "reason": "MEDIA_BYTES_REQUIRED",
                        "current_stage": "MEDIA_EXTRACTION", "stage_outcome": "PENDING",
                        "processing_stage": "Waiting for WhatsApp image bytes", "last_progress_at": now,
                        "media_job": job,
                    }, record["revision"])
                else:
                    saved = record
                self._trace(event_id, "MEDIA_EXTRACTION", "PENDING", "MEDIA_BYTES_REQUIRED",
                            conversation_id=conversation, message_revision=saved["revision"],
                            retryable=True, metadata=job)
                return {"status": "CAPTURED_REVIEW", "id": event_id, "sent": False}
        if self.local_first and not has_media and not stock_file:
            result = classify_local_trade(text)
            if result:
                triage = result.model_dump()
                now = time.time()
                saved = await self.repository.put("messages", event_id, {
                    **record["data"], "state": "CLASSIFIED", "triage": triage, "reason": "LOCAL_TRADE_PARSED",
                    "current_stage": "LOCAL_TRIAGE", "stage_outcome": "SUCCESS",
                    "processing_stage": "Trade format parsed locally", "last_progress_at": now,
                    "classification_source": "local", "model_requested": None, "model_served": None,
                    "trade_evaluation_pending": result.proceed,
                }, record["revision"])
                self._trace(event_id, "LOCAL_TRIAGE", "SUCCESS", "LOCAL_TRADE_PARSED",
                            conversation_id=conversation, message_revision=saved["revision"],
                            metadata={"category": triage["category"], "parser": "regex_trade_parser"})
                await self._intent(event_id, saved)
                return {"status": "CLASSIFIED", "id": event_id, "triage": triage, "sent": False}
        if self.classifier is None:
            is_configured = bool(setup.get("classifier_configured") or setup.get("gemini_configured"))
            state = "RECEIVED" if is_configured else "NEEDS_REVIEW"
            reason = "PENDING_CLASSIFICATION" if is_configured else "MODEL_KEY_REQUIRED"
            now = time.time()
            saved = await self.repository.put("messages", event_id, {
                **record["data"],
                "state": state,
                "reason": reason,
                "current_stage": "AI_CLASSIFICATION",
                "stage_outcome": "PENDING",
                "processing_stage": "Pending AI classification" if is_configured else "Waiting for Gemini configuration",
                "last_progress_at": now,
                "model_requested": None,
                "model_served": None,
            }, record["revision"])
            self._trace(event_id, "AI_CLASSIFICATION", "PENDING", reason,
                        conversation_id=conversation, message_revision=saved["revision"])
            return {"status": "CAPTURED_REVIEW", "id": event_id, "sent": False, "queue_state": reason}
        # Persist an attempt count before a paid model call. Crash retries are bounded.
        attempts=record["data"].get("classifier_attempts",0)
        if attempts>=3:
            now = time.time()
            saved = await self.repository.put("messages", event_id, {
                **record["data"], "state": "NEEDS_REVIEW", "reason": "Classification retry budget exhausted",
                "current_stage": "AI_CLASSIFICATION", "stage_outcome": "FAILED",
                "processing_stage": "Retry budget exhausted", "last_progress_at": now,
            }, record["revision"])
            self._trace(event_id, "AI_CLASSIFICATION", "FAILED", "CLASSIFICATION_RETRY_EXHAUSTED",
                        conversation_id=conversation, message_revision=saved["revision"])
            return {"status":"NEEDS_REVIEW","reason":"Classification retry budget exhausted"}
        if not await self._reserve_budget(setup):
            now = time.time()
            saved = await self.repository.put("messages", event_id, {
                **record["data"], "state": "NEEDS_REVIEW", "reason": "Daily classification limit reached",
                "current_stage": "AI_CLASSIFICATION", "stage_outcome": "BLOCKED",
                "processing_stage": "Daily limit reached", "last_progress_at": now,
            }, record["revision"])
            self._trace(event_id, "AI_CLASSIFICATION", "BLOCKED", "DAILY_LIMIT_REACHED",
                        conversation_id=conversation, message_revision=saved["revision"])
            return {"status":"NEEDS_REVIEW","reason":"Daily classification limit reached"}
        stage = "Gemini is reading the image" if media_b64 and media_mime else "Gemini is classifying this message"
        now = time.time()
        record=await self.repository.put("messages",event_id,{
            **record["data"],"reason":None,"processing_stage":stage,
            "current_stage": "AI_CLASSIFICATION", "stage_outcome": "PENDING", "last_progress_at": now,
            "classifier_attempts":attempts+1
        },record["revision"])
        self._trace(event_id, "AI_CLASSIFICATION", "PENDING", "CLASSIFYING",
                    conversation_id=conversation, message_revision=record["revision"],
                    metadata={"attempt": attempts + 1})
        if self.on_change:
            self.on_change("messages")
        try:
            if media_b64 and media_mime:
                try:
                    result=await self.classifier.classify(text, media_b64=media_b64, media_mime=media_mime)
                except TypeError:
                    result=await self.classifier.classify(text)
            else:
                result=await self.classifier.classify(("Daily inventory snapshot. Extract stock rows.\n" if stock_file else "") + text)
        except Exception as error:
            code = getattr(error,"code",None) or "CLASSIFICATION_FAILED"
            retryable = code in ("MODEL_QUOTA","MODEL_COOLDOWN","MODEL_UNAVAILABLE")
            now = time.time()
            saved = await self.repository.put("messages",event_id,{
                **record["data"],"state":"NEEDS_REVIEW","processing_stage":f"Classification halted: {code}","reason":code,
                "current_stage": "AI_CLASSIFICATION", "stage_outcome": "FAILED", "last_progress_at": now,
                "classifier_attempts":attempts if retryable else attempts+1,
            },record["revision"])
            self._trace(event_id, "AI_CLASSIFICATION", "FAILED", code,
                        conversation_id=conversation, message_revision=saved["revision"],
                        retryable=retryable, metadata={"error": str(error)})
            if self.on_change:
                self.on_change("messages")
            raise
        triage=result.model_dump()
        clean_data={k: v for k, v in record["data"].items() if k not in ("media_b64", "reason", "processing_stage")}
        now = time.time()
        req_model = getattr(result, "model_requested", None) or getattr(self.classifier, "model", None)
        served_model = getattr(result, "model_served", None) or req_model
        call_meta = getattr(result, "call_metadata", None) or {}
        triage["classification_source"] = "gemini"
        triage["model_requested"] = req_model
        triage["model_served"] = served_model

        if attachment_only and result.category == "OTHER":
            saved = await self.repository.put("messages",event_id,{
                **clean_data,"state":"IGNORED_SOCIAL","reason":"MEDIA_NO_TRADE_SIGNAL","triage":triage,
                "current_stage": "AI_CLASSIFICATION", "stage_outcome": "IGNORED",
                "processing_stage": "Media without trade signal", "last_progress_at": now,
                "classification_source": "gemini", "model_requested": req_model, "model_served": served_model,
            },record["revision"])
            self._trace(event_id, "AI_CLASSIFICATION", "IGNORED", "MEDIA_NO_TRADE_SIGNAL",
                        conversation_id=conversation, message_revision=saved["revision"],
                        metadata={"model_requested": req_model, "model_served": served_model, **call_meta})
            await self._withdraw(event_id)
            return {"status":"IGNORED_SOCIAL","id":event_id,"sent":False}
        if attachment_only and (result.category not in ("SELL", "STOCK") or not result.proceed or not any(item.device.strip() and item.quantity is not None for item in result.items)):
            saved = await self.repository.put("messages",event_id,{
                **clean_data,"state":"NEEDS_REVIEW","reason":"MEDIA_STOCK_UNCONFIRMED","triage":triage,
                "current_stage": "AI_CLASSIFICATION", "stage_outcome": "BLOCKED",
                "processing_stage": "Media stock unconfirmed", "last_progress_at": now,
                "classification_source": "gemini", "model_requested": req_model, "model_served": served_model,
            },record["revision"])
            self._trace(event_id, "AI_CLASSIFICATION", "BLOCKED", "MEDIA_STOCK_UNCONFIRMED",
                        conversation_id=conversation, message_revision=saved["revision"],
                        metadata={"model_requested": req_model, "model_served": served_model, **call_meta})
            await self._withdraw(event_id)
            return {"status":"CAPTURED_REVIEW","id":event_id,"sent":False}
        if has_media and text in ("[Photo / Stock Image]", "[Media attachment]", "") and triage.get("items"):
            item_summary = ", ".join(f"{it.get('device')} {it.get('variant') or ''} x{it.get('quantity') or ''}".strip() for it in triage["items"][:3])
            clean_data["source_text"] = text
            clean_data["text"] = f"[Image OCR] {item_summary}"[:300]
        elif has_media and text in ("[Photo / Stock Image]", "") and triage.get("reason"):
            clean_data["source_text"] = text
            clean_data["text"] = f"[Image] {triage['reason']}"[:300]

        decision_protected = self._is_decision_protected(record["data"])
        ocr_failed = bool(record["data"].get("media_job", {}).get("status") in ("TIMEOUT", "FAILED"))

        is_injection = (result.reason == "PROMPT_INJECTION_DETECTED")
        if is_injection:
            outcome = "BLOCKED"
            reason_code = "PROMPT_INJECTION_DETECTED"
            stage_desc = "Security: Injection rejected"
        elif ocr_failed:
            outcome = "BLOCKED"
            reason_code = "MEDIA_TIMEOUT" if record["data"].get("media_job", {}).get("status") == "TIMEOUT" else "INVALID_MEDIA"
            stage_desc = "Held: Incomplete image inquiry (OCR failed)"
        else:
            outcome = "SUCCESS"
            reason_code = f"CLASSIFIED_{result.category}"
            stage_desc = f"Classified by {served_model}"

        trade_eval_pending = (result.proceed and not is_injection and not ocr_failed and not decision_protected) if self.local_first else False

        data = {
            **clean_data,
            "state": "IGNORED_SOCIAL" if is_injection else ("NEEDS_REVIEW" if ocr_failed else "CLASSIFIED"),
            "triage": triage,
            "classification_source": "gemini",
            "model_requested": req_model,
            "model_served": served_model,
            "current_stage": "AI_CLASSIFICATION",
            "stage_outcome": outcome,
            "reason": reason_code,
            "processing_stage": stage_desc,
            "last_progress_at": now,
            "trade_evaluation_pending": trade_eval_pending,
            **({"decision_protected": True} if decision_protected else {}),
        }
        if result.category in ("BUY", "MIXED") and result.proceed and not is_injection and not ocr_failed:
            data["buyer_pricing"] = await buyer_pricing(record["data"].get("phone"), triage)
        cur_rec = await self._get_record("messages", event_id)
        latest_rev = cur_rec["revision"] if cur_rec else record["revision"]
        saved = await self.repository.put("messages", event_id, data, latest_rev)
        self._trace(event_id, "AI_CLASSIFICATION", outcome, reason_code,
                    conversation_id=conversation, message_revision=saved["revision"],
                    metadata={"model_requested": req_model, "model_served": served_model, **call_meta})
        if is_injection or ocr_failed or decision_protected:
            if is_injection:
                await self._withdraw(event_id)
            return {"status": data["state"], "id": event_id, "sent": False}
        await self._intent(event_id, saved)
        return {"status": "CLASSIFIED", "id": event_id, "triage": result.model_dump(), "sent": False}

    async def _reserve_budget(self,setup):
        # Reserve before a paid call. A crash can consume a slot but cannot overspend it.
        key="classifier-budget-"+datetime.now(timezone.utc).strftime("%Y-%m-%d")
        limit=setup.get("classifier_daily_limit",500)
        for _ in range(4):
            current=await self._get_record("configuration",key)
            used=current["data"]["used"] if current else 0
            if used>=limit: return False
            try:
                await self.repository.put("configuration",key,{"used":used+1,"timezone":"UTC"},current["revision"] if current else 0)
                return True
            except SheetsError as error:
                if error.code!="REVISION_CONFLICT": raise
        return False

    async def _withdraw(self,event_id):
        for collection in ("requests","offers"):
            old=await self._get_record(collection,event_id)
            if old and old["data"].get("state")!="WITHDRAWN":
                await self.repository.put(collection,event_id,{**old["data"],"state":"WITHDRAWN"},old["revision"])

    async def _intent(self,event_id,record):
        triage=record["data"]["triage"]
        if record["data"].get("stock_file_text"):
            await self._withdraw(event_id)
            items = triage.get("items", [])
            if (triage["category"] not in ("SELL", "STOCK") or not triage["proceed"] or triage.get("needs_review")
                    or not items or any(not item.get("device") or not item.get("variant") or not item.get("quantity") for item in items)):
                return
            stamp = record["data"].get("timestamp")
            if not isinstance(stamp, (int, float)) or not 0 <= time.time() - stamp < 86400:
                return
            previous = await self._get_record("configuration", "daily-stock")
            if previous and previous["data"].get("source_timestamp", 0) >= stamp:
                return
            await self.repository.put("configuration", "daily-stock", {
                "source_message": event_id, "source_phone": record["data"]["phone"],
                "source_timestamp": stamp, "items": items,
            }, previous["revision"] if previous else 0)
            if self.on_change:
                self.on_change("inventory")
            return
        if triage["proceed"] and triage["category"] == "MIXED":
            buy_items = [it for it in triage.get("items", []) if it.get("intent") == "BUY"]
            sell_items = [it for it in triage.get("items", []) if it.get("intent") in ("SELL", "STOCK")]
            if not buy_items and not sell_items:
                buy_items = triage.get("items", [])
            if buy_items:
                old_req = await self._get_record("requests", event_id)
                data = {
                    "source_message": event_id, "source_revision": record["revision"],
                    "phone": record["data"]["phone"], "raw_text": record["data"]["text"],
                    "items": buy_items, "category": "BUY", "state": "NEEDS_REVIEW",
                    "owned_stock": False, "source_time_confirmed": False,
                }
                if record["data"].get("buyer_pricing"):
                    data["buyer_pricing"] = record["data"]["buyer_pricing"]
                await self.repository.put("requests", event_id, data, old_req["revision"] if old_req else 0)
            if sell_items:
                old_off = await self._get_record("offers", event_id)
                await self.repository.put("offers", event_id, {
                    "source_message": event_id, "source_revision": record["revision"],
                    "phone": record["data"]["phone"], "raw_text": record["data"]["text"],
                    "items": sell_items, "category": "SELL", "state": "NEEDS_REVIEW",
                    "owned_stock": False, "source_time_confirmed": False,
                }, old_off["revision"] if old_off else 0)
            return
        desired = None
        if triage["proceed"] and triage["category"] in ("BUY", "SELL", "STOCK"):
            desired = "requests" if triage["category"] == "BUY" else "offers"
        for collection in ("requests","offers"):
            old=await self._get_record(collection,event_id)
            if collection==desired:
                if old and old["data"].get("source_revision")==record["revision"]: continue
                data={"source_message":event_id,"source_revision":record["revision"],"phone":record["data"]["phone"],"raw_text":record["data"]["text"],"items":triage.get("items",[]),"category":triage["category"],"state":"NEEDS_REVIEW","owned_stock":False,"source_time_confirmed":False}
                if record["data"].get("buyer_pricing"):
                    data["buyer_pricing"]=record["data"]["buyer_pricing"]
                await self.repository.put(collection,event_id,data,old["revision"] if old else 0)
            elif old and old["data"].get("state")!="WITHDRAWN":
                await self.repository.put(collection,event_id,{**old["data"],"state":"WITHDRAWN"},old["revision"])
