"""A guess at how full a BMTC bus is, because BMTC publishes no occupancy. Riders are split into kinds (IT commuters, other office workers, students,
domestic and daily-wage workers, shoppers and errands, seniors and hospital visits, airport travellers, leisure ...). For a bus boarded at some hour we add
up the kinds that are travelling then and that the route serves, then scale by the kind of bus and how many buses run. Every number is an assumption in
config/bus/crowd.yaml. The result is always labelled a guess, never measured and never live."""
from bisect import bisect_left, bisect_right
from functools import lru_cache

from app.config import load_yaml

LABEL = {"low": "Seats likely", "moderate": "Standing room, a seat is possible", "high": "Crowded, a seat is unlikely", "packed": "Likely packed, no seats"}
NOTE = ("A guess from office-hour patterns, who travels on the route (IT commuters, students, shoppers and others), the kind of bus and how many buses run. "
        "BMTC does not publish how full its buses are, so this is not measured and not live.")


@lru_cache
def cfg() -> dict:
    return load_yaml("bus/crowd.yaml")


def _hour(seg: dict, minute: float) -> float:
    h = (round(minute) // 60) % 24
    return max((w for a, b, w in seg["windows"] if a <= h < b), default=seg["base_hour"])


def segment_hits(stop_names) -> dict:
    """{segment key: number of stops on the route whose name has one of its words}, for the segments that have words."""
    names = [n.lower() for n in stop_names]
    return {k: sum(1 for n in names if any(w in n for w in s["words"])) for k, s in cfg()["segments"].items() if s["words"]}


def _fit(seg: dict, hits: int) -> float:
    f = seg["fit"]
    return min(f["cap"], f["base"] + f["per_hit"] * hits)


def buses_per_hour(deps: list, off: float, board_min: float, usable: bool):
    """Buses an hour passing the boarding stop around board_min (half an hour either side), or None when the listed timetable is too thin."""
    if not usable:
        return None
    return bisect_right(deps, board_min + 30 - off) - bisect_left(deps, board_min - 30 - off)


def guess(board_min: float, cls: str, short: str, hits: dict, per_hour, day_type: str = "weekday") -> dict:
    """{level, label, note, why[], mix[{key,label,pct}], score} for a bus boarded at minute-of-day board_min."""
    c = cfg()
    ordinary = cls == "ordinary"
    demand, weights = 0.0, {}
    for k, s in c["segments"].items():
        base = s["share"] * _hour(s, board_min) * _fit(s, hits.get(k, 0)) * c["day_factor"][k][day_type]
        demand += base * (s["shakti"] if ordinary else 1.0)
        weights[k] = base * (s["shakti"] if ordinary else s["ac"])  # who is aboard: AC is a price filter, ordinary has the Shakti riders
    score = c["scale"] * demand * c["class_factor"][cls]
    why = []
    if day_type != "weekday":
        why.append({"holiday": "a public holiday", "sunday": "a Sunday", "saturday": "a Saturday"}[day_type] + ": fewer commuters")
    top = max(weights, key=weights.get)
    why.append(f"mostly {c['segments'][top]['label']} at this hour")
    if short.upper().startswith("MF"):
        score *= c["feeder_factor"]
        why.append("a short metro-feeder route")
    if not ordinary:
        why.append("AC buses cost more and are usually less full")
    else:
        why.append("ordinary buses fill faster, partly because women ride free (Shakti)")
    sp = c["supply"]
    if per_hour is not None:
        score *= min(sp["max_factor"], max(sp["min_factor"], (sp["neutral_per_hour"] / max(per_hour, 1)) ** sp["exponent"]))
        why.append(f"about {per_hour} bus{'es' if per_hour != 1 else ''} an hour listed")
    level = next(lv for limit, lv in c["bands"] if score < limit)
    total = sum(weights.values()) or 1
    mix = [{"key": k, "label": c["segments"][k]["label"], "pct": 5 * round(100 * w / total / 5)} for k, w in sorted(weights.items(), key=lambda x: -x[1])]
    mix = [m for m in mix if m["pct"] >= 5][:5]
    return {"level": level, "label": LABEL[level], "note": NOTE, "why": why, "mix": mix, "score": round(score, 2)}
