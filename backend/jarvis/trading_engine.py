"""Autonomous B2B electronics trading brain: 1% triage, stock OCR, matcher, geo-scheduler, anti-slop quote, @Agent & banner/video."""
import base64
import hashlib
import io
import json
import random
import re
import subprocess
import time
import uuid
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Literal
import xml.etree.ElementTree as ET
import zipfile
from zoneinfo import ZoneInfo

import phonenumbers
from phonenumbers import timezone as phone_tz
from pydantic import BaseModel, ConfigDict, Field

from .fx import calculate_dynamic_quote
from .intelligence import DeskSettings, alert, settings as load_desk_settings


_TRADE_SIGNAL_RE = re.compile(
    r"\b(wtb|wts|want to buy|want to sell|wat to sale|stock|ready stock|offer|price|rate|quote|rfq|po|bom|mpn|hsn|active|non[\s-]*act(?:ive)?|activated|"
    r"jv(?:\s*lock)?|unlocked|gst|dafza|indian\s*spec|usa\s*spec|master\s*carto+n|pcs|pc|piece|pieces|units|qty|quantity|nos|nug|lot|packet|dabba|carton|moq|spq|lead\s*time|"
    r"chahiye|chahye|chahie|bhav|bhao|daam|dam|kya price|kya rate|rate do|rate chahiye|best rate|urgent|maal|stock hai|hoga kya|milega|"
    r"iphone|samsung|galaxy|pixel|oneplus|macbook|ipad|airpods|apple\s*watch|itel|redmi|xiaomi|vivo|oppo|realme|poco|nothing|lenovo|dell|hp|asus|acer|sony|playstation|xbox|nintendo|airtag|smartwatch|earbuds|"
    r"ic|ics|mcu|microcontroller|mosfet|mosfets|igbt|diode|diodes|transistor|transistors|capacitor|capacitors|resistor|resistors|inductor|inductors|pcb|pcba|smd|smt|dip|bga|qfp|qfn|soic|sop|tssop|sot|to-220|to-247|0402|0603|0805|1206|1210|"
    r"reel|tray|tube|tape|cut\s*tape|loose|bulk|module|sensor|relay|connector|adapter|charger|smps|inverter|display|oled|amoled|semiconductor|"
    r"stm32|esp32|esp8266|atmega|pic16|pic18|ne555|lm358|lm317|lm7805|tl072|max232|ch340|cp2102|ft232|irfz44n|2n2222|1n4007|1n4148|texas\s*instruments|stmicroelectronics|microchip|nxp|analog\s*devices|infineon|onsemi|murata|tdk|yageo|vishay|kemet|"
    r"usd|aed|inr|eur|gbp|\d{1,2}/\d{2,4}|\d{2,4}\s*(?:gb|tb)|\d+(?:\.\d+)?\s*(?:pf|nf|uf|µf|k|ohm|ohms|kohm|mohm|v|kv|ma|a|mhz|ghz))\b|"
    r"(?:/-|\$|₹|€|£|@\s*\d{2,})|\b[A-Z]{2,}\d{2,}[A-Z0-9-]*\b|\b\d{4}\s+[A-Z0-9]+\b",
    re.I,
)


class StockLineItem(BaseModel):
    model_config = ConfigDict(extra="ignore")
    brand: str = Field(default="", max_length=60)
    model: str = Field(min_length=1, max_length=120)
    category: str = Field(default="ELECTRONICS", max_length=60)
    part_number: str = Field(default="", max_length=80)
    specs: str = Field(default="", max_length=200)
    packaging: str = Field(default="", max_length=60)
    moq: int | None = Field(default=None, ge=1, le=10000000)
    warranty: str = Field(default="", max_length=60)
    warehouse: str = Field(default="", max_length=80)
    ram_gb: int | None = None
    storage_gb: int | None = None
    variant: str = Field(default="", max_length=120)
    color_breakdown: dict[str, int] = Field(default_factory=dict)
    condition: Literal["NON_ACTIVE", "ACTIVATED", "REFURB", "UNKNOWN"] = "UNKNOWN"
    lock_status: Literal["UNLOCKED", "JV_LOCK", "UNKNOWN"] = "UNKNOWN"
    region_spec: str = Field(default="GLOBAL", max_length=40)
    tax_status: Literal["WITH_GST", "WITHOUT_GST", "UNKNOWN"] = "UNKNOWN"
    quantity: int = Field(default=1, ge=0, le=10000000)
    unit_cost: float | None = Field(default=None, gt=0)
    currency: str = Field(default="INR", pattern=r"^[A-Z]{3}$")


class StockBatch(BaseModel):
    model_config = ConfigDict(extra="ignore")
    items: list[StockLineItem] = Field(default_factory=list, max_length=250)


def is_worth_effort(
    text: str,
    contact_category: str = "UNKNOWN",
    *,
    has_media: bool = False,
    media_type: str | None = None,
) -> tuple[bool, str]:
    """Ultra-fast local 1% pre-filter: isolates FAMILY/casual before spending LLM tokens."""
    if contact_category in ("FAMILY_PERSONAL", "IGNORED"):
        return False, "FAMILY_PERSONAL"
    cleaned = str(text or "").strip()
    is_raw_b64 = len(cleaned) > 100 and not re.search(r"\s", cleaned) and (
        cleaned.startswith(("/9j/", "iVBORw0KGgo", "R0lGOD", "UklGR")) or bool(re.fullmatch(r"[A-Za-z0-9+/=]{100,}", cleaned))
    )
    if has_media and str(media_type or "").lower() in ("image", "document", "pdf"):
        return True, "MEDIA_OCR_CHECK"
    if is_raw_b64:
        return False, "RAW_BASE64_IGNORED"
    if re.fullmatch(r"\s*(good\s+(morning|night|evening|afternoon)|hi+|hello+|hey+|thanks?|thank\s+you|ok+|okay+|gm|gn|kya\s+hal(?:\s*chal)?|kaise\s+ho|namaste|pranam|radhe\s*radhe|jai\s*jinendra|ram\s*ram|kaisa\s+hai)(?:\s+(bhai|bro|sir|ji|yaar|dost|all|team))?[\s!.,🌸🌺🙏👍❤️😊]*", cleaned, re.I):
        return False, "IGNORED_SOCIAL"
    if len(cleaned) < 3:
        return False, "TOO_SHORT"
    if _TRADE_SIGNAL_RE.search(cleaned):
        return True, "TRADE_SIGNAL"
    if cleaned.count("\n") >= 2 and any(ch.isdigit() for ch in cleaned):
        return True, "TRADE_SIGNAL"
    if re.search(r"\+?\d[\d\s-]{8,14}", cleaned) and len(cleaned) > 10:
        return True, "PHONE_SIGNAL"
    return False, "NO_TRADE_SIGNAL"


