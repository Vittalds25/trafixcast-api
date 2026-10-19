"""BMTC bus planner: which bus to take between two places, when it leaves, where to change, what it costs, and ordinary (non-AC) against
AC (Vajra, Vayu Vajra) side by side. Direct buses and trips with one change. Scheduled times and the stop-pair fares from the BMTC app data
(see data.py); nothing is live and there is no crowd data."""
from bisect import bisect_left
from collections import defaultdict
from datetime import date, datetime

from functools import lru_cache

from fastapi import HTTPException

from app.bus import crowd
from app.bus.data import AC, CLASS_LABEL, Bus, metres
from app.config import load_yaml
from app.metro import lastmile
from app.metro.planner import holiday_name
from app.providers.curated import YamlHolidays
from app.service import IST

_HUBS: dict = {}  # pattern index -> {segment: stops on it that suit that kind of rider} (the data never changes while the process runs)
LEVELS = ["low", "moderate", "high", "packed"]

WALK_M_PER_MIN, DETOUR = 80, 1.3  # the same walking pace and street detour the metro planner uses
RADII = (600, 1000, 1500)  # metres from the start and the end to a stop; the first that finds stops is used
MAX_STOPS = 12
CHANGE_WALK_M = 250  # the longest walk between the stop you get off and the stop you board the next bus
CHANGE_BUFFER_MIN = 2
MAX_WAIT_MIN, MAX_TOTAL_MIN = 180, 360  # longer than this and the trip is not shown; a wait over LONG_WAIT_MIN is flagged
LONG_WAIT_MIN = 60
TOP = 6
ATTRIBUTION = "Bus data: BMTC, via Vonter/bmtc-gtfs (ODbL 1.0). Scheduled times and fares from the Namma BMTC app data, not live."
DISCLAIMER = ("Scheduled times, not live: buses can run late, change route or not run. Fares are the stop-to-stop fares in the BMTC app data; "
              "check the ticket price. Bus crowd levels are our guess (BMTC publishes no occupancy), not measured.")
SHAKTI_NOTE = ("Shakti: women and transgender women who live in Karnataka travel free on ordinary (non-AC) BMTC buses; they still collect a "
               "zero-value ticket and show a valid ID. AC buses (Vajra, Vayu Vajra) charge the full fare. Based on news reports of the scheme.")


MIN_LM_M = 150  # a shorter way than this to the first stop (or from the last) is simply walked


@lru_cache
def _lm() -> dict:
    return load_yaml("metro/lastmile.yaml")  # the same walk, bike taxi and auto tariffs the metro planner uses


def _end(straight_m: float, clock: float, forced: str) -> dict:
    """Getting to the first bus stop, or from the last one to where you are going: walk, bike taxi or auto, with the metro planner's tariffs.
    {desc, mid minutes, cost}."""
    m = straight_m * DETOUR
    opts = lastmile.options(_lm(), m, clock)
    chosen, _ = lastmile.choose(opts, "walk" if m < MIN_LM_M else forced)
    return {"desc": lastmile.describe(m, opts, chosen), "mid": (chosen["minutes"]["lo"] + chosen["minutes"]["hi"]) / 2, "cost": chosen["cost_inr"]}


def walk_min(m: float) -> int:
    return max(1, round(m * DETOUR / WALK_M_PER_MIN))


def hhmm(t: float) -> str:
    t = round(t) % 1440
    return f"{t // 60:02d}:{t % 60:02d}"


def _every(deps: list, k: int):
    """Typical minutes between buses around departure number k (an hour either side), or None."""
    near = [d for d in deps[max(0, k - 12): k + 12] if abs(d - deps[k]) <= 60]
    return round((near[-1] - near[0]) / (len(near) - 1)) if len(near) >= 3 else None


