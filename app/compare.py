"""Road vs metro for one trip: the road estimate (service.py) and the metro plan (metro/planner.py) side by side, with a
plain verdict. Costs are indicative: fuel only for the road, ticket price only for the metro. Nothing here is live."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date as Date
from datetime import datetime
from typing import Literal

from fastapi import HTTPException

from app.bus import planner as bus_planner
from app.bus.data import get_bus
from app.metro import planner
from app.metro.data import engine, get_net
from app.schemas import EstimateRequest
from app.service import IST, build_estimate, resolve

DISCLAIMER = "Estimates from predicted traffic, past ridership and typical train frequency, not live. Costs are indicative."
NOT_BENGALURU = "The metro comparison is only available for Bengaluru."
VEHICLE_HEADLINE = {"car": "Drive", "bike": "Take the bike", "scooter": "Take the scooter"}
MODE_WORDS = {"walk": "walk", "bike_taxi": "bike taxi", "auto": "auto"}
VEHICLE_VERB = {"car": "driving", "bike": "riding", "scooter": "riding"}
SEAT_WORDS = {"low": "A seat is likely at this hour", "moderate": "Standing room, a seat is possible at this hour",
              "high": "The metro will be crowded and a seat is unlikely at this hour", "packed": "The metro is likely packed, with no seats at this hour"}


class CompareRequest(EstimateRequest):
    date: Date | None = None
    last_mile: Literal["best", "walk", "bike_taxi", "auto"] = "best"  # how to get to and from the station


def _label(d: Date) -> str:
    return d.strftime("%a %d %b")


def _clock(hhmm: str) -> str:
    return datetime.strptime(hhmm, "%H:%M").strftime("%I:%M %p").lstrip("0")


def _mid(lo, hi) -> float:
    return (lo + hi) / 2


def _road_cost(slot: dict, req) -> tuple[dict | None, list, str, dict]:
    """(cost range, breakdown, note, extra) from the engine's own fuel figure. No fuel data means no cost, and we say so."""
    if slot.get("fuel"):
        f = slot["fuel"]
        return ({"lo": round(f["p50_inr"]), "hi": round(f["p80_inr"])}, [{"label": "Fuel", "inr": round(f["p50_inr"])}],
                "Fuel only. Parking and tolls are not included.", {})
    if slot.get("ev"):
        e = slot["ev"]
        return (None, [], "No rupee cost is estimated for electric vehicles; the share of range used is shown instead. Parking and tolls are not included.",
                {"ev_range_used_pct": {"lo": e["used_pct_p50"], "hi": e["used_pct_p80"]}})
    return None, [], "Add your fuel type and mileage to see the fuel cost. Parking and tolls are not included.", {}


def _gen(minutes: float, cost, crowd_level, cfg) -> float:
    """Generalised minutes used to rank the options: trip minutes + money at the value of time + a crowding penalty."""
    return minutes + (cost / cfg["value_of_time_inr_per_min"] if cost is not None else 0) + (cfg["crowd_penalty_min"].get(crowd_level, 0) if crowd_level else 0)


def _pick(road_g: float, metro_g: float, tie_pct: float) -> str:
    if abs(road_g - metro_g) < tie_pct / 100 * min(road_g, metro_g):
        return "tie"
    return "metro" if metro_g < road_g else "road"


def _route_summary(route: dict) -> str:
    legs = route["legs"]
    s = f"{legs[0]['line_name']} towards {legs[0]['towards']}"
    for i, t in enumerate(route["transfers"]):
        nxt = legs[i + 1]
        s += f", change at {t['display']} for the {nxt['line_name']} towards {nxt['towards']}"
    return s