# libphonenumber still returns some legacy zone names. Windows has no system
# zone database and slim Linux images drop the "backward" aliases, so map them.
_LEGACY_ZONES = {
    "Asia/Calcutta": "Asia/Kolkata", "Asia/Katmandu": "Asia/Kathmandu", "Asia/Saigon": "Asia/Ho_Chi_Minh",
    "Asia/Rangoon": "Asia/Yangon", "Asia/Dacca": "Asia/Dhaka", "Asia/Thimbu": "Asia/Thimphu",
    "Asia/Ulan_Bator": "Asia/Ulaanbaatar", "Europe/Kiev": "Europe/Kyiv", "America/Buenos_Aires": "America/Argentina/Buenos_Aires",
    "Atlantic/Faeroe": "Atlantic/Faroe", "Pacific/Truk": "Pacific/Chuuk", "Pacific/Ponape": "Pacific/Pohnpei",
}


def resolve_buyer_timezone(phone: str | None) -> str:
    if phone:
        try:
            parsed = phonenumbers.parse(phone, None)
            zones = phone_tz.time_zones_for_number(parsed)
            if zones and zones[0] != "Etc/Unknown":
                name = str(zones[0])
                canonical = _LEGACY_ZONES.get(name, name)
                for candidate in (name, canonical):
                    try:
                        ZoneInfo(candidate)
                        return candidate
                    except Exception:
                        continue
                return canonical
        except Exception:
            pass
    return "UTC"


def evaluate_working_hours(
    phone: str | None,
    *,
    start_hhmm: str = "08:30",
    end_hhmm: str = "21:30",
    target_morning_hhmm: str = "08:45",
    now_utc: datetime | None = None,
) -> dict:
    tz_name = resolve_buyer_timezone(phone)
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz_name = "UTC"
        tz = timezone.utc
    now = (now_utc or datetime.now(timezone.utc)).astimezone(tz)
    sh, sm = (int(x) for x in start_hhmm.split(":"))
    eh, em = (int(x) for x in end_hhmm.split(":"))
    th, tm = (int(x) for x in target_morning_hhmm.split(":"))
    start_t = dtime(sh, sm)
    end_t = dtime(eh, em)
    local_t = now.time()
    within = (start_t <= local_t <= end_t) if start_t <= end_t else (local_t >= start_t or local_t <= end_t)
    if within:
        wake_dt = now
    else:
        target_date = now.date() if local_t < start_t else (now.date() + timedelta(days=1))
        target_time = dtime(th, tm)
        if not start_t <= target_time <= end_t:
            target_time = start_t
        wake_dt = datetime.combine(target_date, target_time, tzinfo=tz)
    return {
        "timezone": tz_name,
        "local_time_iso": now.isoformat(),
        "within_hours": within,
        "wake_at_ts": wake_dt.astimezone(timezone.utc).timestamp(),
        "wake_local_iso": wake_dt.isoformat(),
    }


async def parse_stock_vision(gemini, text: str, *, image_b64: str | None = None, mime_type: str | None = None) -> list[dict]:
    """Extract strict structured wholesale electronics stock from text or stock images/PDFs."""
    prompt = (
        "Extract wholesale electronics inventory items from this supplier message or stock sheet image. "
        "Capture Non active/Non Act -> NON_ACTIVE, ACTIVATED -> ACTIVATED, JV LOCK -> JV_LOCK, unlocked -> UNLOCKED, "
        "Indian Spec/DAFZA/USA Spec into region_spec, With Gst -> WITH_GST, Without Gst -> WITHOUT_GST. "
        "Capture color counts (e.g. Black-73, Lavender-10) in color_breakdown. Default currency INR for '/-' prices, USD for '$'. Return JSON only."
    )
    parts = [{"text": f"{prompt}\n\nTEXT:\n{(text or '')[:6000]}"}]
    if image_b64 and mime_type:
        parts.append({"inlineData": {"mimeType": mime_type, "data": image_b64}})
    if image_b64 and hasattr(gemini, "generate_multimodal"):
        raw = await gemini.generate_multimodal(prompt, parts, StockBatch.model_json_schema(), 2048)
    else:
        raw = await gemini.generate(prompt, (text or "")[:6000], StockBatch.model_json_schema(), 2048)
    batch = StockBatch.model_validate_json(raw)
    return [item.model_dump() for item in batch.items]


def _norm_tok(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(s or "").casefold())


def _is_region_compatible(buy_item: dict, stock_item: dict) -> bool:
    br = str(buy_item.get("region_spec") or "").upper().strip()
    sr = str(stock_item.get("region_spec") or "").upper().strip()
    if br and sr and br != "GLOBAL" and sr != "GLOBAL" and br != sr:
        return False
    return True


def _is_grade_compatible(buy_item: dict, stock_item: dict) -> bool:
    bl = str(buy_item.get("lock_status") or "UNKNOWN").upper().strip()
    sl = str(stock_item.get("lock_status") or "UNKNOWN").upper().strip()
    if bl in ("JV_LOCK", "UNLOCKED") and sl in ("JV_LOCK", "UNLOCKED") and bl != sl:
        return False
    if bl == "UNLOCKED" and "JV" in sl:
        return False
    if bl == "JV_LOCK" and "UNLOCK" in sl:
        return False

    bc = str(buy_item.get("condition") or "UNKNOWN").upper().strip()
    sc = str(stock_item.get("condition") or "UNKNOWN").upper().strip()
    new_grades = {"NON_ACTIVE", "NEW", "SEALED", "BRAND_NEW"}
    used_grades = {"ACTIVATED", "ACTIVE", "REFURB", "REFURBISHED", "USED", "A_GRADE", "B_GRADE", "C_GRADE"}
    if bc in new_grades and sc in used_grades:
        return False
    if bc in used_grades and sc in new_grades:
        return False
    if bc in ("NON_ACTIVE", "ACTIVATED") and sc in ("NON_ACTIVE", "ACTIVATED") and bc != sc:
        return False
    return True


