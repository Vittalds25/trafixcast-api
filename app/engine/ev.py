import math

from app.config import get_engine_config


def validate_ev(vehicle: str, range_km, battery_pct=None) -> str | None:
    """Raises ValueError on bad numbers; returns a warning for implausible range."""
    for v, name in ((range_km, "Range"), (battery_pct, "Battery")):
        if v is None and name == "Battery":
            continue
        if isinstance(v, bool) or not isinstance(v, int | float) or not math.isfinite(v) or v <= 0:
            raise ValueError(f"{name} must be a positive number.")
    if battery_pct is not None and not 1 <= battery_pct <= 100:
        raise ValueError("Battery level must be between 1 and 100.")
    r = get_engine_config()["ev"]["range_km"]["car" if vehicle == "car" else "bike_scooter"]
    if not r["min"] <= range_km <= r["max"]:
        return f"Range {range_km:g} km looks unusual for an electric {vehicle} (typical {r['min']} to {r['max']})."
    return None


def ev_usage(
    distance_km: float, full_range_km: float, vehicle: str, ratio_p50: float, *,
    rain_mm: float = 0.0, temp_c: float | None = None, battery_pct: float | None = None,
    round_trip: bool = False, own_real_range: bool = False,
) -> dict:
    c = get_engine_config()["ev"]
    effective = full_range_km if own_real_range else full_range_km * c["real_world_factor"]
    trip_km = distance_km * (2 if round_trip else 1)
    heat = c["heat_car_factor"] if vehicle == "car" and (temp_c or 0) >= c["heat_car_above_c"] else 0.0

    def used_km(rain: float, ratio: float) -> float:
        relief = max(c["congestion_relief_floor"], 1 - c["congestion_relief"] * max(ratio - 1, 0))
        return trip_km * relief * (1 + min(c["rain_cap"], c["rain_per_mm"] * rain)) * (1 + heat)

    # worst case: no slow-traffic relief and heavier rain, so P50 <= P80 always
    u50 = used_km(rain_mm, ratio_p50)
    u80 = max(u50, used_km(rain_mm * c["worst_case_rain_multiplier"], 1.0))
    out = {
        "effective_range_km": round(effective, 1),
        "used_pct_p50": round(u50 / effective * 100),
        "used_pct_p80": round(u80 / effective * 100),
        "trip_km": round(trip_km, 1),
        "adjustments": {"rain_mm": rain_mm, "heat_applied": heat > 0, "round_trip": round_trip,
                        "real_world_factor": 1.0 if own_real_range else c["real_world_factor"]},
        "battery_after_pct_p50": None, "battery_after_pct_p80": None, "warning": None,
    }
    start = battery_pct if battery_pct is not None else 100
    remaining80 = effective * start / 100 - u80
    if battery_pct is not None:
        out["battery_after_pct_p50"] = max(0, round((effective * start / 100 - u50) / effective * 100))
        out["battery_after_pct_p80"] = max(0, round(remaining80 / effective * 100))
    if remaining80 < max(c["safety_buffer_km"], effective * c["safety_buffer_percent"] / 100):
        out["warning"] = (
            "This trip may not be completable on the current charge. Consider charging first."
            if remaining80 < 0 else
            "Range after this trip may be low. Consider charging first."
        )
    return out
