"""Builds the 7-day estimate: slots x routing/weather/holiday -> engine -> fuel or EV."""
import dataclasses
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone

from fastapi import HTTPException

from app.config import get_city, get_engine_config, get_settings, load_yaml
from app.engine.estimate import estimate_slot
from app.engine.ev import ev_usage, validate_ev
from app.engine.fuel import fuel_cost, price_is_stale, validate_mileage
from app.providers.base import Point
from app.providers.mock import MockRouting
from app.schemas import EstimateRequest, Place

IST = timezone(timedelta(hours=5, minutes=30))
DISCLAIMER = (
    "Trafixcast shows AI-generated estimates based on historical patterns, schedules, and reported "
    "data. Actual travel time may vary. Do not rely on this for time-critical travel."
)
FUEL_NOTE = "Fuel cost is an estimate based on the mileage and fuel price you provide."
EV_NOTE = ("EV range usage is an estimate based on the range you provide. Real range varies with "
           "load, speed, weather, battery health, and driving style.")
_cache: dict = {}  # shortcut: per-process, swap for Redis before running more than one instance
OK_TTL, DEGRADED_TTL = 6 * 3600, 120


RAIN_ORDER = ["none", "light", "moderate", "heavy"]


def rain_level(mm: float) -> str:
    c = get_engine_config()["rain_levels"]
    return "heavy" if mm >= c["heavy"] else "moderate" if mm >= c["moderate"] else "light" if mm >= c["light"] else "none"


def areas(city: str = "bengaluru") -> dict[str, dict]:
    return {a["id"]: a for a in load_yaml(get_city(city)["areas"])}


PIN_DECIMALS = 3  # ~110 m: precise enough for a commute, and lets nearby pins share cached routes


def resolve(place: Place, city: str = "bengaluru") -> tuple[Point, str]:
    if place.area_id is not None:
        a = areas(city).get(place.area_id)
        if a is None:
            raise HTTPException(422, "Unknown area.")
        return Point(a["lat"], a["lon"]), a["name"]
    return Point(round(place.lat, PIN_DECIMALS), round(place.lon, PIN_DECIMALS)), "Pinned location"


_spend_lock = threading.Lock()
_spent = {"day": None, "n": 0}


def _within_budget() -> bool:
    """One lookup against today's allowance. shortcut: per-process counter, share it (Redis) before running
    more than one server."""
    today = datetime.now(IST).date()
    with _spend_lock:
        if _spent["day"] != today:
            _spent.update(day=today, n=0)
        if _spent["n"] >= get_settings().daily_route_budget:
            return False
        _spent["n"] += 1
        return True


def _route(providers, o: Point, d: Point, when: datetime):
    key = (o, d, when)
    hit = _cache.get(key)
    if hit and hit[1] > time.time():
        return hit[0]
    r = providers.routing.route(o, d, when) if _within_budget() else None
    if r is None:  # provider outage or budget used up: rough fallback, flagged so confidence drops
        r = dataclasses.replace(MockRouting().route(o, d, when), degraded=True)
    _cache[key] = (r, time.time() + (DEGRADED_TTL if r.degraded else OK_TTL))
    return r


def _slots(req: EstimateRequest, now: datetime) -> list[datetime]:
    e = get_engine_config()["estimate"]
    start, end = req.window_start_hour, req.window_end_hour
    out, shown, offset = [], 0, 0
    while shown < (5 if req.weekdays_only else 7):
        base = (now + timedelta(days=offset)).replace(hour=0, minute=0, second=0, microsecond=0)
        offset += 1
        if req.weekdays_only and base.weekday() >= 5:
            continue
        shown += 1
        t = base + timedelta(hours=start)
        while t < base + timedelta(hours=end):
            out.append(t)  # passed slots are kept so today's row stays visible
            t += timedelta(minutes=e["slot_step_min"])
    return out