def _leg(bus: Bus, pi: int, i: int, j: int, t_at: float, day_type: str = "weekday") -> dict | None:
    """Ride pattern pi from position i to j, for a traveller who reaches the boarding stop at minute t_at. None if no bus is left."""
    rid, _, head, stops, off, deps = bus.patterns[pi]
    k = bisect_left(deps, t_at - off[i])
    if k == len(deps):
        return None
    short, long, cls = bus.routes[rid]
    fare, fare_src = bus.fare(pi, i, j)
    sa, sb = bus.stops[stops[i]], bus.stops[stops[j]]
    board, alight = deps[k] + off[i], deps[k] + off[j]
    if pi not in _HUBS:
        _HUBS[pi] = crowd.segment_hits(bus.stops[s][0] for s in stops)
    usable = len(deps) >= crowd.cfg()["supply"]["min_departures_listed"]
    guess = crowd.guess(board, cls, short, _HUBS[pi], crowd.buses_per_hour(deps, off[i], board, usable), day_type)
    return {
        "crowd": guess, "route": short, "route_name": long, "class": cls, "class_label": CLASS_LABEL[cls], "ac": cls in AC, "towards": head,
        "board": {"name": sa[0], "towards": sa[4], "lat": sa[1], "lon": sa[2]}, "alight": {"name": sb[0], "towards": sb[4], "lat": sb[1], "lon": sb[2]},
        "stops": j - i, "board_time": hhmm(board), "alight_time": hhmm(alight), "ride_min": round(alight - board), "wait_min": round(board - t_at),
        "next_buses": [hhmm(deps[x] + off[i]) for x in range(k, min(k + 3, len(deps)))], "every_min": _every(deps, k),
        "fare_inr": fare, "fare_source": fare_src, "free_for_eligible_women": cls == "ordinary",
        "_board": board, "_alight": alight,
    }


def _candidates(bus: Bus, boards, alights, depart: float, classes: set):
    """Cheap estimates of every direct bus and every one-change trip (no timetable look-ups yet), best per route (pair) first."""
    alight_d = {s: m for m, s in alights}
    direct, change = {}, {}
    d_pat = defaultdict(list)
    for m, s in alights:
        for pi, j in bus.stop_pats[s]:
            d_pat[pi].append((j, m))
    near_x = {}
    for mb, b in boards:
        for pi, i in bus.stop_pats[b]:
            rid, _, _, stops, off, _ = bus.patterns[pi]
            cls = bus.routes[rid][2]
            if cls not in classes:
                continue
            short = bus.routes[rid][0]
            for j in range(i + 1, len(stops)):
                ma = alight_d.get(stops[j])
                if ma is not None:
                    est = walk_min(mb) + 5 + off[j] - off[i] + walk_min(ma)
                    if short not in direct or est < direct[short][0]:
                        direct[short] = (est, pi, i, j, mb, ma)
            for kk in range(i + 1, min(len(stops), i + 70)):
                x = stops[kk]
                if x not in near_x:
                    near_x[x] = bus.near(bus.stops[x][1], bus.stops[x][2], CHANGE_WALK_M)
                for mx, y in near_x[x]:
                    for pj, ypos in bus.stop_pats[y]:
                        if pj == pi or pj not in d_pat or bus.routes[bus.patterns[pj][0]][2] not in classes:
                            continue
                        off2 = bus.patterns[pj][4]
                        for j, ma in d_pat[pj]:
                            if j > ypos:
                                est = walk_min(mb) + 5 + off[kk] - off[i] + walk_min(mx) + 5 + off2[j] - off2[ypos] + walk_min(ma)
                                key = (short, bus.routes[bus.patterns[pj][0]][0])
                                if key not in change or est < change[key][0]:
                                    change[key] = (est, pi, i, kk, pj, ypos, j, mb, mx, ma)
    return sorted(direct.values())[:TOP], sorted(change.values())[:TOP * 3]


def _option(legs: list, e_o: dict, e_d: dict, depart: float, changes: list) -> dict:
    """e_o and e_d: the way to the first stop and from the last one (see _end). The totals include them: minutes, and rupees for the bus fares
    plus any bike taxi or auto at either end."""
    last = legs[-1]["_alight"]
    fares = [lg["fare_inr"] for lg in legs]
    ac_fares = [lg["fare_inr"] for lg in legs if lg["ac"]]
    classes = {lg["class"] for lg in legs}
    worst = max((lg["crowd"] for lg in legs), key=lambda c: LEVELS.index(c["level"]))
    lm = {"lo": e_o["cost"]["lo"] + e_d["cost"]["lo"], "hi": e_o["cost"]["hi"] + e_d["cost"]["hi"]}
    total = lambda fare: None if fare is None else {"lo": round(fare + lm["lo"]), "hi": round(fare + lm["hi"])}  # noqa: E731
    return {
        "crowd": {"level": worst["level"], "label": worst["label"]},
        "kind": "direct" if len(legs) == 1 else "change", "legs": legs, "changes": changes,
        "last_mile": {"origin": e_o["desc"], "dest": e_d["desc"], "cost_inr": lm}, "to_first_min": round(e_o["mid"]), "from_last_min": round(e_d["mid"]),
        "leave_by": hhmm(depart), "arrive_time": hhmm(last + e_d["mid"]), "total_min": round(last + e_d["mid"] - depart),
        "fare_inr": None if None in fares else sum(fares), "fare_estimated": any(lg["fare_source"] == "table" for lg in legs),
        "fare_inr_if_free_for_women": None if None in ac_fares else sum(ac_fares),  # only the AC legs are charged
        "total_cost_inr": total(None if None in fares else sum(fares)),  # bus fares plus the ride to the first stop and from the last
        "total_cost_inr_if_free_for_women": total(None if None in ac_fares else sum(ac_fares)),
        "long_wait": any(lg["wait_min"] > LONG_WAIT_MIN for lg in legs) or any(c["wait_min"] > LONG_WAIT_MIN for c in changes), "all_ordinary": classes == {"ordinary"}, "all_ac": all(lg["ac"] for lg in legs),
        "mixed": len(classes) > 1 and not all(lg["ac"] for lg in legs) and any(lg["ac"] for lg in legs),
    }


