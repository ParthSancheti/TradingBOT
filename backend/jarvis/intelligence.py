"""Observable analytics, settings and reviewable alerts. No trade execution."""
import hashlib
import math
import statistics
import time
from datetime import datetime,timezone,timedelta
import re
from typing import Literal
from pydantic import BaseModel,ConfigDict,Field,field_validator,model_validator
from urllib.parse import urlsplit


class DeskSettings(BaseModel):
    model_config=ConfigDict(extra="forbid")
    notifications: bool=True
    chime: bool=True
    base_currency: str=Field(default="INR",pattern=r"^[A-Z]{3}$")
    fx_threshold_inr: float=Field(default=.5,ge=.005,le=100)
    fx_poll_seconds: int=Field(default=60,ge=60,le=3600)
    market_enabled: bool=True
    news_interval_seconds: int=Field(default=3600,ge=3600,le=86400)
    match_alerts: bool=True
    sheet_url: str=Field(default="",max_length=300)
    live_model: str=Field(default="gemini-3.8-live",pattern=r"^gemini-[a-zA-Z0-9.\-]{1,90}$")
    fx_base: str=Field(default="USD",pattern=r"^[A-Z]{3}$")
    fx_quote: str=Field(default="INR",pattern=r"^[A-Z]{3}$")
    fx_watchlist: list[str]=Field(default_factory=lambda:["USD","INR","AED","EUR","GBP","SAR","CNY","JPY","SGD","HKD","CAD","AUD","CHF","QAR","KWD","KRW"],max_length=32)
    agent_name: str=Field(default="Jarvis",min_length=1,max_length=40)
    agent_persona: Literal["balanced","executive","proactive","concise"]="balanced"
    agent_language: Literal["auto_hindlish","english","hindi"]="auto_hindlish"
    agent_custom_instructions: str=Field(default="",max_length=1200)
    agent_auto_navigate: bool=True
    agent_allow_writes: bool=False
    agent_proactive_insights: bool=True
    agent_max_tool_steps: int=Field(default=6,ge=2,le=10)

    # Startup & Display
    always_start_stopped: bool=True
    ui_scale: int=Field(default=100,ge=50,le=200)

    # Autonomy & Dispatch
    autonomy_mode: Literal["observe","draft","send","turbo"]="observe"
    legacy_autonomy_migration: str | None=None
    auto_reply_max_usd: float=Field(default=5000.0,ge=0,le=500000)
    reply_bubble_mode: Literal["single","two_bubble"]="single"
    group_auto_reply_enabled: bool=False
    human_mute_hours: float=Field(default=4.0,ge=0.25,le=48.0)
    dispatch_jitter_min_ms: int=Field(default=1500,ge=0,le=60000)
    dispatch_jitter_max_ms: int=Field(default=4200,ge=0,le=60000)

    # Demand & Sourcing
    min_buy_quantity_threshold: int=Field(default=1,ge=1)
    below_moq_policy: Literal["hold","explain"]="hold"
    min_buyers_for_seller_outreach: int=Field(default=2,ge=1)
    min_total_qty_for_seller_outreach: int=Field(default=5,ge=1)
    offer_freshness_ttl_hours: float=Field(default=24.0,ge=0.1)

    # Price & FX
    default_margin_pct: float=Field(default=3.5,ge=0,le=50)
    min_margin_floor_pct: float=Field(default=0.0,ge=0.0)
    volatility_aa_cap_pct: float=Field(default=2.0,ge=0,le=10)
    quote_validity_minutes: int=Field(default=30,ge=1,le=1440)
    show_usd_alongside_local: bool=False

    # Hours & Schedules
    working_hours_start: str=Field(default="08:30",pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    working_hours_end: str=Field(default="21:30",pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    morning_dispatch_time: str=Field(default="08:45",pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    off_hours_generate_and_schedule: bool=True
    reprice_fx_on_wake: bool=True
    out_of_stock_auto_reply_enabled: bool=True
    partial_stock_policy: Literal["quote_available_with_gap","hold_for_review"]="quote_available_with_gap"

    # Alerts & Notifications
    notify_deal_matches: bool=True
    notify_high_demand: bool=True
    notify_sourcing_gaps: bool=True
    notify_critical_errors: bool=True
    buyer_repeat_cooldown_seconds: int=Field(default=15,ge=0,le=300)

    @model_validator(mode="before")
    @classmethod
    def pre_migrate(cls, data):
        if isinstance(data, dict):
            raw_mode = str(data.get("autonomy_mode") or "").strip().lower()
            if raw_mode == "auto":
                data["autonomy_mode"] = "turbo"
                data["legacy_autonomy_migration"] = "Migrated legacy auto -> turbo (historical automatic intent)"
            elif raw_mode == "approved_auto":
                data["autonomy_mode"] = "draft"
                data["legacy_autonomy_migration"] = "Migrated legacy approved_auto -> draft (historical approval intent)"
            elif raw_mode and raw_mode not in ("observe", "draft", "send", "turbo"):
                raise ValueError(f"Invalid autonomy_mode '{raw_mode}'. Allowed modes are: observe, draft, send, turbo.")
        return data

    @model_validator(mode="after")
    def validate_jitter(self):
        if self.dispatch_jitter_min_ms > self.dispatch_jitter_max_ms:
            raise ValueError("dispatch_jitter_min_ms cannot exceed dispatch_jitter_max_ms")
        return self


    @field_validator("agent_name")
    @classmethod
    def check_agent_name(cls,v):
        cleaned=re.sub(r"[<>{}\r\n\t]+"," ",str(v or "")).strip()
        if not cleaned:
            return "Jarvis"
        return cleaned[:40]
    @field_validator("agent_custom_instructions")
    @classmethod
    def check_agent_instructions(cls,v):
        return str(v or "").strip()[:1200]
    @field_validator("fx_watchlist")
    @classmethod
    def check_watchlist(cls,v):
        import re
        cleaned=[]
        for item in v:
            code=str(item).strip().upper()
            if not re.fullmatch(r"[A-Z]{3}",code): raise ValueError("Use three-letter ISO currency codes")
            if code not in cleaned: cleaned.append(code)
        return cleaned
    @field_validator("sheet_url")
    @classmethod
    def sheet_link(cls,v):
        if not v:return v
        u=urlsplit(v)
        import re
        if u.scheme!="https" or u.netloc!="docs.google.com" or not re.fullmatch(r"/spreadsheets/d/[A-Za-z0-9_-]+(?:/edit)?",u.path):
            raise ValueError("Use the connected Google spreadsheet's /spreadsheets/d/.../edit link")
        return "https://docs.google.com"+u.path+("" if u.path.endswith("/edit") else "/edit")


async def settings(repo):
    if hasattr(repo, "get_local_state"):
        local = repo.get_local_state("desktop-settings")
        if isinstance(local, dict):
            return DeskSettings.model_validate(local).model_dump()
        row = repo.get_sync("configuration", "desktop-settings")
    else:
        row = await repo.get("configuration", "desktop-settings")
    cfg = DeskSettings.model_validate(row["data"] if row else {}).model_dump()
    if row and hasattr(repo, "set_local_state") and repo.get_local_state("desktop-settings") is None:
        repo.set_local_state("desktop-settings", cfg)
    return cfg


def estimate(series,interval="1day"):
    """Walk-forward MAE selection; test split is never used to fit/select models."""
    clean=[]
    for p in sorted(series,key=lambda x:x["at"]):
        if not isinstance(p.get("rate"),(int,float)) or not math.isfinite(p["rate"]) or p["rate"]<=0:continue
        if clean and p["at"]==clean[-1]["at"]:continue
        clean.append({"at":p["at"],"rate":float(p["rate"])})
    if len(clean)<40:return {"available":False,"reason":"At least 40 valid historical observations are required.","history":clean[-30:]}
    values=[p["rate"] for p in clean]
    def predict(history,method):
        if method=="random_walk":return history[-1]
        tail=history[-21:]
        returns=[math.log(b/a) for a,b in zip(tail,tail[1:])]
        # Shrunk recent drift, rather than an LLM-invented price movement.
        drift=sum(r*(.9**i) for i,r in enumerate(reversed(returns)))/sum(.9**i for i in range(len(returns))) if returns else 0
        return history[-1]*math.exp(drift*.35)
    split=max(30,int(len(values)*.75))
    scores={m:statistics.mean(abs(predict(values[:i],m)-values[i]) for i in range(20,split)) for m in ("random_walk","shrunk_drift")}
    method=min(scores,key=scores.get)
    errors=[predict(values[:i],method)-values[i] for i in range(split,len(values))]
    pct=[abs(e)/values[i]*100 for e,i in zip(errors,range(split,len(values)))]
    residuals=[predict(values[:i],method)-values[i] for i in range(20,split)]
    bound=sorted(abs(e) for e in residuals)[min(len(residuals)-1,int(.9*len(residuals)))]
    prediction=predict(values,method);last=values[-1]
    parsed=datetime.fromisoformat(clean[-1]["at"].replace("Z","+00:00"))
    if parsed.tzinfo is None:parsed=parsed.replace(tzinfo=timezone.utc)
    target=parsed+(timedelta(hours=1) if interval=="1h" else timedelta(days=1))
    if interval!="1h":
        while target.weekday()>4:target+=timedelta(days=1)
    return {"available":True,"model":method,"basis":"historical_prices_only","interval":interval,"predicted_rate":prediction,
            "change_pct":(prediction/last-1)*100,"last_rate":last,"as_of":clean[-1]["at"],"target_at":target.isoformat(),
            "range_low":max(.0001,prediction-bound),"range_high":prediction+bound,"range_label":"90th-percentile training residual band; not a guarantee",
            "test_samples":len(errors),"mae_inr":statistics.mean(abs(e) for e in errors),"mape_pct":statistics.mean(pct),"max_error_pct":max(pct),
            "empirical_coverage_pct":sum(abs(e)<=bound for e in errors)/len(errors)*100,"error_target_pct":10,
            "target_met_on_test":max(pct)<=10,"guaranteed_error_bound":False,"history":clean[-60:]}


_last_system_alerts = {}

async def record_system_alert(repo, category: str, title: str, body: str, details: dict = None, cooldown_seconds: float = 300.0):
    """Emit actionable system alert with coalescing of repeated failures within cooldown window."""
    now = time.time()
    key = f"{category}:{title}"
    last_time = _last_system_alerts.get(key, 0)
    if now - last_time < cooldown_seconds:
        return None
    _last_system_alerts[key] = now
    cooldown_bucket = int(now // cooldown_seconds)
    return await alert(repo, category, f"{category}:{title}:{cooldown_bucket}", title, body, details)


async def alert(repo, kind, key, title, body, details=None):
    rid = "alert-" + hashlib.sha256((kind + key).encode()).hexdigest()[:40]
    existing = await repo.get("tasks", rid)
    if existing:
        return existing
    return await repo.put(
        "tasks",
        rid,
        {
            "kind": "notification",
            "category": kind,
            "title": title,
            "body": body,
            "details": details or {},
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
        rid,
    )


async def match_opportunities(repo):
    rows=await repo.list("messages",500);buy=[];sell=[]
    for row in rows:
        d=row["data"];t=d.get("triage",{})
        if d.get("state")!="CLASSIFIED" or not t.get("proceed") or t.get("needs_review") or d.get("stock_file_text"):continue
        cat = str(t.get("category") or "").upper()
        for item in t.get("items",[]):
            ident_name = item.get("device") or item.get("model") or item.get("part_number") or item.get("mpn")
            if not ident_name:continue
            entry=(row,item)
            item_intent = str(item.get("direction") or item.get("intent") or "").upper()
            if cat == "BUY" or (cat == "MIXED" and item_intent == "BUY"):
                buy.append(entry)
            elif cat in ("SELL","STOCK") or (cat == "MIXED" and item_intent in ("SELL","STOCK")):
                sell.append(entry)
    import re
    norm=lambda s:re.sub(r"[^a-z0-9]+","",(s or "").casefold())
    def compat(a, b):
        la = str(a.get("lock_status") or "").upper().strip()
        lb = str(b.get("lock_status") or "").upper().strip()
        if la in ("JV_LOCK", "UNLOCKED") and lb in ("JV_LOCK", "UNLOCKED") and la != lb:
            return False
        if la == "UNLOCKED" and "JV" in lb:
            return False
        if la == "JV_LOCK" and "UNLOCK" in lb:
            return False
        ca = str(a.get("condition") or "").upper().strip()
        cb = str(b.get("condition") or "").upper().strip()
        new_grades = {"NON_ACTIVE", "NEW", "SEALED", "BRAND_NEW"}
        used_grades = {"ACTIVATED", "ACTIVE", "REFURB", "REFURBISHED", "USED", "A_GRADE", "B_GRADE", "C_GRADE"}
        if ca in new_grades and cb in used_grades:
            return False
        if ca in used_grades and cb in new_grades:
            return False
        if ca in ("NON_ACTIVE", "ACTIVATED") and cb in ("NON_ACTIVE", "ACTIVATED") and ca != cb:
            return False
        return True
    inventory = await repo.list("inventory", 500)
    snapshot = await repo.get("configuration", "daily-stock")
    stock = [row["data"] for row in inventory]
    now = datetime.now(timezone.utc)
    if snapshot and 0 <= now.timestamp() - snapshot["data"].get("source_timestamp", 0) < 86400:
        stock.extend(snapshot["data"]["items"])

    def recent(row):
        try:
            stamp = datetime.fromisoformat(row["updated_at"].replace("Z", "+00:00"))
            return 0 <= (now - stamp).total_seconds() <= 7 * 86400
        except (ValueError, TypeError, KeyError):
            return False

    def identity(row):
        data = row["data"]
        return data.get("phone") or data.get("sender_id") or data.get("conversation_id")

    def same_product(a, b):
        pna = norm(a.get("part_number") or a.get("mpn"))
        pnb = norm(b.get("part_number") or b.get("mpn"))
        if pna and pnb:
            return pna == pnb and compat(a, b)
        dev_a = norm(a.get("device") or a.get("model"))
        dev_b = norm(b.get("device") or b.get("model"))
        return dev_a and dev_a == dev_b and norm(a.get("variant")) == norm(b.get("variant")) and compat(a, b)

    made = []
    seen = set()
    recent_buy = [(row, item) for row, item in buy if recent(row) and identity(row)]
    for seller, offered in sell:
        seller_identity = identity(seller)
        if not recent(seller) or not seller_identity:
            continue
        offered_name = offered.get("part_number") or offered.get("device") or offered.get("model") or ""
        product_key = (seller["id"], norm(offered_name), norm(offered.get("variant")))
        if product_key in seen:
            continue
        seen.add(product_key)
        matching = {}
        for buyer, wanted in recent_buy:
            buyer_identity = identity(buyer)
            if buyer_identity != seller_identity and same_product(wanted, offered):
                matching.setdefault(buyer_identity, (buyer, wanted))
        # B09 & B15: Supplier stock must never be represented as owned inventory
        stocked = any(
            item.get("quantity") and item["quantity"] > 0
            and same_product(item, offered)
            and item.get("source_kind") not in ("supplier_wts", "supplier")
            and item.get("ownership") != "supplier"
            and not item.get("supplier_phone")
            for item in stock
        )
        if not matching and not stocked:
            continue
        count = len(matching)
        band = "priority" if count >= 10 else "multiple" if count >= 2 else "single" if count else "stock"
        title = f"High demand ({count} buyers) · {offered_name}" if count >= 2 else (
            "Buyer and supplier match" if count else "Seller offer already in stock"
        )
        body = f"Seller offered {offered_name} {offered.get('variant') or ''}. {count} matching buyer(s)."
        if stocked:
            body += " This product is already listed in stock."
        details = {"seller_message": seller["id"], "device": offered["device"], "buyers_count": count,
                   "already_in_stock": stocked, "seller_item": offered, "needs_manual_review": True,
                   "can_merge_to_sheet": True}
        if matching:
            buyer, wanted = next(iter(matching.values()))
            details.update(buyer_message=buyer["id"], buyer_item=wanted)
        result = await alert(repo, "stock_match", ":".join((*product_key, band, str(stocked))), title, body, details)
        made.append(result)
        if count >= 2 and hasattr(repo, "queue_outbound"):
            phone = seller["data"].get("phone")
            jid = seller["data"].get("conversation_id")
            if isinstance(phone, str) and jid in (phone[1:] + "@c.us", phone[1:] + "@s.whatsapp.net"):
                repo.queue_outbound(
                    jid, [f"Thanks for your {offered['device']} offer. Could you confirm the available quantity, price and pickup location?"],
                    kind="seller_followup", recipient_phone=phone, dedupe_key="seller-followup:" + ":".join(product_key),
                )
    return made
