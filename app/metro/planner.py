"""The metro estimate: slots x (timetable, crowd history, holiday, weather) -> travel time range and crowd level.

Everything is an estimate from public timetable information and Aug-Sep 2025 ridership (BMRCL via RTI), never live."""
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from functools import lru_cache

from fastapi import HTTPException

from app.config import CONFIG_DIR, get_city
from app.metro import lastmile
from app.metro.data import Net, daytype, engine, hhmm
from app.metro.route import find_route
from app.providers.base import Point
from app.service import IST, RAIN_ORDER, rain_level

DISCLAIMER = "Estimates from past ridership and typical train frequency, not live."
STATION_DISCLAIMER = "Estimates from past ridership, not live."
ATTRIBUTION = "Ridership data: BMRCL via RTI, compiled by Vonter/bmrcl-ridership-hourly, ODbL"
FARE_AS_OF = "Feb 2025 tariff"
FARE_NOTE = ("Fare by distance (Rs 10 to Rs 90) under BMRCL's February 2025 tariff, with no later revision reported. "
             "It is approximate near a distance boundary: check the price when you buy.")  # a planned Feb 2026 rise was suspended; discounts not applied
LEVELS =["low", "moderate", "high", "packed"]


@lru_cache
def _holiday_names() -> dict[str, str]:
    text = (CONFIG_DIR / get_city("bengaluru")["holidays"]).read_text(encoding="utf-8")
    return {m[1]: m[2].strip() for m in re.finditer(r"(\d{4}-\d{2}-\d{2})\s*#\s*~?\s*(.+)", text)}


def _clock(hhmm_text: str) -> str:
    """"08:30" -> "8:30 AM", the same way the website words times."""
    return datetime.strptime(hhmm_text, "%H:%M").strftime("%I:%M %p").lstrip("0")


def holiday_name(day: date) -> str:
    return _holiday_names().get(day.isoformat(), "Public holiday")


def crowd_level(score: float, crush: float, seated: float) -> dict:
    """score = passengers per train / crush capacity (the bar). The level comes from absolute anchors on passengers per
    train P (see engine.yaml): a seat needs P below the seated count, not below a fraction of crush capacity."""
    e = engine()["crowd_levels"]
    p = score * crush
    level = ("low" if p < e["seat_likely_x_seated"] * seated else "moderate" if p < e["standing_frac_crush"] * crush
             else "high" if p < e["crowded_frac_crush"] * crush else "packed")
    return {"score": round(score, 2), "level": level, "seat": engine()["seat"][level]}


def walk_min(km: float) -> tuple[int, int]:
    """(path metres, minutes) for a walk of this straight-line distance."""
    e = engine()
    metres = km * 1000 * e["walk_detour_factor"]
    return round(metres), max(1, round(metres / e["walk_speed_m_per_min"]))


def direction_hint(net: Net, sid: str) -> str:
    out = []
    for lid in net.station_lines[sid]:
        ends = [net.display(t) for t in net.lines[lid]["terminals"] if t != sid]
        out.append(f"{net.lines[lid]['name']} towards " + " or ".join(ends))
    return "; ".join(out)


# ---- nearest
def nearest(net: Net, lat: float, lon: float, name: str | None, limit: int) -> dict:
    results = []
    for km, sid in net.nearest(lat, lon, limit):
        metres, mins = walk_min(km)
        results.append({"station_id": sid, "display": net.display(sid), "distance_m": round(km * 1000), "walk_min": mins,
                        "lines": net.summary(sid)["lines"], "direction_hint": direction_hint(net, sid)})
    return {"query": {"lat": round(lat, 5), "lon": round(lon, 5), "name": name}, "results": results}