def build_estimate(req: EstimateRequest, providers, now: datetime | None = None) -> dict:
    now = now or datetime.now(IST)
    o, oname = resolve(req.origin, req.city)
    d, dname = resolve(req.dest, req.city)
    if o == d:
        raise HTTPException(422, "Origin and destination are the same.")
    zone_level = req.origin.area_id is not None or req.dest.area_id is not None
    ev = req.fuel_type == "electric"
    warnings: list[str] = []

    price = price_as_of = None
    if req.fuel_type in ("petrol", "diesel", "cng") and req.mileage is not None:
        w = validate_mileage(req.vehicle, req.mileage)
        warnings += [w] if w else []
        if req.fuel_price_override:
            price, price_as_of, source = req.fuel_price_override, None, "override"
        else:
            price, price_as_of = providers.fuel.price(req.fuel_type, req.city)
            source = "curated"
            if price_is_stale(price_as_of, now.date()):
                warnings.append(f"Fuel price is from {price_as_of} and may be out of date; you can enter your own.")
    if ev and req.ev_range_km is not None:
        w = validate_ev(req.vehicle, req.ev_range_km, req.battery_pct)
        warnings += [w] if w else []

    slots = _slots(req, now)
    future = [t for t in slots if t >= now]  # the routing provider cannot predict the past
    if not future:
        raise HTTPException(422, "No departure times left in that window.")
    # the weather service only needs the neighbourhood, so an exact pin is rounded to ~1 km before it leaves us
    forecast = providers.weather.forecast(Point(round(o.lat, 2), round(o.lon, 2)))
    with ThreadPoolExecutor(3) as pool:  # TomTom free tier is ~5 requests/second
        routes = list(pool.map(lambda t: _route(providers, o, d, t), future))
    route_at = dict(zip(future, routes, strict=True))

    days: dict[str, dict] = {}
    for t in slots:
        day = days.setdefault(t.date().isoformat(), {
            "date": t.date().isoformat(), "weekday": t.strftime("%a"),
            "holiday": providers.holidays.is_holiday(t.date(), req.city), "rain_level": "none", "slots": []})
        if t < now:
            day["slots"].append({"time": t.strftime("%H:%M"), "past": True})
            continue
        route = route_at[t]
        rain_temp = forecast.at(t) if forecast else None
        rain, temp = rain_temp if rain_temp else (0.0, None)
        s = estimate_slot(route, req.vehicle, user_speed_kmh=req.avg_speed_kmh, rain_mm=rain,
                          holiday=providers.holidays.is_holiday(t.date(), req.city), zone_level=zone_level,
                          weather_known=rain_temp is not None)
        slot = {
            "time": t.strftime("%H:%M"), "past": False, "p50": s.p50_min, "p80": s.p80_min,
            "breakdown": s.breakdown, "confidence": s.confidence,
            "confidence_reasons": s.confidence_reasons, "fuel": None, "ev": None,
            "rain": {"mm": round(rain, 1), "level": rain_level(rain)},
        }
        if price is not None:
            lo, hi, eff = fuel_cost(route.distance_km, req.mileage, price, s.ratio_p50, s.ratio_p80)
            slot["fuel"] = {"p50_inr": lo, "p80_inr": hi, "distance_km": route.distance_km,
                            "mileage_used": eff, "price": price, "price_as_of": price_as_of,
                            "price_source": source}
        if ev and req.ev_range_km is not None:
            slot["ev"] = ev_usage(route.distance_km, req.ev_range_km, req.vehicle, s.ratio_p50,
                                  rain_mm=rain, temp_c=temp, battery_pct=req.battery_pct,
                                  round_trip=req.round_trip, own_real_range=req.ev_real_range)
        day["slots"].append(slot)
        if RAIN_ORDER.index(slot["rain"]["level"]) > RAIN_ORDER.index(day["rain_level"]):
            day["rain_level"] = slot["rain"]["level"]

    # "Best time to leave" = quickest slot on the soonest day that still has one: today if any of
    # today's slots are ahead, otherwise the next date. The user compares the other days in the calendar.
    first = next(d for d in days.values() if any(not s["past"] for s in d["slots"]))
    top = min((s for s in first["slots"] if not s["past"]), key=lambda s: s["p50"])
    best = {"date": first["date"], "time": top["time"], "p50": top["p50"], "p80": top["p80"]}

    if any(r.degraded for r in routes):
        warnings.append("Traffic data was unavailable for some slots; those estimates are rough.")
    if forecast is None:
        warnings.append("Weather forecast unavailable; rain is not included.")
    notes = [DISCLAIMER] + ([FUEL_NOTE] if price is not None else []) + ([EV_NOTE] if ev else [])
    return {
        "origin": oname, "dest": dname, "distance_km": routes[0].distance_km,
        "best": best, "days": list(days.values()), "warnings": warnings,
        "disclaimer": " ".join(notes), "generated_at": datetime.now(UTC).isoformat(),
    }