def _plan_classes(bus: Bus, boards, alights, depart: float, classes: set, day_type: str = "weekday", lm_mode: str = "best") -> list[dict]:
    direct, change = _candidates(bus, boards, alights, depart, classes)
    out = []
    for est, pi, i, j, mb, ma in direct:
        e_o = _end(mb, depart, lm_mode)
        lg = _leg(bus, pi, i, j, depart + e_o["mid"], day_type)
        if lg:
            out.append(_option([lg], e_o, _end(ma, lg["_alight"], lm_mode), depart, []))
    for est, pi, i, kk, pj, ypos, j, mb, mx, ma in change:
        e_o = _end(mb, depart, lm_mode)
        a = _leg(bus, pi, i, kk, depart + e_o["mid"], day_type)
        if not a:
            continue
        b = _leg(bus, pj, ypos, j, a["_alight"] + walk_min(mx) + CHANGE_BUFFER_MIN, day_type)
        if not b:
            continue
        changes = [{"get_off": a["alight"]["name"], "board": b["board"]["name"], "walk_m": round(mx), "walk_min": walk_min(mx) if mx else 0,
                    "wait_min": round(b["_board"] - a["_alight"] - (walk_min(mx) if mx else 0))}]
        out.append(_option([a, b], e_o, _end(ma, b["_alight"], lm_mode), depart, changes))
    # the data lists few departures for many routes: a bus that only comes hours later is not an option worth showing
    out = [x for x in out if x["total_min"] <= MAX_TOTAL_MIN and all(lg["wait_min"] <= MAX_WAIT_MIN for lg in x["legs"]) and
           all(c["wait_min"] <= MAX_WAIT_MIN for c in x["changes"])]
    out.sort(key=lambda o: (o["total_min"], len(o["legs"])))
    return out


def _stops_near(bus: Bus, lat: float, lon: float, what: str, radius: float):
    if not bus.near(lat, lon, radius, 1):
        raise HTTPException(422, f"There is no BMTC bus stop within {radius / 1000:g} km of {what}.")


def _brief(o: dict | None) -> dict | None:
    if o is None:
        return None
    lg = o["legs"]
    return {"crowd": o["crowd"], "routes": " then ".join(x["route"] for x in lg), "total_min": o["total_min"], "fare_inr": o["fare_inr"], "arrive_time": o["arrive_time"],
            "total_cost_inr": o["total_cost_inr"], "total_cost_inr_if_free_for_women": o["total_cost_inr_if_free_for_women"],
            "next_bus": lg[0]["board_time"], "changes": len(lg) - 1, "wait_min": lg[0]["wait_min"], "every_min": lg[0]["every_min"]}


