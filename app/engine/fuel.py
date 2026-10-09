import math
from datetime import date

from app.config import get_engine_config


def validate_mileage(vehicle: str, mileage) -> str | None:
    """Raises ValueError for zero/negative/non-numeric; returns a warning if implausible."""
    if isinstance(mileage, bool) or not isinstance(mileage, int | float) or not math.isfinite(mileage) or mileage <= 0:
        raise ValueError("Mileage must be a positive number.")
    r = get_engine_config()["mileage_kmpl"]["car" if vehicle == "car" else "bike_scooter"]
    if not r["min"] <= mileage <= r["max"]:
        return f"Mileage {mileage:g} looks unusual for a {vehicle} (typical {r['min']} to {r['max']})."
    return None


def _round(x: float) -> int:
    step = get_engine_config()["fuel"]["round_to_inr"]
    return max(step, round(x / step) * step)


def fuel_cost(distance_km: float, mileage: float, price: float, ratio_p50: float, ratio_p80: float):
    """Returns (p50_inr, p80_inr, effective_mileage_p50). Stop-and-go lowers mileage."""
    k = get_engine_config()["fuel"]["congestion_mileage_penalty"]
    m50 = mileage / (1 + k * max(ratio_p50 - 1, 0))
    m80 = mileage / (1 + k * max(ratio_p80 - 1, 0))
    return _round(distance_km / m50 * price), _round(distance_km / m80 * price), round(m50, 1)


def price_is_stale(as_of: str, today: date) -> bool:
    return (today - date.fromisoformat(as_of)).days > get_engine_config()["fuel"]["stale_days"]
