from decimal import Decimal, ROUND_CEILING


def quote_usd(cost_inr: Decimal, inr_per_usd: Decimal, fee: Decimal, margin: Decimal, haircut: Decimal):
    values = (cost_inr, inr_per_usd, fee, margin, haircut)
    if not all(isinstance(v, Decimal) and v.is_finite() for v in values):
        raise ValueError("Finite decimal inputs are required")
    if cost_inr <= 0 or inr_per_usd <= 0 or any(v < 0 or v >= 1 for v in (fee, margin, haircut)):
        raise ValueError("Invalid quote parameters")
    raw = cost_inr / ((1-margin) * (1-fee) * inr_per_usd * (1-haircut))
    return raw.quantize(Decimal("0.01"), rounding=ROUND_CEILING)
