"""Reference currency conversions and world FX matrices via Frankfurter v2."""
import asyncio
import math
import re
import time
import weakref
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

from .onboarding import contact_hint

FRANKFURTER_BASE = "https://api.frankfurter.dev"
DEFAULT_WORLD_CURRENCIES = (
    "USD", "INR", "AED", "EUR", "GBP", "SAR", "CNY", "JPY",
    "SGD", "HKD", "CAD", "AUD", "CHF", "QAR", "KWD", "KRW",
)
METAL_PRECISION = {"XAU": 4, "XAG": 4, "XPT": 4, "XPD": 4}
_CURRENCY_CACHE = {"at": 0.0, "data": None}
_RATES_CACHE = {}
_HISTORY_CACHE = {}
_FX_CLIENTS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


class FXUnavailable(ValueError):
    pass


def _valid_code(code):
    return isinstance(code, str) and bool(re.fullmatch(r"[A-Z]{3}", code))


def _finite_rate(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    val = float(value)
    if not math.isfinite(val) or val <= 0:
        return None
    return val


def _pooled_fx_client(timeout=15):
    import httpx
    loop = asyncio.get_running_loop()
    client = _FX_CLIENTS.get(loop)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(
            timeout=timeout,
            limits=httpx.Limits(max_keepalive_connections=8, max_connections=16, keepalive_expiry=60),
        )
        _FX_CLIENTS[loop] = client
    return client


async def _frankfurter_get(path, params=None, *, timeout=15, transport=None):
    import httpx
    url = FRANKFURTER_BASE + path
    for attempt in range(2):
        try:
            async with asyncio.timeout(timeout):
                if transport is not None:
                    async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
                        response = await client.get(url, params=params or None)
                        response.raise_for_status()
                        return response.json()
                response = await _pooled_fx_client(timeout).get(url, params=params or None)
                response.raise_for_status()
                return response.json()
        except (httpx.TimeoutException, httpx.NetworkError, httpx.ProtocolError, TimeoutError):
            if attempt == 0 and transport is None:
                await asyncio.sleep(0.25)
                continue
            raise FXUnavailable("Live Frankfurter currency data is currently unavailable.") from None
        except (httpx.HTTPError, ValueError, TypeError):
            raise FXUnavailable("Live Frankfurter currency data is currently unavailable.") from None


def _downsample(points, limit=30):
    if len(points) <= limit:
        return points
    step = (len(points) - 1) / (limit - 1)
    indices = sorted({round(i * step) for i in range(limit)} | {len(points) - 1})
    return [points[i] for i in indices]


def preferred_currency(phone, explicit=None):
    if isinstance(explicit, str) and re.fullmatch(r"[A-Z]{3}", explicit):
        return {"currency": explicit, "source": "buyer_requested"}
    if isinstance(phone, str) and phone:
        try:
            from babel.numbers import get_territory_currencies
            region = contact_hint(phone)["country_hint"]
            currencies = get_territory_currencies(region) if region else []
            if len(currencies) == 1:
                return {"currency": currencies[0], "source": "number_region_hint"}
        except ValueError:
            pass
    return {"currency": "USD", "source": "default"}


async def convert_reference(amount, base, quote, *, transport=None):
    import httpx
    from babel.numbers import get_currency_precision
    if not all(re.fullmatch(r"[A-Z]{3}", code) for code in (base, quote)):
        raise ValueError("Use three-letter currency codes.")
    amount = Decimal(str(amount))
    if not amount.is_finite() or not 0 < amount <= Decimal("1000000000000"):
        raise ValueError("Enter a positive amount up to one trillion.")
    rate = Decimal(1)
    as_of = None
    if base != quote:
        try:
            async with asyncio.timeout(5):
                async with httpx.AsyncClient(timeout=5, transport=transport) as client:
                    response = await client.get(f"{FRANKFURTER_BASE}/v2/rate/{base}/{quote}")
                    response.raise_for_status()
                    row = response.json(parse_float=Decimal)
            if row["base"] != base or row["quote"] != quote:
                raise ValueError("Currency pair did not match.")
            as_of = date.fromisoformat(row["date"])
            age = (datetime.now(timezone.utc).date() - as_of).days
            rate = Decimal(str(row["rate"]))
            if not 0 <= age <= 7 or not rate.is_finite() or not Decimal("1e-15") <= rate <= Decimal("1e15"):
                raise ValueError("Rate is stale or invalid.")
        except (httpx.HTTPError, TimeoutError, ValueError, KeyError, TypeError, ArithmeticError):
            if transport is None:
                try:
                    async with asyncio.timeout(5):
                        async with httpx.AsyncClient(timeout=5) as client:
                            fb = await client.get(f"https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest/v1/currencies/{base.lower()}.json")
                            fb.raise_for_status()
                            payload = fb.json(parse_float=Decimal)
                            val = (payload.get(base.lower()) or {}).get(quote.lower())
                            if val is not None:
                                rate = Decimal(str(val))
                                if rate.is_finite() and Decimal("1e-15") <= rate <= Decimal("1e15"):
                                    as_of = date.fromisoformat(str(payload.get("date")))
                                    if not 0 <= (datetime.now(timezone.utc).date() - as_of).days <= 7:
                                        raise ValueError("Fallback rate is stale or future dated")
                                    digits = METAL_PRECISION.get(quote) or get_currency_precision(quote)
                                    precision = Decimal(1).scaleb(-digits)
                                    return {
                                        "base": base,
                                        "quote": quote,
                                        "amount": format(amount, "f"),
                                        "converted": format((amount * rate).quantize(precision, rounding=ROUND_HALF_UP), "f"),
                                        "rate": format(rate, "f"),
                                        "as_of": as_of.isoformat(),
                                        "source": "exchange-api CDN",
                                        "kind": "reference",
                                    }
                except Exception:
                    pass
            raise FXUnavailable(f"A recent Frankfurter rate for {base}/{quote} is unavailable.") from None
    digits = METAL_PRECISION.get(quote) or get_currency_precision(quote)
    precision = Decimal(1).scaleb(-digits)
    return {
        "base": base,
        "quote": quote,
        "amount": format(amount, "f"),
        "converted": format((amount * rate).quantize(precision, rounding=ROUND_HALF_UP), "f"),
        "rate": format(rate, "f"),
        "as_of": as_of.isoformat() if as_of else None,
        "source": "Frankfurter" if as_of else "same currency",
        "kind": "reference",
    }


async def world_currencies(*, include_metals=True, refresh=False, transport=None):
    now = time.monotonic()
    if transport is None and not refresh and _CURRENCY_CACHE["data"] and now - _CURRENCY_CACHE["at"] < 21600:
        return _CURRENCY_CACHE["data"] if include_metals else [c for c in _CURRENCY_CACHE["data"] if c["type"] == "currency"]
    raw = await _frankfurter_get("/v2/currencies", timeout=10, transport=transport)
    if not isinstance(raw, list):
        raise FXUnavailable("Unexpected currency catalog response.")
    pinned_index = {code: idx for idx, code in enumerate(DEFAULT_WORLD_CURRENCIES)}
    catalog = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        code = str(item.get("iso_code") or "").upper()
        kind = str(item.get("type") or "")
        if not _valid_code(code) or kind not in ("currency", "metal"):
            continue
        catalog.append({
            "code": code,
            "name": str(item.get("name") or code)[:80],
            "symbol": str(item.get("symbol") or code)[:12],
            "type": kind,
            "pinned_default": code in pinned_index,
        })
    catalog.sort(key=lambda c: (0 if c["pinned_default"] else (1 if c["type"] == "currency" else 2), pinned_index.get(c["code"], 999), c["code"]))
    if transport is None:
        _CURRENCY_CACHE["at"] = now
        _CURRENCY_CACHE["data"] = catalog
    return catalog if include_metals else [c for c in catalog if c["type"] == "currency"]


async def _fetch_board(base, *, refresh=False, transport=None):
    now = time.monotonic()
    cached = _RATES_CACHE.get(base)
    if transport is None and not refresh and cached and now - cached["at"] < 90:
        return cached["data"]
    raw = await _frankfurter_get("/v2/rates", {"base": base}, timeout=10, transport=transport)
    if not isinstance(raw, list):
        raise FXUnavailable("Unexpected FX board response.")
    board = {}
    for row in raw:
        if not isinstance(row, dict) or row.get("base") != base:
            continue
        quote = str(row.get("quote") or "").upper()
        rate = _finite_rate(row.get("rate"))
        day = str(row.get("date") or "")
        if _valid_code(quote) and rate is not None and day:
            board[quote] = {"rate": rate, "date": day}
    if transport is None and board:
        _RATES_CACHE[base] = {"at": now, "data": board}
    return board


async def _fetch_multi_history(base, quotes, days, *, refresh=False, transport=None):
    quote_list = tuple(sorted({q for q in quotes if _valid_code(q) and q != base}))
    if not quote_list:
        return {}
    key = (base, quote_list, days)
    now = time.monotonic()
    cached = _HISTORY_CACHE.get(key)
    if transport is None and not refresh and cached and now - cached["at"] < 300:
        return cached["data"]
    start_date = (datetime.now(timezone.utc).date() - timedelta(days=days)).isoformat()
    raw = await _frankfurter_get(
        "/v2/rates",
        {"base": base, "quotes": ",".join(quote_list), "from": start_date},
        timeout=12,
        transport=transport,
    )
    if not isinstance(raw, list):
        raise FXUnavailable("Unexpected FX history response.")
    series_map = defaultdict(list)
    for row in raw:
        if not isinstance(row, dict) or row.get("base") != base:
            continue
        quote = str(row.get("quote") or "").upper()
        day = str(row.get("date") or "")
        rate = _finite_rate(row.get("rate"))
        if quote in quote_list and day and rate is not None:
            series_map[quote].append({"date": day, "rate": rate})
    result = {q: sorted(pts, key=lambda p: p["date"]) for q, pts in series_map.items()}
    if transport is None and result:
        _HISTORY_CACHE[key] = {"at": now, "data": result}
    return result


async def world_rates(base="USD", *, symbols=None, days=30, compare_quotes=None, refresh=False, transport=None):
    base = str(base or "USD").upper()
    if not _valid_code(base):
        raise ValueError("Use a valid three-letter base currency code.")
    days = max(7, min(365, int(days)))

    default_order = [c for c in DEFAULT_WORLD_CURRENCIES if c != base]
    pinned = []
    for sym in (symbols or default_order):
        code = str(sym or "").upper()
        if _valid_code(code) and code != base and code not in pinned:
            pinned.append(code)
    pinned = pinned[:24] or default_order[:15]

    compare_list = []
    for sym in (compare_quotes or [c for c in ("INR", "AED", "EUR", "GBP", "JPY", "CNY") if c != base]):
        code = str(sym or "").upper()
        if _valid_code(code) and code != base and code not in compare_list:
            compare_list.append(code)
    history_targets = sorted(set(pinned) | set(compare_list[:6]))

    catalog, board, hist_map = await asyncio.gather(
        world_currencies(include_metals=True, refresh=refresh, transport=transport),
        _fetch_board(base, refresh=refresh, transport=transport),
        _fetch_multi_history(base, history_targets, days, refresh=refresh, transport=transport),
    )

    meta = {c["code"]: c for c in catalog}
    pinned_rank = {code: idx for idx, code in enumerate(pinned)}
    rows = []
    for code, entry in board.items():
        if code == base:
            continue
        info = meta.get(code)
        if not info:
            continue
        pts = hist_map.get(code) or []
        change_1d = None
        change_period = None
        high = None
        low = None
        if len(pts) >= 2:
            prev = pts[-2]["rate"]
            first = pts[0]["rate"]
            cur = pts[-1]["rate"]
            if prev > 0:
                change_1d = round((cur / prev - 1) * 100, 3)
            if first > 0:
                change_period = round((cur / first - 1) * 100, 3)
            rates_only = [p["rate"] for p in pts]
            high = max(rates_only)
            low = min(rates_only)
        rows.append({
            "code": code,
            "name": info["name"],
            "symbol": info["symbol"],
            "type": info["type"],
            "pinned": code in pinned_rank,
            "rate": entry["rate"],
            "inverse_rate": 1.0 / entry["rate"],
            "as_of": entry["date"],
            "change_1d_pct": change_1d,
            "change_period_pct": change_period,
            "period_high": high,
            "period_low": low,
            "sparkline": _downsample(pts, 28) if pts else [],
        })

    rows.sort(key=lambda r: (0 if r["pinned"] else (1 if r["type"] == "currency" else 2), pinned_rank.get(r["code"], 999), r["code"]))

    comparison = []
    for code in compare_list[:6]:
        pts = hist_map.get(code) or []
        if len(pts) < 2 or pts[0]["rate"] <= 0:
            continue
        first = pts[0]["rate"]
        norm = [{"date": p["date"], "pct": round((p["rate"] / first - 1) * 100, 3), "rate": p["rate"]} for p in _downsample(pts, 45)]
        comparison.append({"code": code, "name": meta.get(code, {}).get("name", code), "points": norm})

    latest_date = max((r["as_of"] for r in rows), default=datetime.now(timezone.utc).date().isoformat())
    return {
        "base": base,
        "days": days,
        "as_of": latest_date,
        "source": "Frankfurter / central bank & global reference rates",
        "kind": "reference",
        "currencies": catalog,
        "rates": rows,
        "comparison": comparison,
        "pinned": pinned,
    }


async def pair_history(base="USD", quote="INR", *, days=90, refresh=False, transport=None):
    base = str(base or "USD").upper()
    quote = str(quote or "INR").upper()
    if not (_valid_code(base) and _valid_code(quote)):
        raise ValueError("Use three-letter currency codes.")
    days = max(7, min(365, int(days)))
    if base == quote:
        today = datetime.now(timezone.utc).date().isoformat()
        return {
            "base": base,
            "quote": quote,
            "days": days,
            "as_of": today,
            "latest_rate": 1.0,
            "change_pct": 0.0,
            "change_1d_pct": 0.0,
            "high": 1.0,
            "low": 1.0,
            "mean": 1.0,
            "source": "Same currency identity",
            "kind": "reference",
            "points": [{"date": today, "rate": 1.0, "ma7": 1.0}],
        }

    hist_map = await _fetch_multi_history(base, [quote], days, refresh=refresh, transport=transport)
    pts = hist_map.get(quote) or []
    if not pts:
        # Fallback to current single spot rate if historical range returned sparse rows
        board = await _fetch_board(base, refresh=refresh, transport=transport)
        if quote not in board:
            raise FXUnavailable(f"Historical rate series for {base}/{quote} is unavailable.")
        pts = [{"date": board[quote]["date"], "rate": board[quote]["rate"]}]

    enriched = []
    window = []
    for p in pts:
        window.append(p["rate"])
        if len(window) > 7:
            window.pop(0)
        enriched.append({"date": p["date"], "rate": p["rate"], "ma7": sum(window) / len(window)})
    rates = [p["rate"] for p in enriched]
    first, latest = rates[0], rates[-1]
    prev = rates[-2] if len(rates) >= 2 else latest
    return {
        "base": base,
        "quote": quote,
        "days": days,
        "as_of": enriched[-1]["date"],
        "latest_rate": latest,
        "change_pct": round((latest / first - 1) * 100, 3) if first > 0 else 0.0,
        "change_1d_pct": round((latest / prev - 1) * 100, 3) if prev > 0 else 0.0,
        "high": max(rates),
        "low": min(rates),
        "mean": sum(rates) / len(rates),
        "source": "Frankfurter / central bank & global reference rates",
        "kind": "reference",
        "points": _downsample(enriched, 90),
    }


async def buyer_pricing(phone, triage):
    preference = preferred_currency(phone, triage.get("reply_currency"))
    stated = next((item for item in triage.get("items", []) if item.get("unit_price") and item.get("currency")), None)
    result = {**preference, "basis": "buyer_stated_unit_price", "conversions": []}
    if stated:
        async def lookup(target):
            try:
                return await convert_reference(stated["unit_price"], stated["currency"], target)
            except (FXUnavailable, ValueError, ArithmeticError):
                return {"quote": target, "unavailable": True}
        result["item"] = stated["device"]
        targets = dict.fromkeys(("USD", preference["currency"]))
        result["conversions"] = await asyncio.gather(*(lookup(target) for target in targets))
    return result


def compute_aa_buffer_pct(*, fx_change_1h_pct=0.0, tech_drop_pct=0.0, adverse_news_count=0, aa_cap_pct=2.0):
    """Upward-only safety margin ratchet (aa%). Protects dealer margins on FX spikes or market shocks."""
    fx_shock = max(0.0, float(fx_change_1h_pct or 0.0)) * 1.25
    tech_shock = max(0.0, -float(tech_drop_pct or 0.0)) * 0.25
    news_shock = min(0.75, max(0, int(adverse_news_count or 0)) * 0.25)
    raw = fx_shock + tech_shock + news_shock
    cap = max(0.0, min(10.0, float(aa_cap_pct or 2.0)))
    return round(min(cap, max(0.0, raw)), 3)


async def calculate_dynamic_quote(cost_amount, cost_currency, buyer_phone, *, margin_pct=3.5, aa_pct=0.0, explicit_currency=None, transport=None):
    """Compute USD + buyer-preferred local currency quote with upward-only aa% FX shock buffer."""
    from babel.numbers import get_currency_precision
    pref = preferred_currency(buyer_phone, explicit_currency)
    target_ccy = pref["currency"]
    base_cost = Decimal(str(cost_amount))
    usd_conv = await convert_reference(base_cost, cost_currency.upper(), "USD", transport=transport)
    spot_usd = Decimal(str(usd_conv["converted"]))
    margin_mult = Decimal(1) + (Decimal(str(max(0.0, float(margin_pct)))) / Decimal(100))
    aa_mult = Decimal(1) + (Decimal(str(max(0.0, float(aa_pct)))) / Decimal(100))
    quote_usd = (spot_usd * margin_mult * aa_mult).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    quote_local = quote_usd
    if target_ccy != "USD":
        local_conv = await convert_reference(quote_usd, "USD", target_ccy, transport=transport)
        digits = METAL_PRECISION.get(target_ccy) or get_currency_precision(target_ccy)
        quote_local = Decimal(str(local_conv["converted"])).quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP)

    return {
        "quote_usd": format(quote_usd, "f"),
        "target_currency": target_ccy,
        "currency_source": pref["source"],
        "quote_local": format(quote_local, "f"),
        "margin_pct": float(margin_pct),
        "aa_pct_applied": float(aa_pct),
        "fx_as_of": usd_conv.get("as_of"),
    }