# ---- one station
def _station_scores(net: Net, sid: str, day_type: str, minute: float) -> tuple[float, str] | None:
    best = None  # (score, line)
    for lid in net.station_lines[sid]:
        order = net.order[lid]
        i = net.pos[(lid, sid)]
        for j in (i - 1, i + 1):
            if not 0 <= j < len(order):
                continue
            for a, b in ((sid, order[j]), (order[j], sid)):
                towards = net.lines[lid]["terminals"][1 if net.pos[(lid, b)] > net.pos[(lid, a)] else 0]
                load = net.load_at(lid, towards, a, b, day_type, minute)
                if load is not None:
                    s = load / (60 / net.headway(lid, day_type, minute)[0] * net.cap(lid))
                    best = (s, lid) if best is None or s > best[0] else best
    return best


def station_info(net: Net, sid: str, day: date, providers, now: datetime) -> dict:
    cfg = engine()
    day_type = daytype(day, providers.holidays.is_holiday(day))
    s = net.summary(sid)
    platforms, first_last, headway_now = [], [], {}
    for lid in net.station_lines[sid]:
        line = net.lines[lid]
        for t in line["terminals"]:
            if t == sid:
                continue
            p = (net.stations[sid].get("platforms") or {}).get(lid, {}).get(f"towards_{t}") or {}
            ok = p.get("confidence") == "verified" and p.get("number") is not None
            platforms.append({"line": lid, "line_name": line["name"], "colour": line["colour"], "towards": net.display(t), "towards_id": t,
                              "number": p["number"] if ok else None, "confidence": p.get("confidence", "unknown"),
                              "label": f"Towards {net.display(t)}"})
            fl = net.first_last(lid, sid, t, day_type)
            if fl:
                first_last.append({"line": lid, "towards": net.display(t), "first": hhmm(fl[0]), "last": hhmm(fl[1]), "confidence": fl[2]})
        h = net.headway(lid, day_type, now.hour * 60 + now.minute)
        headway_now[lid] = {"min": h[0], "band": h[1]}
    service = [fl for lid in net.station_lines[sid] for t in net.lines[lid]["terminals"] if t != sid
               for fl in [net.first_last(lid, sid, t, day_type)] if fl]
    crowd = []
    rows = net.ridership.get(sid, {}).get("board", {}).get(day_type)
    for h in range(24):
        if service and not any(f[0] <= h * 60 + 30 <= f[1] for f in service):
            continue
        score = _station_scores(net, sid, day_type, h * 60 + 30) if net.has_crowd else None
        entry = {"hour": h, "boardings": round(rows[h]) if rows else None}
        entry.update(crowd_level(score[0], net.cap(score[1]), net.seated(score[1])) if score is not None else {"score": None, "level": "unknown", "seat": None})
        crowd.append(entry)
    ranked = sorted((c for c in crowd if c["boardings"] is not None), key=lambda c: -c["boardings"])
    busiest = sorted(c["hour"] for c in ranked[:cfg["station"]["busiest_hours"]])
    near = [{"id": o, "display": net.display(o), "distance_m": round(km * 1000)}
            for km, o in net.nearest(net.stations[sid]["lat"], net.stations[sid]["lon"], cfg["station"]["nearby_limit"] + 1)
            if o != sid and km * 1000 <= cfg["station"]["nearby_radius_m"]][:cfg["station"]["nearby_limit"]]
    connects, seen = [], set()
    for lid in net.station_lines[sid]:
        i = net.pos[(lid, sid)]
        for j in (i - 1, i + 1):
            if 0 <= j < len(net.order[lid]) and net.operational(net.order[lid][j]) and net.order[lid][j] not in seen:
                seen.add(net.order[lid][j])
                connects.append({"id": net.order[lid][j], "display": net.display(net.order[lid][j])})
    return {"station": s, "platforms": platforms, "first_last": first_last, "headway_now": headway_now, "crowd_today": crowd,
            "busiest_hours": busiest, "connects": connects, "nearby_stations": near, "disclaimer": STATION_DISCLAIMER}