def _bus_side(req, day: Date, slot_time: str) -> dict:
    """The BMTC bus for the same trip, leaving at the road slot's time on the chosen day: the quickest ordinary or AC option, door to door with the
    ride to the first stop and from the last (bus fares plus those), and the best ordinary and best AC side by side. Never raises."""
    out = {"available": False, "reason": NOT_BENGALURU, "best": None, "ordinary": None, "ac": None, "cost_note": ""}
    if req.city != "bengaluru":
        return out
    try:
        bus = get_bus()
        (op, on), (dp, dn) = resolve(req.origin, req.city), resolve(req.dest, req.city)
        plan = bus_planner.plan(bus, {"lat": op.lat, "lon": op.lon, "name": on}, {"lat": dp.lat, "lon": dp.lon, "name": dn},
                                int(slot_time[:2]) * 60 + int(slot_time[3:]), day, req.last_mile)
    except HTTPException as e:
        return {**out, "reason": str(e.detail)}
    except (OSError, KeyError, ValueError):
        return {**out, "reason": "Bus data is not available right now."}
    if not plan["options"]:
        return {**out, "reason": plan["warnings"][-1] if plan["warnings"] else "No BMTC bus was found for this trip."}
    o = plan["options"][0]
    cost = o["total_cost_inr"]
    return {"available": True, "reason": None, "ordinary": plan["compare"]["ordinary"], "ac": plan["compare"]["ac"],
            "best": {"routes": " then ".join(lg["route"] for lg in o["legs"]), "kind": "ac" if o["all_ac"] else "ordinary" if o["all_ordinary"] else "mixed",
                     "time": o["legs"][0]["board_time"], "door_to_door_min": {"lo": o["total_min"], "hi": o["total_min"]}, "fare_inr": o["fare_inr"],
                     "cost_inr": cost, "changes": len(o["legs"]) - 1, "crowd": o["crowd"], "next_buses": o["legs"][0]["next_buses"],
                     "wait_min": o["legs"][0]["wait_min"]},
            "cost_note": "Bus fares from the BMTC app data plus an estimated walk, bike taxi or auto to the first stop and from the last. Scheduled times, not live. "
                         "Bus crowd levels are a guess.", "warnings": plan["warnings"]}