def _compare(ordinary: dict | None, ac: dict | None) -> dict:
    """Best ordinary-only trip against best AC-only trip: minutes, fares, and a plain pick."""
    out = {"ordinary": _brief(ordinary), "ac": _brief(ac), "pick": None, "saves_min": None, "extra_fare_inr": None, "reasons": []}
    if ordinary and ac:
        dt = ordinary["total_min"] - ac["total_min"]
        out["saves_min"] = dt
        if ordinary["fare_inr"] is not None and ac["fare_inr"] is not None:
            out["extra_fare_inr"] = ac["fare_inr"] - ordinary["fare_inr"]
        # dt > 0: the AC bus is quicker. A clearly quicker AC bus wins; an ordinary bus that is as quick, or quicker, wins; in between the fare decides
        out["pick"] = "ac" if dt >= 8 else "ordinary" if dt <= -3 or (out["extra_fare_inr"] or 0) > 0 else "similar"
        out["reasons"].append(f"The AC bus is about {abs(dt)} min {'quicker' if dt > 0 else 'slower'} door to door" if abs(dt) >= 3 else
                              "Door to door the two take about the same time")
        mid = lambda c: (c["lo"] + c["hi"]) / 2  # noqa: E731
        if ordinary["total_cost_inr"] and ac["total_cost_inr"]:  # the whole trip: bus fares plus getting to the first stop and from the last
            ex = round(mid(ac["total_cost_inr"]) - mid(ordinary["total_cost_inr"]))
            out["reasons"].append(f"In all, the AC trip costs about Rs {abs(ex)} {'more' if ex > 0 else 'less'} (bus fares plus getting to and from the stops)")
        elif out["extra_fare_inr"] is not None:
            out["reasons"].append(f"AC costs Rs {abs(out['extra_fare_inr'])} {'more' if out['extra_fare_inr'] > 0 else 'less'} than the ordinary bus")
        if ordinary["crowd"]["level"] != ac["crowd"]["level"]:
            out["reasons"].append(f"Likely crowd (a guess): ordinary {ordinary['crowd']['label'].lower()}, AC {ac['crowd']['label'].lower()}")
    elif ordinary:
        out["pick"] = "ordinary"
        out["reasons"].append("No AC bus (Vajra or Vayu Vajra) serves this trip, so the ordinary bus is the choice")
    elif ac:
        out["pick"] = "ac"
        out["reasons"].append("Only an AC bus serves this trip")
    return out


def day_info(day: date) -> dict:
    """What kind of day it is for the crowd guess: a public holiday (the same Karnataka list the other planners use), a Sunday, a Saturday or a weekday."""
    if YamlHolidays().is_holiday(day):
        kind, name = "holiday", holiday_name(day)
    else:
        kind, name = ("sunday", None) if day.weekday() == 6 else ("saturday", None) if day.weekday() == 5 else ("weekday", None)
    return {"date": day.isoformat(), "label": day.strftime("%a %d %b"), "type": kind, "holiday": name}


def plan(bus: Bus, o: dict, d: dict, depart_min: int, day: date | None = None, last_mile: str = "best") -> dict:
    """o and d are {"lat", "lon", "name"}; depart_min is the minute of the day the traveller starts walking; day sets the kind of day (holidays are quieter)."""
    info = day_info(day or datetime.now(IST).date())
    day_type = info["type"]
    _stops_near(bus, o["lat"], o["lon"], o["name"], RADII[-1])  # a clear message when there is no stop at all
    _stops_near(bus, d["lat"], d["lon"], d["name"], RADII[-1])
    for r in RADII:  # walk as little as possible: widen the search only when nothing is found
        boards, alights = bus.near(o["lat"], o["lon"], r, MAX_STOPS), bus.near(d["lat"], d["lon"], r, MAX_STOPS)
        if not boards or not alights:
            continue
        ordinary = _plan_classes(bus, boards, alights, depart_min, {"ordinary"}, day_type, last_mile)
        ac = _plan_classes(bus, boards, alights, depart_min, set(AC), day_type, last_mile)
        if ordinary or ac:
            break
    options = sorted(ordinary[:4] + ac[:3], key=lambda x: x["total_min"])[:TOP]
    for opt in options:
        for lg in opt["legs"]:
            lg.pop("_board", None), lg.pop("_alight", None)
    warnings = []
    if day_type != "weekday":
        warnings.append(f"{info['holiday'] + ' (public holiday)' if info['holiday'] else info['label']}: fewer office, school and factory riders, more shoppers and "
                        "leisure trips. The schedule data has no separate holiday or weekend timetable, so the times shown are the usual ones and BMTC may run fewer buses.")
    if not options:
        warnings.append(f"No direct bus or one-change trip was found soon after {hhmm(depart_min)}. The schedule data lists few departures for many routes: "
                        "try another time, a start or end nearer a main road, or the metro.")
    best_ord = next((x for x in ordinary if x["all_ordinary"]), None)
    best_ac = next((x for x in ac if x["all_ac"]), None)
    return {
        "day": info, "origin": o["name"], "dest": d["name"], "leave_by": hhmm(depart_min), "options": options, "compare": _compare(best_ord, best_ac),
        "nearest_stops": {"origin": [{"name": bus.stops[s][0], "m": round(m)} for m, s in boards[:3]], "dest": [{"name": bus.stops[s][0], "m": round(m)} for m, s in alights[:3]]},
        "last_mile_note": _lm().get("disclaimer"), "last_mile_choice": last_mile,
        "warnings": warnings, "shakti_note": SHAKTI_NOTE, "disclaimer": DISCLAIMER, "attribution": ATTRIBUTION, "feed": bus.feed,
    }