# ---- plan
def endpoint(net: Net, ref: dict) -> dict:
    """A station id, or a place snapped to the nearest operational station with the walk to it."""
    if ref.get("station_id"):
        if not net.operational(ref["station_id"]):
            raise HTTPException(422, "We do not know that station.")
        return {"station_id": ref["station_id"], "walk_m": None, "walk_min": None, "place": None}
    near = net.nearest(ref["lat"], ref["lon"], 1)
    place = (ref.get("name") or "Pinned location")[:80]
    if not near or near[0][0] > engine()["snap_max_km"]:
        where = f" ({net.display(near[0][1])}, about {near[0][0]:.0f} km away)" if near else ""
        raise HTTPException(422, f"{place} is more than {engine()['snap_max_km']} km from the nearest metro station{where}, "
                                 "which is too far for a metro trip. Try the road option.")
    metres, mins = walk_min(near[0][0])
    return {"station_id": near[0][1], "walk_m": metres, "walk_min": mins, "place": place, "_km": near[0][0]}


def _slot(net: Net, route, minute: int, day_type: str, rain_lvl: str, walk_in: float, walk_out: float) -> dict | None:
    """One departure (leaving home/the station at `minute`). None when a train on the way is not running."""
    cfg = engine()
    r = cfg["range"]
    t = minute + walk_in + (cfg["station_access_min"])
    walk, wait_mid, wait_lo, wait_hi, ride = walk_in + cfg["station_access_min"], 0.0, 0.0, 0.0, 0.0
    worst, boards, bands = None, [], {}
    for i, leg in enumerate(route.legs):
        h, band, _, _ = net.headway(leg.line, day_type, t)
        fl = net.first_last(leg.line, leg.stations[0], leg.towards_id, day_type)
        if fl and not fl[0] <= t <= fl[1]:
            return None
        boards.append((leg.line, t))
        bands[leg.line] = {"min": h, "band": band}
        wait_mid, wait_lo, wait_hi = wait_mid + h * cfg["wait_fraction"], wait_lo + h * r["wait_lo_frac"], wait_hi + h * r["wait_hi_frac"]
        step = net.lines[leg.line].get("avg_interstation_min", 2.0)
        depart = t + h * cfg["wait_fraction"]
        for k in range(len(leg.stations) - 1):
            a, b = leg.stations[k], leg.stations[k + 1]
            load = net.load_at(leg.line, leg.towards_id, a, b, day_type, depart + k * step)
            if load is not None:
                tph = 60 / net.headway(leg.line, day_type, depart + k * step)[0]
                score = load / (tph * net.cap(leg.line))
                if worst is None or score > worst[0]:
                    worst = (score, a, leg.line)
        leg_ride = (len(leg.stations) - 1) * step
        ride += leg_ride
        t = depart + leg_ride
        if i < len(route.changes):
            walk += route.changes[i].walk_min
            t += route.changes[i].walk_min
    walk += cfg["station_egress_min"] + walk_out
    mid = walk + wait_mid + ride
    crowd = None
    if worst is not None:
        crowd = crowd_level(worst[0] * (1 + cfg["rain_crowd_factor"][rain_lvl]), net.cap(worst[2]), net.seated(worst[2]))
    return {"mid": mid, "lo": walk + wait_lo + ride, "hi": walk + wait_hi + ride * (1 + r["ride_hi_pct"]), "wait": wait_mid,
            "ride": ride, "walk": walk, "crowd": crowd, "peak": worst[1] if worst else None, "boards": boards, "bands": bands}


def _label(day: date, today: date) -> str:
    return "Today" if day == today else "Tomorrow" if day == today + timedelta(days=1) else day.strftime("%a %d %b")