async def find_best_stock_match(repo, buy_item: dict, *, offer_ttl_seconds: int = 86400) -> dict | None:
    """Universal 4-tier matcher: owned inventory -> supplier WTS offers -> daily stock -> sourcing alert."""
    want_mpn = _norm_tok(buy_item.get("part_number") or buy_item.get("mpn"))
    want_dev = _norm_tok(buy_item.get("device") or buy_item.get("model"))
    want_var = _norm_tok(buy_item.get("variant"))
    if not want_mpn and not want_dev:
        return None

    requested_qty = int(buy_item.get("quantity") or 1)
    inv_rows = await repo.list("inventory", 500)
    offer_rows = await repo.list("offers", 300)
    snapshot = await repo.get("configuration", "daily-stock")

    now_ts = time.time()
    candidates = []

    def _var_matches(w_var, c_var):
        if not w_var and not c_var:
            return True
        if not w_var:
            return True
        if not c_var:
            return False
        w_clean = re.sub(r"[^a-z0-9]", "", w_var)
        c_clean = re.sub(r"[^a-z0-9]", "", c_var)
        return (w_clean == c_clean) or (w_var in c_var or c_var in w_var)

    # Tier 1 & 1b: Owned inventory (B15: verified ownership)
    for row in inv_rows:
        d = row.get("data") or {}
        # Supplier stock must never be matched as owned inventory
        if d.get("source_kind") in ("supplier_wts", "supplier") or d.get("ownership") == "supplier" or d.get("supplier_phone"):
            continue
        qty = int(d.get("quantity") or 0)
        if qty <= 0 or not _is_grade_compatible(buy_item, d) or not _is_region_compatible(buy_item, d):
            continue
        cand_mpn = _norm_tok(d.get("part_number") or d.get("mpn"))
        cand_dev = _norm_tok(d.get("device") or d.get("model"))
        cand_var = _norm_tok(d.get("variant"))
        if want_mpn and cand_mpn and want_mpn != cand_mpn:
            continue
        is_exact = bool(want_mpn and cand_mpn and want_mpn == cand_mpn) or bool(
            want_dev and cand_dev == want_dev and _var_matches(want_var, cand_var)
        )
        if is_exact:
            score = 100 if qty >= requested_qty else 90
            candidates.append((score, "owned", row["id"], d, False))
        elif want_dev and (want_dev in cand_dev or cand_dev in want_dev):
            candidates.append((70, "owned_alt", row["id"], d, True))

    # Tier 2: Fresh supplier WTS offers (with TTL & unit_cost fix)
    for row in offer_rows:
        d = row.get("data") or {}
        if d.get("state") in ("WITHDRAWN", "NEEDS_REVIEW", "IGNORED"):
            continue
        if d.get("source_time_confirmed") is False:
            continue
        if hasattr(repo, "is_chat_muted") and d.get("phone"):
            p = str(d["phone"])
            if repo.is_chat_muted(p[1:] + "@c.us" if p.startswith("+") else p + "@c.us"):
                continue
        raw_ts = d.get("timestamp") or row.get("created_at")
        if not raw_ts:
            continue
        if isinstance(raw_ts, (int, float)):
            offer_time = float(raw_ts)
        elif isinstance(raw_ts, str):
            try:
                offer_time = datetime.fromisoformat(raw_ts.replace("Z", "+00:00")).timestamp()
            except Exception:
                try:
                    offer_time = float(raw_ts)
                except Exception:
                    continue
        else:
            continue
        if offer_time > now_ts + 300 or now_ts - offer_time > offer_ttl_seconds:
            continue
        for item in d.get("items") or []:
            qty = int(item.get("quantity") or 0)
            if qty <= 0 or not _is_grade_compatible(buy_item, item) or not _is_region_compatible(buy_item, item):
                continue
            cand_mpn = _norm_tok(item.get("part_number") or item.get("mpn"))
            cand_dev = _norm_tok(item.get("device") or item.get("model"))
            cand_var = _norm_tok(item.get("variant"))
            if want_mpn and cand_mpn and want_mpn != cand_mpn:
                continue
            cost = item.get("unit_cost") or item.get("unit_price")
            item_payload = {**item, "unit_cost": cost, "supplier_phone": d.get("phone")}
            is_exact = bool(want_mpn and cand_mpn and want_mpn == cand_mpn) or bool(
                want_dev and cand_dev == want_dev and _var_matches(want_var, cand_var)
            )
            if is_exact:
                score = 85 if qty >= requested_qty else 75
                candidates.append((score, "supplier_wts", row["id"], item_payload, False))
            elif want_dev and (want_dev in cand_dev or cand_dev in want_dev):
                candidates.append((55, "supplier_alt", row["id"], item_payload, True))

    # Daily stock snapshot
    if snapshot and 0 <= now_ts - snapshot["data"].get("source_timestamp", 0) < 86400:
        for item in snapshot["data"].get("items") or []:
            qty = int(item.get("quantity") or 0)
            if qty <= 0 or not _is_grade_compatible(buy_item, item) or not _is_region_compatible(buy_item, item):
                continue
            cand_mpn = _norm_tok(item.get("part_number") or item.get("mpn"))
            cand_dev = _norm_tok(item.get("device") or item.get("model"))
            cand_var = _norm_tok(item.get("variant"))
            if want_mpn and cand_mpn and want_mpn != cand_mpn:
                continue
            cost = item.get("unit_cost") or item.get("unit_price")
            item_payload = {**item, "unit_cost": cost}
            is_exact = bool(want_mpn and cand_mpn and want_mpn == cand_mpn) or bool(
                want_dev and cand_dev == want_dev and _var_matches(want_var, cand_var)
            )
            if is_exact:
                score = 95 if qty >= requested_qty else 80
                candidates.append((score, "daily_stock", snapshot["id"], item_payload, False))

    if candidates:
        candidates.sort(key=lambda c: (not c[4], int(int(c[3].get("quantity") or 0) >= requested_qty), c[0], int(c[3].get("quantity") or 0)), reverse=True)
        score, source_kind, row_id, item, is_suggestion = candidates[0]
        avail_qty = int(item.get("quantity") or 0)
        is_partial = avail_qty < requested_qty
        quantity_gap = max(0, requested_qty - avail_qty)
        result = {
            "score": score,
            "source_kind": source_kind,
            "row_id": row_id,
            "item": item,
            "is_exact": score >= 85 and not is_suggestion,
            "is_partial": is_partial,
            "shortage_status": "PARTIAL_SHORTAGE" if is_partial else "FULL_STOCK",
            "available_quantity": avail_qty,
            "requested_quantity": requested_qty,
            "quantity_gap": quantity_gap,
            "is_suggestion": is_suggestion,
        }
        if is_partial and quantity_gap > 0 and hasattr(repo, "put"):
            item_label = buy_item.get("part_number") or buy_item.get("device") or buy_item.get("model") or "Item"
            norm_part = want_mpn or want_dev or "item"
            source_seed = buy_item.get("source_id") or buy_item.get("message_id") or buy_item.get("conversation_id") or ""
            gap_alert_id = "sourcing-gap-" + hashlib.sha256(f"{norm_part}:{quantity_gap}:{source_seed}".encode()).hexdigest()[:16]
            try:
                await repo.put(
                    "tasks",
                    gap_alert_id,
                    {
                        "kind": "notification",
                        "category": "sourcing_need",
                        "title": f"Shortage · {item_label} (need {quantity_gap} more pcs)",
                        "body": f"Buyer requested {requested_qty} pcs of {item_label}. {avail_qty} in stock, gap of {quantity_gap} pcs.",
                        "details": {"buy_item": buy_item, "available_quantity": avail_qty, "quantity_gap": quantity_gap},
                        "state": "NEW",
                        "created_at": datetime.now(timezone.utc).isoformat(),
                        "notified_at": None,
                        "delivery_state": "UNNOTIFIED",
                        "delivery_error": None,
                        "read_at": None,
                        "resolved_at": None,
                        "dismissed_at": None,
                        "archived_at": None,
                        "resolution_reason": None,
                        "sheet_status": "none",
                        "sheet_synced_at": None,
                        "sheet_error": None,
                        "sourcing_task": None,
                        "availability_outbound_id": None,
                        "availability_status": "NONE",
                        "buyer_notified": False,
                        "buyer_notified_at": None,
                    },
                    0,
                )
            except Exception:
                pass
        if source_kind == "daily_stock":
            result["snapshot_revision"] = snapshot["revision"]
            result["snapshot_timestamp"] = snapshot["data"]["source_timestamp"]
        return result

    # Tier 4: Sourcing needed
    item_label = buy_item.get("part_number") or buy_item.get("device") or buy_item.get("model") or "Item"
    if hasattr(repo, "put"):
        norm_part = want_mpn or want_dev or "item"
        source_seed = buy_item.get("source_id") or buy_item.get("message_id") or buy_item.get("conversation_id") or ""
        alert_id = "sourcing-" + hashlib.sha256(f"{norm_part}:{requested_qty}:{source_seed}".encode()).hexdigest()[:16]
        try:
            await repo.put(
                "tasks",
                alert_id,
                {
                    "kind": "notification",
                    "category": "sourcing_need",
                    "title": f"Sourcing Need · {item_label} ({requested_qty} pcs)",
                    "body": f"Buyer requested {requested_qty} pcs of {item_label}. 0 in stock, 0 active supplier offers.",
                    "details": {"buy_item": buy_item, "requested_quantity": requested_qty},
                    "state": "NEW",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "notified_at": None,
                    "delivery_state": "UNNOTIFIED",
                    "delivery_error": None,
                    "read_at": None,
                    "resolved_at": None,
                    "dismissed_at": None,
                    "archived_at": None,
                    "resolution_reason": None,
                    "sheet_status": "none",
                    "sheet_synced_at": None,
                    "sheet_error": None,
                    "sourcing_task": None,
                    "availability_outbound_id": None,
                    "availability_status": "NONE",
                    "buyer_notified": False,
                    "buyer_notified_at": None,
                },
                0,
                alert_id,
            )
        except Exception:
            pass

    return {
        "score": 0,
        "source_kind": "sourcing_needed",
        "row_id": None,
        "item": None,
        "is_exact": False,
        "is_partial": False,
        "is_suggestion": False,
        "sourcing_needed": True,
        "available_quantity": 0,
        "requested_quantity": requested_qty,
        "quantity_gap": requested_qty,
    }


