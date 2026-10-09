from dataclasses import dataclass

from app.config import get_engine_config
from app.providers.base import RouteEstimate


@dataclass
class SlotEstimate:
    p50_min: float
    p80_min: float
    breakdown: dict[str, float]  # components sum exactly to p50_min
    ratio_p50: float  # P50 / free-flow, drives fuel and EV adjustments
    ratio_p80: float
    confidence: str
    confidence_reasons: list[str]


def estimate_slot(
    route: RouteEstimate,
    vehicle: str,
    *,
    user_speed_kmh: float | None = None,
    rain_mm: float = 0.0,
    holiday: bool = False,
    zone_level: bool = True,
    weather_known: bool = True,
) -> SlotEstimate:
    if route.distance_km <= 0 or route.free_flow_minutes <= 0:
        raise ValueError("Origin and destination are too close to estimate.")
    cfg = get_engine_config()
    e = cfg["estimate"]
    free = route.free_flow_minutes
    delay = max(route.traffic_minutes - free, 0.0) * cfg["vehicle_congestion_multiplier"][vehicle]
    holiday_adj = -delay * (1 - e["holiday_delay_factor"]) if holiday else 0.0
    rain_share = min(e["rain_cap"], e["rain_per_mm"] * rain_mm)
    weather = (free + delay + holiday_adj) * rain_share
    subtotal = free + delay + holiday_adj + weather

    personal = 0.0
    if user_speed_kmh:
        lo, hi = e["speed_factor_clamp"]
        factor = min(max((route.distance_km / (subtotal / 60)) / user_speed_kmh, lo), hi)
        personal = subtotal * (factor - 1)

    parts = {
        "free_flow": free,
        "traffic_signal_delay": delay,
        "holiday_adjustment": holiday_adj,
        "weather_delay": weather,
        "personal_speed_adjustment": personal,
    }
    breakdown = {k: round(v, 1) for k, v in parts.items()}
    p50 = round(sum(breakdown.values()), 1)  # sum of rounded parts so the UI always adds up
    ratio = p50 / free
    spread = e["base_spread"] + e["spread_per_congestion"] * max(ratio - 1, 0) + e["spread_rain"] * rain_share
    if route.degraded:
        spread *= e["degraded_spread_multiplier"]
    p80 = round(p50 * (1 + spread), 1)

    score, reasons = 1.0, []
    if route.degraded:
        score -= 0.4
        reasons.append("Traffic provider unavailable; using a rough fallback.")
    if not weather_known:
        score -= 0.1
        reasons.append("Weather forecast unavailable.")
    if zone_level:
        score -= 0.1
        reasons.append("Area-level locations are less precise than exact pins.")
    if ratio > 2:
        score -= 0.1
        reasons.append("Heavy congestion is harder to predict.")
    label = "high" if score >= 0.8 else "medium" if score >= 0.6 else "low"
    return SlotEstimate(p50, p80, breakdown, ratio, p80 / free, label, reasons)