def plan(net: Net, origin: dict, dest: dict, start_hour: int, end_hour: int, weekdays_only: bool, only_date: date | None,
         providers, now: datetime | None = None, last_mile: str = "best") -> dict:
    cfg = engine()
    now = now or datetime.now(IST)
    o, d = endpoint(net, origin), endpoint(net, dest)
    if o["station_id"] == d["station_id"]:
        raise HTTPException(422, "Origin and destination are the same station." if o["place"] is None and d["place"] is None
                            else "Both places are nearest to the same station. Try places farther apart.")
    route = find_route(net, o["station_id"], d["station_id"])
    if route is None:
        raise HTTPException(422, "There is no metro route between those two stations yet.")
    today = now.date()
    if only_date is not None:
        if not 0 <= (only_date - today).days <= 6:
            raise HTTPException(422, "I can only look up to 7 days ahead.")
        dates = [only_date]
    else:
        dates, offset = [], 0
        while len(dates) < (5 if weekdays_only else 7):
            day = today + timedelta(days=offset)
            offset += 1
            if not (weekdays_only and day.weekday() >= 5):
                dates.append(day)
    st = net.stations[o["station_id"]]
    pt = lambda sid: Point(round(net.stations[sid]["lat"], 2), round(net.stations[sid]["lon"], 2))  # noqa: E731
    # the arrival end matters too (most people walk or take an auto from the station); one lookup if both ends round to the same spot,
    # and the two lookups run in parallel so a slow weather service costs one timeout, not two
    if pt(d["station_id"]) == pt(o["station_id"]):
        forecast = forecast_end = providers.weather.forecast(pt(o["station_id"]))
    else:
        with ThreadPoolExecutor(2) as pool:
            forecast, forecast_end = pool.map(lambda sid: providers.weather.forecast(pt(sid)), (o["station_id"], d["station_id"]))
    def lm_end(e: dict, clock: float):
        """(options, chosen, warning) for getting to/from the station at this clock time; None for a station or a few metres."""
        if net.lastmile is None or e["walk_m"] is None or e["walk_m"] < cfg["lastmile"]["min_distance_m"]:
            return None
        opts = lastmile.options(net.lastmile, e["walk_m"], clock)
        return (opts, *lastmile.choose(opts, last_mile))

    def lm_minutes(e: dict, res) -> tuple[float, float, dict, dict]:
        """(lo, hi, cost lo/hi) of that end: the chosen ride/walk, or the plain walk when there is no last-mile data."""
        if res:
            m = res[1]["minutes"]
            return m["lo"], m["hi"], res[1]["cost_inr"]
        w = float(e["walk_min"] or 0)
        return w, w, {"lo": 0, "hi": 0}

    days, closed = [], False
    for day in dates:
        is_hol = providers.holidays.is_holiday(day)
        day_type = daytype(day, is_hol)
        entry = {"date": day.isoformat(), "weekday": day.strftime("%a"), "label": _label(day, today),
                 "holiday": holiday_name(day) if is_hol else None, "rain_level": "none", "summary": None, "slots": []}
        for minute in range(round(start_hour * 60), round(end_hour * 60), cfg["slot_step_min"]):  # hours may be fractional (chat)
            if day == today and minute < now.hour * 60 + now.minute:
                continue
            rain = forecast.at(datetime(day.year, day.month, day.day, minute // 60, tzinfo=IST)) if forecast else None
            lvl = rain_level(rain[0]) if rain else "none"
            res_o = lm_end(o, minute)
            o_lo, o_hi, o_cost = lm_minutes(o, res_o)
            w_in = (o_lo + o_hi) / 2
            s = _slot(net, route, minute, day_type, lvl, w_in, 0.0)
            if s is None:
                closed = True
                continue
            res_d = lm_end(d, minute + s["mid"])  # the auto's night rate depends on when you arrive
            d_lo, d_hi, d_cost = lm_minutes(d, res_d)
            d_mid = (d_lo + d_hi) / 2
            s["lo"] += o_lo - w_in + d_lo
            s["hi"] += o_hi - w_in + d_hi
            s["mid"] += d_mid
            s["walk"] += d_mid
            lm_cost = {"lo": o_cost["lo"] + d_cost["lo"], "hi": o_cost["hi"] + d_cost["hi"]}
            arrive = datetime(day.year, day.month, day.day, tzinfo=IST) + timedelta(minutes=minute + s["mid"])
            end = forecast_end.at(arrive.replace(minute=0, second=0, microsecond=0)) if forecast_end else None
            wet = {"start": lvl, "end": rain_level(end[0])} if rain and end else None  # None: a forecast is missing, never "dry"
            entry["slots"].append({"time": hhmm(minute), "wait_min": round(s["wait"]), "ride_min": round(s["ride"]), "walk_min": round(s["walk"]),
                                   "total_min": {"lo": round(s["lo"]), "hi": max(round(s["hi"]), round(s["lo"]))},
                                   "crowd": s["crowd"], "peak_station": net.display(s["peak"]) if s["peak"] else None, "note": None,
                                   "rain": wet, "umbrella": bool(wet) and any(v != "none" for v in wet.values()),
                                   "last_mile_cost_inr": lm_cost, "_lm": (res_o, res_d), "_mid": s["mid"], "_bands": s["bands"]})
            if RAIN_ORDER.index(lvl) > RAIN_ORDER.index(entry["rain_level"]):
                entry["rain_level"] = lvl
        days.append(entry)

    def cost(sl):
        return sl["_mid"] + (cfg["crowd_penalty_min"][sl["crowd"]["level"]] if sl["crowd"] else 0)

    for e in days:
        e["umbrella"] = any(sl["umbrella"] for sl in e["slots"])
        if e["slots"]:
            top = min(e["slots"], key=cost)
            e["summary"] = {"best_time": top["time"], "best_total_min": round(top["_mid"])}
    first = next((e for e in days if e["slots"]), None)
    if first is None:
        fl = [net.first_last(lg.line, lg.stations[0], lg.towards_id, "weekday") for lg in route.legs]
        span = f" Trains on this route run from about {_clock(hhmm(fl[0][0]))} to {_clock(hhmm(fl[0][1]))}." if fl and fl[0] else ""
        raise HTTPException(422, "No train times left in that window." + span)
    top = min(first["slots"], key=cost)
    quick = min(first["slots"], key=lambda sl: sl["_mid"])
    crowd = top["crowd"]
    if not crowd:
        reason = "Quickest time in your window. Crowd estimates are not available for this trip."
    elif top is quick:
        reason = f"Quickest time in your window. {cfg['seat_phrase'][crowd['level']]}."
    else:
        reason = f"Best balance of trip time and crowding. {cfg['seat_phrase'][crowd['level']]}. The quickest time, {_clock(quick['time'])}, is more crowded."
    fare = net.fare(o["station_id"], d["station_id"])
    lm_warn = []
    for e, res in zip((o, d), top["_lm"], strict=True):
        e["last_mile"] = lastmile.describe(e["walk_m"], res[0], res[1]) if res else None
        if res and res[2]:
            lm_warn.append(res[2])
    lmc = top["last_mile_cost_inr"]
    best = {"date": first["date"], "time": top["time"], "weekday": first["weekday"], "total_min": top["total_min"], "crowd": crowd,
            "reason": reason, "rain": top["rain"], "umbrella": top["umbrella"], "last_mile_cost_inr": lmc,
            "total_cost_inr": {"lo": fare + lmc["lo"], "hi": fare + lmc["hi"]} if fare is not None else None}
    umbrella_note = None
    if top["umbrella"]:
        r = top["rain"]
        when = ("when you set off and when you arrive" if r["start"] != "none" and r["end"] != "none"
                else "when you set off" if r["start"] != "none" else "when you arrive")
        umbrella_note = (f"We predict rain {when}. Please carry an umbrella: you may be walking or taking a Rapido or auto after the station.")

    legs, boards = [], dict(top["_bands"])
    for leg in route.legs:
        ln = net.lines[leg.line]
        p = (net.stations[leg.stations[0]].get("platforms") or {}).get(leg.line, {}).get(f"towards_{leg.towards_id}") or {}
        verified = p.get("confidence") == "verified" and p.get("number") is not None
        legs.append({"line_id": leg.line, "line_name": ln["name"], "colour": ln["colour"], "from_id": leg.stations[0],
                     "from": net.display(leg.stations[0]), "to_id": leg.stations[-1], "to": net.display(leg.stations[-1]),
                     "stops": len(leg.stations) - 1, "towards": net.display(leg.towards_id), "towards_id": leg.towards_id,
                     "platform": {"number": p["number"] if verified else None, "confidence": p.get("confidence", "unknown"),
                                  "label": f"Towards {net.display(leg.towards_id)}"},
                     "station_ids": leg.stations})
    transfers = [{"station_id": c.station_id, "display": net.display(c.station_id), "from_line": c.from_line, "to_line": c.to_line,
                  "walk_min": round(c.walk_min), "note": f"Change to the {net.lines[c.to_line]['name']} towards "
                  f"{net.display(route.legs[i + 1].towards_id)}." + ("" if c.confidence == "verified" else " Walking time is approximate.")}
                 for i, c in enumerate(route.changes)]
    ride = round(sum((len(lg.stations) - 1) * net.lines[lg.line].get("avg_interstation_min", 2.0) for lg in route.legs))
    fl, best_type = {}, daytype(date.fromisoformat(first["date"]), bool(first["holiday"]))
    for lg in route.legs:
        f = net.first_last(lg.line, lg.stations[0], lg.towards_id, best_type)
        if f:
            fl[lg.line] = {"station": net.display(lg.stations[0]), "towards": net.display(lg.towards_id), "first": hhmm(f[0]),
                           "last": hhmm(f[1]), "confidence": f[2]}
    warnings = list(lm_warn)
    for e in (o, d):
        km = e.pop("_km", 0)
        if km > cfg["far_warn_km"]:
            warnings.append(f"{e['place']} is about {km:.1f} km from the nearest metro station ({net.display(e['station_id'])}), "
                            "so most of this trip is the ride to or from the station.")
    if o["walk_min"] and o["walk_min"] >= 15 and (o["last_mile"] is None or o["last_mile"]["chosen"] == "walk"):
        warnings.append(f"The nearest station to your start is about {o['walk_min']} minutes' walk away.")
    if d["walk_min"] and d["walk_min"] >= 15 and (d["last_mile"] is None or d["last_mile"]["chosen"] == "walk"):
        warnings.append(f"The nearest station to your destination is about {d['walk_min']} minutes' walk away.")
    wet = [e for e in days if e["rain_level"] != "none"]
    if wet:
        warnings.append("Rain is forecast on " + ", ".join(f"{e['label']} ({e['rain_level']})" for e in wet[:3]) +
                        ". Metro trains run in rain, but stations can be busier; crowd scores are nudged up (an assumption).")
    if forecast is None or forecast_end is None:
        warnings.append("Weather forecast unavailable; rain is not included.")
    if any(e["holiday"] for e in days):
        warnings.append("Holiday days use the holiday crowd pattern, and Sunday train frequency where the timetable has no holiday figure.")
    if not net.has_crowd:
        warnings.append("Crowd estimates are not available right now.")
    if any(c.confidence != "verified" for c in route.changes):
        warnings.append("Time to change trains is approximate.")
    if any(first_conf != "verified" for first_conf in (v["confidence"] for v in fl.values())):
        warnings.append("First and last train times at your station are worked out from the terminal times; check the station display.")
    if closed:
        warnings.append("Times when trains are not running on this route are not shown.")
    for e in days:
        for sl in e["slots"]:
            del sl["_mid"], sl["_bands"], sl["_lm"]
    return {
        "origin": {**o, "display": net.display(o["station_id"])}, "dest": {**d, "display": net.display(d["station_id"])},
        "route": {"stops": route.stops, "ride_min": ride, "legs": legs, "transfers": transfers},
        "last_mile_disclaimer": (net.lastmile or {}).get("disclaimer"),
        "fare_inr": fare, "fare_as_of": FARE_AS_OF, "fare_note": FARE_NOTE,
        "best": best, "umbrella_note": umbrella_note, "days": days, "headway": boards, "first_last": fl, "warnings": warnings,
        "disclaimer": DISCLAIMER, "attribution": ATTRIBUTION,
    }