def compose_dealer_quote(buy_item: dict, match: dict, pricing: dict | None = None) -> list[str]:
    """Anti-slop B2B wholesale 2-bubble quote formatter (zero internal supplier leaks)."""
    stock = match.get("item") or {}
    mpn = stock.get("part_number") or stock.get("mpn") or buy_item.get("part_number") or buy_item.get("mpn")
    if mpn:
        pkg = stock.get("packaging") or buy_item.get("packaging")
        header = f"{mpn}{(' (' + str(pkg) + ')') if pkg else ''}"
    else:
        name = str(stock.get("device") or stock.get("model") or buy_item.get("device") or "Device").strip()
        var = str(stock.get("variant") or buy_item.get("variant") or "").strip()
        cond = str(stock.get("condition") or "").upper()
        cond_tag = " Non Act" if cond == "NON_ACTIVE" else (" Act" if cond == "ACTIVATED" else "")
        lock_tag = " JV Lock" if str(stock.get("lock_status") or "").upper() == "JV_LOCK" else ""
        header = f"{name}{(' ' + var) if var else ''}{cond_tag}{lock_tag}".strip()

    colors = stock.get("color_breakdown") if isinstance(stock.get("color_breakdown"), dict) else {}
    if colors:
        color_line = ", ".join(f"{k}-{v}" for k, v in colors.items())
        line1 = f"{header}\n{color_line}"
    elif match.get("is_partial"):
        avail = match.get("available_quantity")
        req = match.get("requested_quantity")
        line1 = f"{header} · {avail} pcs ready (of {req} requested)"
    else:
        qty = stock.get("quantity")
        line1 = f"{header} · {qty} pcs ready" if qty else header

    if match.get("is_suggestion") or (not match.get("is_exact") and match.get("source_kind") not in ("supplier_wts", "daily_stock")):
        line1 = f"Alt available:\n{line1}"

    parts = [line1]
    if pricing and pricing.get("quote_usd"):
        usd = pricing["quote_usd"]
        tccy = pricing.get("target_currency") or "USD"
        if tccy != "USD" and pricing.get("quote_local"):
            parts.append(f"{usd}$ (~{pricing['quote_local']} {tccy})")
        else:
            parts.append(f"{usd}$")
    return parts


def compose_multi_item_quote(evaluations: list[dict]) -> list[str]:
    """Compose a clean, professional multi-item quotation without conversational fluff."""
    if not evaluations:
        return ["No matching stock available currently."]
    if len(evaluations) == 1:
        ev = evaluations[0]
        return compose_dealer_quote(ev["buy_item"], ev["match"], ev.get("pricing"))

    lines = ["Available Quote:"]
    total_usd = 0.0
    for idx, ev in enumerate(evaluations, 1):
        buy_item = ev["buy_item"]
        match = ev["match"]
        pricing = ev.get("pricing")
        stock = match.get("item") or {}
        mpn = stock.get("part_number") or stock.get("mpn") or buy_item.get("part_number") or buy_item.get("mpn")
        if mpn:
            pkg = stock.get("packaging") or buy_item.get("packaging")
            item_desc = f"{mpn}{(' (' + str(pkg) + ')') if pkg else ''}"
        else:
            dev = stock.get("device") or stock.get("model") or buy_item.get("device") or "Item"
            var = stock.get("variant") or buy_item.get("variant") or ""
            cond = str(stock.get("condition") or "").upper()
            cond_tag = " Non Act" if cond == "NON_ACTIVE" else (" Act" if cond == "ACTIVATED" else "")
            lock_tag = " JV Lock" if str(stock.get("lock_status") or "").upper() == "JV_LOCK" else ""
            item_desc = f"{dev}{(' ' + var) if var else ''}{cond_tag}{lock_tag}".strip()

        prefix = "Alt available: " if (match.get("is_suggestion") or not match.get("is_exact")) else ""
        if match.get("is_partial"):
            avail = match.get("available_quantity")
            req = match.get("requested_quantity")
            qty_str = f"{avail} pcs ready (of {req} req)"
        else:
            req_qty = int(buy_item.get("quantity") or 1)
            avail_qty = int(stock.get("quantity")) if stock.get("quantity") is not None else req_qty
            qty_str = f"{min(avail_qty, req_qty) if avail_qty > 0 else req_qty} pcs ready"

        price_str = ""
        if pricing and pricing.get("quote_usd"):
            usd = pricing["quote_usd"]
            tccy = pricing.get("target_currency") or "USD"
            if tccy != "USD" and pricing.get("quote_local"):
                price_str = f" · ${usd} (~{pricing['quote_local']} {tccy})"
            else:
                price_str = f" · ${usd}"
            try:
                req_qty = int(buy_item.get("quantity") or 1)
                avail_qty = int(stock.get("quantity")) if stock.get("quantity") is not None else req_qty
                item_qty = min(avail_qty, req_qty) if avail_qty > 0 else req_qty
                total_usd += float(usd) * item_qty
            except Exception:
                pass
        elif match.get("sourcing_needed"):
            price_str = " · Sourcing in progress"

        lines.append(f"{idx}. {prefix}{item_desc} · {qty_str}{price_str}")

    bubble1 = "\n".join(lines)
    parts = [bubble1]
    if total_usd > 0:
        parts.append(f"Total: ${total_usd:,.2f} USD · Ready for dispatch")
    return parts


