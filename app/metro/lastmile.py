"""Getting to and from the station: walk, bike taxi or auto, with an indicative time and cost from the tariffs in
lastmile.yaml. Prices are estimates from standard tariffs (apps vary with demand), never live."""
from app.metro.data import engine, hm

MODES = ("walk", "bike_taxi", "auto")


def _night(minute: float, frm: str, to: str) -> bool:
    m, a, b = minute % 1440, hm(frm), hm(to)
    return (a <= m or m < b) if a > b else a <= m < b


def tariff(cfg: dict, km: float, minute: float, window: dict) -> float:
    """max(minimum fare, minimum fare + per km x km beyond the base distance), times the night factor at that clock time."""
    fare = max(cfg["min_fare_inr"], cfg["min_fare_inr"] + cfg["per_km_inr"] * (km - cfg.get("base_km", 2)))
    f = cfg.get("night_factor") or 1
    if f != 1 and _night(minute, cfg.get("night_from", window["night_from"]), cfg.get("night_to", window["night_to"])):
        fare *= f
    return fare


def options(lm: dict, metres: float, minute: float) -> list[dict]:
    """walk, bike taxi and auto for this distance (street metres) at this clock time. `eligible` is False for a walk longer
    than the comfortable limit and for a bike taxi marked unavailable; a forced choice still uses them."""
    km = metres / 1000
    w = lm["walk"]
    walk_min = max(1, round(metres / w["speed_m_per_min"]))
    out = [{"mode": "walk", "minutes": {"lo": walk_min, "hi": walk_min}, "cost_inr": {"lo": 0, "hi": 0}, "eligible": walk_min <= w["comfortable_max_min"]}]
    window = {"night_from": lm["auto"].get("night_from", "22:00"), "night_to": lm["auto"].get("night_to", "05:00")}
    for mode in ("bike_taxi", "auto"):
        c = lm[mode]
        mid = tariff(c, km, minute, window)
        s = c.get("cost_spread_pct", 20) / 100
        out.append({"mode": mode,
                    "minutes": {"lo": round(c["pickup_wait_min"]["lo"] + km / c["speed_kmph"]["hi"] * 60),
                                "hi": round(c["pickup_wait_min"]["hi"] + km / c["speed_kmph"]["lo"] * 60)},
                    "cost_inr": {"lo": round(mid * (1 - s)), "hi": round(mid * (1 + s))},
                    "eligible": c.get("available", True) is not False})
    return out


def choose(opts: list[dict], forced: str) -> tuple[dict, str | None]:
    """(chosen option, warning). `best` = lowest middle minutes + middle cost / value of time among the eligible ones."""
    if forced in MODES:
        pick = next(o for o in opts if o["mode"] == forced)
        warn = None
        if not pick["eligible"]:
            warn = ("Walking that far would take about {} minutes.".format(pick["minutes"]["hi"]) if forced == "walk"
                    else "Bike taxis may not be available here; check the app.")
        return pick, warn
    vot = engine()["compare"]["value_of_time_inr_per_min"]
    pool = [o for o in opts if o["eligible"]] or [o for o in opts if o["mode"] == "auto"]
    return min(pool, key=lambda o: (o["minutes"]["lo"] + o["minutes"]["hi"]) / 2 + (o["cost_inr"]["lo"] + o["cost_inr"]["hi"]) / 2 / vot), None


def describe(metres: float, opts: list[dict], chosen: dict) -> dict:
    return {"distance_m": round(metres), "chosen": chosen["mode"], "minutes": chosen["minutes"], "cost_inr": chosen["cost_inr"],
            "options": [{k: o[k] for k in ("mode", "minutes", "cost_inr", "eligible")} for o in opts]}