def compare(req: CompareRequest, providers, now: datetime | None = None) -> dict:
    now = now or datetime.now(IST)
    cfg = engine()["compare"]
    today = now.date()
    if req.date is not None and not 0 <= (req.date - today).days <= 6:
        raise HTTPException(422, "I can only look up to 7 days ahead.")
    assumed = None  # (fuel type, km per litre) when the user gave no mileage
    if req.mileage is None and req.fuel_type != "electric":
        assumed = (req.fuel_type or cfg["default_fuel_type"], cfg["typical_mileage_kmpl"][req.vehicle])
        req = req.model_copy(update={"fuel_type": assumed[0], "mileage": assumed[1]})  # compare is about cost; the response says so
    plan, metro_days, net, reason = None, {}, None, NOT_BENGALURU

    def metro_side():
        nonlocal plan, metro_days, net, reason
        try:
            net = get_net()
            o_pt, o_name = resolve(req.origin, req.city)
            d_pt, d_name = resolve(req.dest, req.city)
            plan = planner.plan(net, {"lat": o_pt.lat, "lon": o_pt.lon, "name": o_name}, {"lat": d_pt.lat, "lon": d_pt.lon, "name": d_name},
                                req.window_start_hour, req.window_end_hour, req.weekdays_only, None, providers, now, req.last_mile)
            metro_days = {d["date"]: d for d in plan["days"] if d["slots"]}
            reason = None if metro_days else "No metro trains in your window on these days."
        except HTTPException as e:
            reason = str(e.detail)
        except (OSError, KeyError, ValueError):
            reason = "Metro data is not available right now."

    # the two halves share nothing but read-only config, so they run side by side (one rate-limit hit, one request)
    with ThreadPoolExecutor(2) as pool:
        metro_job = pool.submit(metro_side) if req.city == "bengaluru" else None
        est = build_estimate(req, providers, now)  # raises 422 for the same spot / no departure times left
        if metro_job:
            metro_job.result()
    road_days = {d["date"]: d for d in est["days"]}
    live = lambda d: [s for s in d["slots"] if not s["past"]]  # noqa: E731

    # ---- metro side, planned for the same window and days
    metro = {"available": False, "reason": NOT_BENGALURU, "best": None, "access": None, "route_summary": None,
             "breakdown": [], "fare_as_of": planner.FARE_AS_OF, "fare_note": planner.FARE_NOTE,
             "cost_note": f"Ticket price only. The walk or an auto/Rapido to and from the station is not included. {planner.FARE_NOTE}", "warnings": []}
    metro["reason"] = reason
    days_available = [{"date": k, "label": _label(Date.fromisoformat(k)), "has_both": bool(live(v)) and k in metro_days} for k, v in road_days.items()]
    if req.date is not None:
        chosen = req.date.isoformat()
        if chosen not in road_days or not live(road_days[chosen]):
            raise HTTPException(422, "No departure times left in that window.")
    else:
        chosen = next((d["date"] for d in days_available if d["has_both"]), None) or next(k for k, v in road_days.items() if live(v))
    cday = road_days[chosen]
    has_metro = plan is not None and chosen in metro_days
    if plan is not None and metro_days and not has_metro:
        metro["reason"] = "No metro trains in your window on that day."
    d = Date.fromisoformat(chosen)

    # ---- road best: quickest p50 slot of the chosen day
    r_slot = min(live(cday), key=lambda s: s["p50"])
    r_cost, r_break, r_note, r_extra = _road_cost(r_slot, req)
    rain = None if any("Weather forecast unavailable" in w for w in est["warnings"]) else r_slot["rain"]["level"]
    road = {"vehicle": req.vehicle,
            "best": {"time": r_slot["time"], "total_min": {"lo": round(r_slot["p50"]), "hi": round(r_slot["p80"])}, "cost_inr": r_cost, **r_extra},
            "breakdown": r_break, "cost_note": r_note, "rain": rain, "umbrella": rain not in (None, "none"),
            "cost_assumed": False, "cost_assumption": None}
    if assumed and r_slot.get("fuel"):
        unit = "kg" if assumed[0] == "cng" else "l"
        sentence = (f"Assumes a typical {assumed[1]:g} km/{unit} {assumed[0]} {req.vehicle} at Rs {r_slot['fuel']['price']:g}/{unit}. "
                    "Set your own mileage in More options.")
        road.update(cost_assumed=True, cost_assumption=sentence, cost_note=f"{sentence} {r_note}")

    # ---- metro best: the planner's own best time for that day
    m_slot = None
    if has_metro:
        mday = metro_days[chosen]
        m_slot = next(s for s in mday["slots"] if s["time"] == mday["summary"]["best_time"])
        fare = net.fare(plan["origin"]["station_id"], plan["dest"]["station_id"])
        crowd = m_slot["crowd"]
        o, de = plan["origin"], plan["dest"]
        lm_o, lm_d, has_lm = o["last_mile"], de["last_mile"], plan["last_mile_disclaimer"] is not None
        walking = lambda e, lm: (lm["chosen"] == "walk" if lm else True) and (e["walk_min"] or 0) > cfg["long_walk_min"]  # noqa: E731
        long_walk = walking(o, lm_o) or walking(de, lm_d)
        warns = [w for w in plan["warnings"]]
        if long_walk:  # with last-mile data this only happens when the user forced walking
            warns.append(f"The walk to or from the station is over {cfg['long_walk_min']} minutes." if has_lm else
                         f"The walk to or from the station is over {cfg['long_walk_min']} minutes. A short auto or Rapido may be needed; its cost is not included.")
        if fare is None:
            warns.append("The ticket price is not available for this trip.")
        lmc = m_slot["last_mile_cost_inr"]
        breakdown = [{"label": "Metro fare", "inr": fare}] if fare is not None else []
        for label, lm in (("To the station", lm_o), ("From the station", lm_d)):
            if lm:
                c = lm["cost_inr"]
                breakdown.append({"label": f"{label} ({MODE_WORDS[lm['chosen']]})", "inr": round(_mid(c["lo"], c["hi"])), "inr_lo": c["lo"], "inr_hi": c["hi"]})
        metro.update(
            available=True, reason=None,
            best={"time": m_slot["time"], "door_to_door_min": m_slot["total_min"], "fare_inr": fare,
                  "cost_inr": {"lo": fare + lmc["lo"], "hi": fare + lmc["hi"]} if fare is not None else None,
                  "crowd": {"level": crowd["level"], "seat": crowd["seat"], "score": crowd["score"]} if crowd else None, "umbrella": m_slot["umbrella"]},
            access={"origin_station": o["display"], "walk_from_origin_min": o["walk_min"], "dest_station": de["display"], "walk_to_dest_min": de["walk_min"],
                    "long_walk": long_walk, "origin_last_mile": lm_o, "dest_last_mile": lm_d},
            route_summary=_route_summary(plan["route"]), breakdown=breakdown, warnings=warns)
        if has_lm:
            metro["cost_note"] = ("Ticket price plus estimated last-mile fares from standard tariffs; apps vary and prices change with demand. "
                                  f"{planner.FARE_NOTE}")

    # ---- verdict
    r_min = _mid(road["best"]["total_min"]["lo"], road["best"]["total_min"]["hi"])
    r_inr = _mid(r_cost["lo"], r_cost["hi"]) if r_cost else None
    verdict = {"pick": "road_only", "headline": VEHICLE_HEADLINE[req.vehicle], "reasons": [], "saves_min": None, "saves_inr": None, "basis": ""}
    same_time = []
    if m_slot is not None:
        m = metro["best"]
        m_min = _mid(m["door_to_door_min"]["lo"], m["door_to_door_min"]["hi"])
        lm_mid = _mid(m_slot["last_mile_cost_inr"]["lo"], m_slot["last_mile_cost_inr"]["hi"])
        m_inr = m["fare_inr"] + lm_mid if m["fare_inr"] is not None else None  # the fare plus the estimated rides to and from the station
        money = r_inr is not None and m_inr is not None  # cost enters the ranking only when both sides have one
        lvl = m["crowd"]["level"] if m["crowd"] else None
        r_g, m_g = _gen(r_min, r_inr if money else None, None, cfg), _gen(m_min, m_inr if money else None, lvl, cfg)
        pick = _pick(r_g, m_g, cfg["tie_pct"])
        verb = VEHICLE_VERB[req.vehicle]
        reasons = []
        dt = round(r_min - m_min)
        reasons.append(f"About {abs(dt)} min quicker by metro door to door at {_clock(m['time'])}" if dt >= cfg["words_min_diff_min"]
                       else f"The metro takes about {abs(dt)} min longer door to door than {verb}" if -dt >= cfg["words_min_diff_min"]
                       else "Door-to-door time is about the same")
        if money:
            di = round(r_inr - m_inr)
            reasons.append(f"Rs {di} cheaper than {verb} (fuel only)" if di >= cfg["words_min_diff_inr"]
                           else f"Rs {-di} more than the fuel for {verb}" if -di >= cfg["words_min_diff_inr"] else f"The cost is about the same as the fuel for {verb}")
        rides = [(w, lm["chosen"]) for w, lm in (("to the station", metro["access"]["origin_last_mile"]), ("from the station", metro["access"]["dest_last_mile"]))
                 if lm and lm["chosen"] != "walk"]
        if rides:
            reasons.append(f"Includes about Rs {round(lm_mid)} for " + " and ".join(f"{'an' if mode == 'auto' else 'a'} {MODE_WORDS[mode]} {w}" for w, mode in rides))
        if lvl:
            reasons.append(SEAT_WORDS[lvl])
        if metro["access"]["long_walk"]:
            reasons.append("The walk to or from the station is long; a short auto or Rapido may be needed")
        if road["umbrella"] or m["umbrella"]:
            reasons.append("We predict rain around this time, so please carry an umbrella")
        verdict.update(pick=pick, reasons=reasons[:4])
        verdict["headline"] = {"metro": "Take the metro", "road": VEHICLE_HEADLINE[req.vehicle],
                               "tie": "It depends on what matters to you: time, money or a seat"}[pick]
        if pick in ("metro", "road"):
            verdict["saves_min"] = max(0, round(abs(r_min - m_min)))
            verdict["saves_inr"] = max(0, round(abs(r_inr - m_inr))) if money else None
        verdict["basis"] = (f"We compare door-to-door time, cost and crowding. Time is valued at Rs {cfg['value_of_time_inr_per_min']:g} per minute "
                            f"and a packed train counts as {cfg['crowd_penalty_min']['packed']} extra minutes."
                            + ("" if money else " Cost is left out because one side has no rupee figure."))
        # every half hour where both modes have a slot
        m_by = {s["time"]: s for s in metro_days[chosen]["slots"]}
        for s in live(cday):
            ms = m_by.get(s["time"])
            if not ms:
                continue
            rc = round(s["fuel"]["p50_inr"]) if s.get("fuel") else None
            mi = m["fare_inr"] + _mid(ms["last_mile_cost_inr"]["lo"], ms["last_mile_cost_inr"]["hi"]) if m["fare_inr"] is not None else None
            mi = round(mi) if mi is not None else None
            rg = _gen(s["p50"], rc if (rc is not None and mi is not None) else None, None, cfg)
            mg = _gen(_mid(ms["total_min"]["lo"], ms["total_min"]["hi"]), mi if (rc is not None and mi is not None) else None,
                      ms["crowd"]["level"] if ms["crowd"] else None, cfg)
            same_time.append({"time": s["time"], "road_min": round(s["p50"]), "metro_min": round(_mid(ms["total_min"]["lo"], ms["total_min"]["hi"])),
                              "road_inr": rc, "metro_inr": mi, "pick": _pick(rg, mg, cfg["tie_pct"])})
    else:
        verdict["reasons"] = [metro["reason"]]
        verdict["basis"] = "The metro could not be compared for this trip, so only the road estimate is shown."

    # ---- the bus joins the race: all available modes are ranked on the same generalised minutes (time, money at the value of time, crowding)
    bus_side = _bus_side(req, d, r_slot["time"])
    if bus_side["available"]:
        b = bus_side["best"]
        cands = {"road": (r_min, r_inr, None)}
        if m_slot is not None:
            cands["metro"] = (m_min, m_inr, lvl)
        cands["bus"] = (b["door_to_door_min"]["lo"], _mid(b["cost_inr"]["lo"], b["cost_inr"]["hi"]) if b["cost_inr"] else None, b["crowd"]["level"])
        money = all(c[1] is not None for c in cands.values())
        g = {k: _gen(c[0], c[1] if money else None, c[2], cfg) for k, c in cands.items()}
        order = sorted(g, key=g.get)
        tie = len(order) > 1 and abs(g[order[0]] - g[order[1]]) < cfg["tie_pct"] / 100 * min(g[order[0]], g[order[1]])
        pick = "tie" if tie else order[0]
        rs = lambda c: f"Rs {c['lo']}" if c["lo"] == c["hi"] else f"Rs {c['lo']} to {c['hi']}"  # noqa: E731
        lines = [f"By road about {round(r_min)} min" + (f" and about Rs {round(r_inr)} in fuel" if money else "")]
        if m_slot is not None:
            lines.append(f"By metro about {round(m_min)} min and about Rs {round(m_inr)} with the rides to and from the stations" if money else f"By metro about {round(m_min)} min")
        lines.append(f"By bus ({b['routes']}) about {b['door_to_door_min']['lo']} min" + (f" and about {rs(b['cost_inr'])} with the rides to and from the stops" if money else ""))
        if lvl:
            lines.append(SEAT_WORDS[lvl])
        lines.append(f"The bus crowd is our guess: {b['crowd']['label'].lower()}")
        if road["umbrella"] or (m_slot is not None and metro["best"]["umbrella"]):
            lines.append("We predict rain around this time, so please carry an umbrella")
        bus_line = lines[2 if m_slot is not None else 1]
        if "bus" in order[:2]:  # the bus wins or is a close second: the road-and-metro reasons no longer explain the pick, so give the three-way picture
            verdict["reasons"] = lines[:5]
        elif len(verdict["reasons"]) < 4:  # otherwise keep the road-and-metro reasons and add one line about the bus
            verdict["reasons"] = verdict["reasons"] + [bus_line]
        verdict["pick"] = pick
        verdict["headline"] = {"metro": "Take the metro", "road": VEHICLE_HEADLINE[req.vehicle], "bus": f"Take the bus ({b['routes']})",
                               "tie": "It depends on what matters to you: time, money or a seat"}[pick]
        if pick != "tie" and len(order) > 1:
            nxt = cands[order[1]]
            verdict["saves_min"] = max(0, round(abs(nxt[0] - cands[pick][0])))
            verdict["saves_inr"] = max(0, round(abs(nxt[1] - cands[pick][1]))) if money else None
        verdict["basis"] = (f"We compare door-to-door time, cost and crowding across road, metro and bus. Time is valued at Rs {cfg['value_of_time_inr_per_min']:g} per minute "
                            f"and a packed train or bus counts as {cfg['crowd_penalty_min']['packed']} extra minutes."
                            + ("" if money else " Cost is left out because one side has no rupee figure."))

    warnings = list(est["warnings"])
    if road["cost_note"].startswith("Add your"):
        warnings.append("No road cost is shown because no fuel type and mileage were given.")
    return {"origin": est["origin"], "dest": est["dest"], "distance_km": est["distance_km"], "date": chosen, "weekday": d.strftime("%a"),
            "label": _label(d), "days_available": days_available, "road": road, "metro": metro, "bus": bus_side, "verdict": verdict,
            "same_time": same_time, "warnings": warnings, "disclaimer": DISCLAIMER, "attribution": planner.ATTRIBUTION}