def generate_promo_banner_png(title: str, subtitle: str, highlight: str, out_dir: Path) -> dict:
    """Render a crisp 1200x675 B2B wholesale promotional banner locally via Pillow."""
    from PIL import Image, ImageDraw, ImageFont

    out_dir.mkdir(parents=True, exist_ok=True)
    W, H = 1200, 675
    img = Image.new("RGB", (W, H), (14, 23, 42))
    draw = ImageDraw.Draw(img)

    # Festive warm gold + royal navy diagonal bands
    for y in range(H):
        r = int(14 + (y / H) * 26)
        g = int(23 + (y / H) * 18)
        b = int(42 + (y / H) * 32)
        draw.line([(0, y), (W, y)], fill=(r, g, b))
    draw.rounded_rectangle([36, 36, W - 36, H - 36], radius=28, outline=(245, 196, 81), width=4)
    draw.rounded_rectangle([60, 60, W - 60, H - 60], radius=22, outline=(99, 102, 241), width=2)

    try:
        font_lg = ImageFont.truetype("arialbd.ttf", 64)
        font_md = ImageFont.truetype("arial.ttf", 34)
        font_sm = ImageFont.truetype("arialbd.ttf", 28)
    except Exception:
        font_lg = ImageFont.load_default()
        font_md = ImageFont.load_default()
        font_sm = ImageFont.load_default()

    draw.text((100, 120), (title or "FESTIVE WHOLESALE DEALS")[:46], fill=(252, 211, 77), font=font_lg)
    draw.text((100, 230), (subtitle or "Ready Bulk Stock · India · DAFZA · USA")[:68], fill=(241, 245, 249), font=font_md)
    draw.rounded_rectangle([100, 340, W - 100, 500], radius=18, fill=(30, 41, 59), outline=(245, 196, 81), width=2)
    draw.text((130, 392), (highlight or "DM for Today's Live Rate Sheet")[:56], fill=(56, 189, 248), font=font_md)
    draw.text((100, 560), "Verified Wholesale Desk · Instant Dispatch", fill=(148, 163, 184), font=font_sm)

    fname = f"banner-{uuid.uuid4().hex[:10]}.png"
    target = out_dir / fname
    img.save(target, format="PNG", optimize=True)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    data_url = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    return {"path": str(target), "filename": fname, "data_url": data_url}


async def summarize_video_url(gemini, url_or_text: str) -> str:
    """Extract video metadata/subtitles via yt-dlp if available (or URL fetch) and produce crisp bullet points."""
    urls = re.findall(r"https?://\S+", url_or_text or "")
    context_text = url_or_text or ""
    if urls:
        target_url = urls[0]
        try:
            proc = subprocess.run(
                ["yt-dlp", "--dump-single-json", "--no-download", "--skip-download", target_url],
                capture_output=True, text=True, timeout=15,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                meta = json.loads(proc.stdout)
                context_text = f"Title: {meta.get('title','')}\nChannel: {meta.get('uploader','')}\nDescription: {(meta.get('description') or '')[:3500]}"
        except Exception:
            pass
    prompt = "Summarize this video content into 4 to 6 crisp, actionable bullet pointers for an electronics trader."
    return await gemini.generate(prompt, context_text[:6000], max_tokens=700)


async def evaluate_inbound_trade(repo, vault, event: dict, setup: dict, *, triage: dict | None = None) -> dict:
    """Evaluate a classified WTB/WTS/MIXED event: match stock for ALL items, compute FX + aa% quotes, and route to Night Queue or Outbound."""
    cfg = await load_desk_settings(repo)
    autonomy = cfg.get("autonomy_mode", "observe")
    if autonomy == "observe":
        return {"action": "OBSERVE_ONLY"}
    if autonomy == "send":
        return {"action": "MANUAL_SEND_ONLY"}
    conv_id = event.get("conversation_id") or ""
    if hasattr(repo, "is_chat_muted") and repo.is_chat_muted(conv_id):
        return {"action": "MUTED_BY_HUMAN_OR_CATEGORY"}
    if not triage or not triage.get("proceed"):
        return {"action": "NO_PROCEED"}

    cat = str(triage.get("category") or "").upper()
    if cat not in ("BUY", "MIXED"):
        return {"action": "LOGGED_NON_BUY"}
    if triage.get("needs_review"):
        return {"action": "TRADE_NEEDS_REVIEW"}

    raw_items = triage.get("items") or []
    if cat == "BUY":
        buy_items = raw_items
    else:  # MIXED intent
        buy_items = [it for it in raw_items if str(it.get("direction") or it.get("intent") or "BUY").upper() == "BUY"]
    if not buy_items:
        return {"action": "NO_BUY_ITEMS"}

    news_rec = await repo.get("news", "latest")
    aa_pct = float((news_rec or {}).get("data", {}).get("aa_pct") or 0.0)

    evaluations = []
    has_any_match = False
    needs_price_review = False
    total_quote_usd = 0.0

    for item in buy_items:
        match = await find_best_stock_match(repo, item)
        if not match or match.get("sourcing_needed"):
            evaluations.append({"buy_item": item, "match": match or {"sourcing_needed": True, "score": 0}, "pricing": None})
            needs_price_review = True
            continue

        has_any_match = True
        stock = match.get("item") or {}
        pricing = None
        unit_cost = stock.get("unit_cost") or stock.get("unit_price")
        currency = stock.get("currency") or "INR"
        if unit_cost:
            try:
                pricing = await calculate_dynamic_quote(
                    unit_cost,
                    currency,
                    event.get("phone"),
                    margin_pct=cfg.get("default_margin_pct", 3.5),
                    aa_pct=aa_pct,
                    explicit_currency=triage.get("reply_currency"),
                )
                if pricing and pricing.get("quote_usd"):
                    qty = int(item.get("quantity") or 1)
                    total_quote_usd += float(pricing["quote_usd"]) * qty
                else:
                    needs_price_review = True
            except Exception:
                pricing = None
                needs_price_review = True
        else:
            needs_price_review = True

        evaluations.append({"buy_item": item, "match": match, "pricing": pricing})

    if not has_any_match:
        return {"action": "NO_STOCK_MATCH", "evaluations": evaluations}

    matched_facts = []
    for ev in evaluations:
        buy_item = ev["buy_item"]
        match = ev["match"]
        stock = match.get("item") or {}
        pricing = ev.get("pricing")
        req_qty = int(buy_item.get("quantity") or 1)
        avail_qty = int(stock.get("quantity") or 0)
        quoted_qty = min(avail_qty, req_qty) if avail_qty > 0 else req_qty
        matched_facts.append({
            "fact_id": str(match.get("row_id") or "fact_1"),
            "product_name": stock.get("device") or stock.get("model") or stock.get("part_number") or buy_item.get("device") or "Item",
            "variant": stock.get("variant") or buy_item.get("variant") or "",
            "condition": stock.get("condition") or buy_item.get("condition") or "",
            "lock_status": stock.get("lock_status") or buy_item.get("lock_status") or "",
            "packaging": stock.get("packaging") or buy_item.get("packaging") or "",
            "available_qty": avail_qty,
            "quoted_qty": quoted_qty,
            "unit_price_usd": pricing.get("quote_usd") if pricing else None,
            "target_currency": pricing.get("target_currency") if pricing else "USD",
            "quote_local": pricing.get("quote_local") if pricing else None,
            "is_alt": bool(match.get("is_suggestion")),
            "is_partial": bool(match.get("is_partial")),
            "source_kind": match.get("source_kind", "owned"),
        })

    text_parts = None
    reply_meta = {}
    llm_reply_generated = False
    llm_reply_error = None

    if vault and not needs_price_review:
        from .gemini import GeminiClient
        transport = getattr(repo, "gemini_transport", None) or getattr(vault, "gemini_transport", None)
        gemini = GeminiClient(vault, transport=transport) if transport is not None else GeminiClient(vault)
        st = gemini.status()
        is_gemini_configured = bool(st.get("configured") and (st.get("paused_until", 0) <= time.time() or st.get("active_key_count", 0) > 0))
        if is_gemini_configured and hasattr(gemini, "generate_quote_reply"):
            try:
                facts_payload = {
                    "conversation_id": conv_id,
                    "phone": event.get("phone"),
                    "inquiry": buy_items[0] if len(buy_items) == 1 else {"items_count": len(buy_items)},
                    "matched_facts": matched_facts,
                }
                quote_reply_obj = await gemini.generate_quote_reply(facts_payload)
                reply_text = quote_reply_obj.reply_text.strip()
                # Fact validation gate
                is_valid = True
                for mf in matched_facts:
                    usd_val = mf.get("unit_price_usd")
                    local_val = mf.get("quote_local")
                    if usd_val:
                        usd_int = usd_val.split(".")[0]
                        if usd_int not in reply_text and (not local_val or local_val.split(".")[0] not in reply_text):
                            is_valid = False
                            break
                if "Supplier offer to confirm:" in reply_text:
                    is_valid = False

                if is_valid:
                    text_parts = [reply_text]
                    llm_reply_generated = True
                    reply_meta = {
                        "model_requested": quote_reply_obj.model_requested,
                        "model_served": quote_reply_obj.model_served,
                        "call_metadata": quote_reply_obj.call_metadata,
                    }
                else:
                    llm_reply_error = "FACT_VALIDATION_MISMATCH"
            except Exception as ex:
                llm_reply_error = str(ex)

    if not text_parts:
        text_parts = compose_multi_item_quote(evaluations) if len(evaluations) > 1 else compose_dealer_quote(evaluations[0]["buy_item"], evaluations[0]["match"], evaluations[0].get("pricing"))

    # Autonomy status determination
    hold_reason = None
    max_usd = float(cfg.get("auto_reply_max_usd", 5000.0))
    if needs_price_review:
        target_status = "NEEDS_PRICE_REVIEW"
        hold_reason = "NEEDS_PRICE_REVIEW"
    elif autonomy in ("turbo", "auto"):
        if 0 < total_quote_usd <= max_usd:
            target_status = "READY"
        else:
            target_status = "DRAFT"
            hold_reason = "CAP_EXCEEDED"
    elif autonomy == "draft":
        target_status = "DRAFT"
        hold_reason = "DRAFT_HELD"
    else:
        target_status = "DRAFT"
        hold_reason = "DRAFT_HELD"

    wh = evaluate_working_hours(
        event.get("phone"),
        start_hhmm=cfg.get("working_hours_start", "08:30"),
        end_hhmm=cfg.get("working_hours_end", "21:30"),
    )
    if not wh["within_hours"] and hasattr(repo, "enqueue_scheduled_job"):
        job_id = "sched-" + hashlib.sha256(f"{conv_id}:{event.get('message_id')}".encode()).hexdigest()[:16]
        repo.enqueue_scheduled_job(
            job_id,
            conv_id,
            event.get("phone"),
            wh["timezone"],
            wh["wake_at_ts"],
            {
                "buy_item": buy_items[0],
                "match": evaluations[0]["match"],
                "evaluations": evaluations,
                "explicit_currency": triage.get("reply_currency"),
                "target_status": target_status,
                "text_parts": text_parts,
            },
            recalculate_fx=True,
        )
        return {"action": "SCHEDULED_NIGHT_HOLD", "job_id": job_id, "timezone": wh["timezone"], "wake_local": wh["wake_local_iso"], "hold_reason": "OUT_OF_HOURS"}

    if hasattr(repo, "queue_outbound"):
        owner_phone = (setup or {}).get("owner_phone")
        if not owner_phone and hasattr(repo, "get_local_state"):
            prof = repo.get_local_state("owner_profile")
            if isinstance(prof, dict):
                owner_phone = prof.get("owner_phone") or prof.get("phone")
        oid = repo.queue_outbound(
            conv_id,
            text_parts,
            kind="quote",
            recipient_phone=event.get("phone"),
            account_phone=owner_phone,
            status=target_status,
            delay_ms=random.randint(1500, 4200),
            meta={
                "device": buy_items[0].get("device") or buy_items[0].get("part_number"),
                "pricing": evaluations[0].get("pricing"),
                "total_quote_usd": total_quote_usd,
                "needs_price_review": needs_price_review,
                "evaluations_count": len(evaluations),
                "source_message": event.get("message_id"),
                "snapshot_revision": evaluations[0]["match"].get("snapshot_revision"),
                "hold_reason": hold_reason,
                "reply_meta": reply_meta,
                "llm_reply_generated": llm_reply_generated,
            },
            dedupe_key="buyer-quote:" + conv_id + ":" + str(event.get("message_id")),
        )
        return {
            "action": "QUEUED_" + target_status,
            "outbound_id": oid,
            "parts": text_parts,
            "target_status": target_status,
            "hold_reason": hold_reason,
            "total_quote_usd": total_quote_usd,
            "max_usd": max_usd,
            "reply_meta": reply_meta,
            "llm_reply_generated": llm_reply_generated,
            "llm_reply_error": llm_reply_error,
        }
    return {
        "action": "DRAFT_READY",
        "parts": text_parts,
        "target_status": target_status,
        "hold_reason": hold_reason,
        "total_quote_usd": total_quote_usd,
        "max_usd": max_usd,
        "reply_meta": reply_meta,
        "llm_reply_generated": llm_reply_generated,
        "llm_reply_error": llm_reply_error,
    }


async def handle_agent_command(repo, vault, command_text: str, conversation_id: str, *, runtime_dir: Path, toolbox=None, agent_name: str = "Jarvis", owner_phone: str | None = None) -> dict:
    """Execute @Agent self-chat command from WhatsApp (banners, videos, broadcasts, live tools)."""
    from .gemini import GeminiClient
    from .workspace import assistant

    if not owner_phone and hasattr(repo, "get_local_state"):
        prof = repo.get_local_state("owner_profile")
        if isinstance(prof, dict):
            owner_phone = prof.get("owner_phone") or prof.get("phone")
        if not owner_phone:
            setup_cfg = repo.get_local_state("setup_config")
            if isinstance(setup_cfg, dict):
                owner_phone = setup_cfg.get("owner_phone")

    raw = re.sub(rf"^@(?:agent|{re.escape(agent_name)})\b[:\s-]*", "", str(command_text or ""), flags=re.I).strip()
    lower = raw.lower()
    gemini = GeminiClient(vault)

    # 1. Confirm pending broadcast campaign ("@Agent SEND")
    if lower in ("send", "send broadcast", "confirm send"):
        pending = await repo.get("tasks", "pending-banner-campaign")
        if not pending or pending["data"].get("state") != "AWAITING_CONFIRM":
            reply = "No pending banner broadcast waiting. Ask me to create a banner first."
        else:
            d = pending["data"]
            recipients = d.get("recipients") or []
            queued = 0
            if hasattr(repo, "queue_outbound"):
                for i, rec in enumerate(recipients):
                    repo.queue_outbound(
                        rec["conversation_id"],
                        [d.get("caption") or "Fresh Stock & Festive Rates Available."],
                        kind="banner_broadcast",
                        recipient_phone=rec.get("phone"),
                        account_phone=owner_phone,
                        status="READY",
                        delay_ms=25000 + i * random.randint(25000, 55000),
                        meta={"attachment_data_url": d.get("data_url"), "attachment_name": d.get("filename"), "attachment_type": "image"},
                    )
                    queued += 1
            await repo.put("tasks", "pending-banner-campaign", {**d, "state": "DISPATCHING", "queued": queued}, pending["revision"])
            reply = f"Launching throttled anti-ban broadcast to {queued} verified trade contacts (25-55s jitter)."
        if hasattr(repo, "queue_outbound"):
            repo.queue_outbound(conversation_id, [reply], kind="agent_reply", status="READY", account_phone=owner_phone, recipient_phone=owner_phone)
        return {"handled": "broadcast_send", "reply": reply}

    # 2. Banner creation + preview ("make a Diwali banner and send to contacts from last 5 days")
    if "banner" in lower:
        m_days = re.search(r"(\d+)\s*day", lower)
        days = int(m_days.group(1)) if m_days else 5
        title = "DIWALI WHOLESALE SPECIAL" if "diwali" in lower else f"{agent_name.upper()} TRADE OFFER"
        banner = generate_promo_banner_png(title, "Ready Bulk Stock · India · DAFZA · USA", "Message for Live Wholesale Sheet", runtime_dir / "banners")
        contacts = repo.list_contacts(days=days) if hasattr(repo, "list_contacts") else []
        safe_targets = [
            {"conversation_id": c["conversation_id"], "phone": c.get("phone"), "name": c.get("name")}
            for c in contacts
            if c.get("category") not in ("FAMILY_PERSONAL", "IGNORED") and not str(c.get("conversation_id", "")).endswith("@g.us")
        ]
        prev = await repo.get("tasks", "pending-banner-campaign")
        await repo.put(
            "tasks",
            "pending-banner-campaign",
            {
                "state": "AWAITING_CONFIRM",
                "filename": banner["filename"],
                "path": banner["path"],
                "data_url": banner["data_url"],
                "caption": "Happy Diwali! Fresh wholesale electronics stock ready. Ping for today's live sheet.",
                "recipients": safe_targets,
                "days": days,
            },
            prev["revision"] if prev else 0,
        )
        reply = f"Banner cooked ({banner['filename']}). Found {len(safe_targets)} trade contacts from last {days} days (Family & Groups excluded). Reply '@Agent SEND' to launch."
        if hasattr(repo, "queue_outbound"):
            repo.queue_outbound(
                conversation_id,
                [reply],
                kind="agent_preview",
                status="READY",
                account_phone=owner_phone,
                recipient_phone=owner_phone,
                meta={"attachment_data_url": banner["data_url"], "attachment_name": banner["filename"], "attachment_type": "image", "caption": reply},
            )
        return {"handled": "banner_preview", "reply": reply, "recipients": len(safe_targets)}

    # 3. Video pointers ("watch this video ... and make pointers")
    if ("video" in lower or "youtube.com" in lower or "youtu.be" in lower) and ("pointer" in lower or "summar" in lower or "watch" in lower):
        summary = await summarize_video_url(gemini, raw)
        if hasattr(repo, "queue_outbound"):
            repo.queue_outbound(conversation_id, [summary], kind="agent_reply", status="READY", account_phone=owner_phone, recipient_phone=owner_phone)
        return {"handled": "video_summary", "reply": summary}

    # 4. General Jarvis workspace & DB assistant command
    req_id = "wa-agent-" + uuid.uuid4().hex[:16]
    res = await assistant(repo, vault, raw, req_id, page="whatsapp", chat_id="whatsapp-self", toolbox=toolbox)
    reply = res.get("answer") or "Done."
    if hasattr(repo, "queue_outbound"):
        repo.queue_outbound(conversation_id, [reply], kind="agent_reply", status="READY", account_phone=owner_phone, recipient_phone=owner_phone)
    return {"handled": "assistant_command", "reply": reply}


def extract_spreadsheet_text(raw_bytes: bytes, file_name: str = "", mime_type: str = "") -> tuple[str, str]:
    """Extract clean row-wise text from .xlsx workbooks or CSV/TSV/TXT/JSON spreadsheets."""
    lower = (file_name or "").lower()
    if lower.endswith(".xlsx") or "spreadsheetml" in (mime_type or "").lower() or raw_bytes[:2] == b"PK":
        try:
            with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
                entries = zf.infolist()
                if len(entries) > 200 or sum(entry.file_size for entry in entries) > 8_000_000:
                    raise ValueError("Workbook expands beyond the import limit")
                shared: list[str] = []
                if "xl/sharedStrings.xml" in zf.namelist():
                    root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
                    for si in root.iter():
                        if si.tag.endswith("}si") or si.tag == "si":
                            shared.append("".join(t.text or "" for t in si.iter() if (t.tag.endswith("}t") or t.tag == "t")))
                sheet_names = sorted(n for n in zf.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml$", n))
                if not sheet_names:
                    raise ValueError("No worksheets inside xlsx")
                out_lines: list[str] = []
                for sname in sheet_names[:5]:
                    sroot = ET.fromstring(zf.read(sname))
                    out_lines.append(f"=== {sname.rsplit('/', 1)[-1]} ===")
                    for row_el in sroot.iter():
                        if not (row_el.tag.endswith("}row") or row_el.tag == "row"):
                            continue
                        cells: list[str] = []
                        for c_el in row_el:
                            if not (c_el.tag.endswith("}c") or c_el.tag == "c"):
                                continue
                            ctype = c_el.attrib.get("t", "")
                            val = ""
                            if ctype == "inlineStr":
                                val = "".join(t.text or "" for t in c_el.iter() if (t.tag.endswith("}t") or t.tag == "t"))
                            else:
                                v_el = next((ch for ch in c_el if (ch.tag.endswith("}v") or ch.tag == "v")), None)
                                if v_el is not None and v_el.text is not None:
                                    raw_v = v_el.text.strip()
                                    if ctype == "s" and raw_v.isdigit() and int(raw_v) < len(shared):
                                        val = shared[int(raw_v)]
                                    else:
                                        val = raw_v
                            if val:
                                cells.append(val.replace("\n", " ").strip())
                        if cells:
                            out_lines.append(" | ".join(cells))
                return "\n".join(out_lines)[:24000], "xlsx"
        except (zipfile.BadZipFile, ET.ParseError, RuntimeError) as error:
            raise ValueError("Invalid Excel workbook") from error
    text = raw_bytes.decode("utf-8", errors="ignore") or raw_bytes.decode("latin-1", errors="ignore")
    fmt = "csv" if lower.endswith(".csv") else ("json" if lower.endswith(".json") else "text")
    return text[:24000], fmt


async def parse_inventory_any_format(
    gemini,
    *,
    raw_text: str = "",
    file_b64: str | None = None,
    file_name: str = "",
    file_mime: str = "",
    existing_inventory: list[dict] | None = None,
) -> dict:
    """Convert Excel (.xlsx), CSV/TSV, messy text, PDF, or stock images via Gemini LLM into InventoryEntry JSON rows."""
    detected_format = "text"
    combined_text = str(raw_text or "").strip()
    img_b64: str | None = None
    img_mime: str | None = None

    if file_b64:
        clean_b64 = file_b64.split(",", 1)[1] if (file_b64.startswith("data:") and "," in file_b64) else file_b64
        raw_bytes = base64.b64decode(clean_b64, validate=False)
        lower_name = (file_name or "").lower()
        lower_mime = (file_mime or "").lower()
        if lower_mime.startswith("image/") or lower_name.endswith((".png", ".jpg", ".jpeg", ".webp")):
            img_b64 = base64.b64encode(raw_bytes).decode("ascii")
            img_mime = file_mime if lower_mime.startswith("image/") else (
                "image/png" if lower_name.endswith(".png") else ("image/webp" if lower_name.endswith(".webp") else "image/jpeg")
            )
            detected_format = "image"
        elif lower_mime == "application/pdf" or lower_name.endswith(".pdf"):
            img_b64 = base64.b64encode(raw_bytes).decode("ascii")
            img_mime = "application/pdf"
            detected_format = "pdf"
        else:
            extracted, fmt = extract_spreadsheet_text(raw_bytes, file_name=file_name, mime_type=file_mime)
            detected_format = fmt
            combined_text = f"{combined_text}\n\n{extracted}".strip() if combined_text else extracted

    if not combined_text and not img_b64:
        raise ValueError("Provide inventory text or select a file (.xlsx, .csv, .txt, .pdf, image).")

    items = await parse_stock_vision(gemini, combined_text, image_b64=img_b64, mime_type=img_mime)
    inv_lookup: dict[tuple[str, str, str, str, str], dict] = {}
    for row in existing_inventory or []:
        d = row.get("data") if isinstance(row.get("data"), dict) else row
        rid = row.get("id") or d.get("id")
        rev = int(row.get("revision") or d.get("revision") or 0)
        key = (
            _norm_tok(d.get("device") or d.get("model")),
            _norm_tok(d.get("variant")),
            _norm_tok(d.get("condition")),
            _norm_tok(d.get("lock_status")),
            _norm_tok(d.get("region_spec")),
        )
        if key[0] and rid:
            inv_lookup[key] = {"id": rid, "revision": rev}

    rows: list[dict] = []
    seen_ids: set[str] = set()
    for idx, item in enumerate(items):
        brand = str(item.get("brand") or "").strip()
        model = str(item.get("model") or "").strip()
        device = model if (not brand or brand.lower() in model.lower()) else f"{brand} {model}"
        device = re.sub(r"\s+", " ", device).strip()[:120]
        if len(device) < 2:
            continue
        var_parts = [str(item.get("variant") or "").strip()]
        if item.get("ram_gb") and item.get("storage_gb") and f"{item['ram_gb']}/{item['storage_gb']}" not in var_parts[0]:
            var_parts.append(f"{item['ram_gb']}/{item['storage_gb']}GB")
        elif item.get("storage_gb") and f"{item['storage_gb']}" not in var_parts[0]:
            var_parts.append(f"{item['storage_gb']}GB")
        if item.get("region_spec") and item["region_spec"] != "GLOBAL" and item["region_spec"].lower() not in var_parts[0].lower():
            var_parts.append(str(item["region_spec"]))
        variant = " · ".join(p for p in var_parts if p)[:100]

        lookup_key = (
            _norm_tok(device),
            _norm_tok(variant),
            _norm_tok(item.get("condition")),
            _norm_tok(item.get("lock_status")),
            _norm_tok(item.get("region_spec")),
        )
        match = inv_lookup.get(lookup_key)
        slug = re.sub(r"[^a-z0-9]+", "-", f"{device}-{variant}-{item.get('condition') or ''}-{item.get('lock_status') or ''}-{item.get('region_spec') or ''}".lower()).strip("-")[:80]
        row_id = match["id"] if match else (slug or f"sku-{uuid.uuid4().hex[:10]}")
        if row_id in seen_ids and not match:
            row_id = f"{row_id}-{idx}"
        seen_ids.add(row_id)
        rows.append({
            "id": row_id,
            "expected_revision": match["revision"] if match else 0,
            "device": device,
            "variant": variant,
            "part_number": item.get("part_number") or item.get("mpn"),
            "packaging": item.get("packaging"),
            "moq": item.get("moq"),
            "warranty": item.get("warranty"),
            "specs": item.get("specs"),
            "quantity": max(0, int(item.get("quantity") or 0)),
            "unit_cost": float(item["unit_cost"]) if item.get("unit_cost") else None,
            "currency": str(item.get("currency") or "INR").upper()[:3],
            "condition": item.get("condition") or "UNKNOWN",
            "lock_status": item.get("lock_status") or "UNKNOWN",
            "region_spec": str(item.get("region_spec") or "GLOBAL")[:40],
            "tax_status": item.get("tax_status") or "UNKNOWN",
            "color_breakdown": item.get("color_breakdown") or None,
        })
    return {"items": items, "rows": rows, "detected_format": detected_format}
