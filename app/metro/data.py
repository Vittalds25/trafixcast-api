"""Loads the metro network, ridership and link loads once and keeps them in memory (standard library + PyYAML only)."""
import difflib
import json
import math
import re
import threading
from datetime import date
from pathlib import Path

import yaml

from app.config import CONFIG_DIR

DIR = CONFIG_DIR / "metro"  # tests point this at backend/tests/fixtures/metro
_cache: dict = {}
_lock = threading.Lock()


def engine() -> dict:
    return _read_yaml(CONFIG_DIR / "metro" / "engine.yaml", cache=True)


def _read_yaml(path: Path, cache: bool = False):
    if cache and path in _cache:
        return _cache[path]
    v = yaml.safe_load(path.read_text(encoding="utf-8"))
    if cache:
        _cache[path] = v
    return v


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def norm(s) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(s).lower()).split())


def hm(s) -> int:
    if isinstance(s, int):  # YAML reads an unquoted 10:40 as base-60 minutes, which is already the right number
        return s
    h, m = str(s).split(":")
    return int(h) * 60 + int(m)


def hhmm(minutes: float) -> str:
    m = round(minutes)
    return f"{m // 60 % 24:02d}:{m % 60:02d}"


def km_between(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(a))


def daytype(day: date, is_holiday: bool) -> str:
    return "holiday" if is_holiday else ("saturday", "sunday")[day.weekday() - 5] if day.weekday() >= 5 else "weekday"


_HEADWAY_KEYS = {"holiday": ["holiday", "sunday", "weekend", "weekday"], "sunday": ["sunday", "weekend", "weekday"],
                 "saturday": ["saturday", "weekend", "weekday"]}


class Net:
    def fare(self, a: str, b: str) -> int | None:
        """Ticket price in rupees between two stations (Aug 2025 fare table, no discounts), None if the table is missing."""
        v = self.fares.get(a, {}).get(b)
        return int(v) if v is not None else None

    def __init__(self, network: dict, ridership: dict, segments: dict, fares: dict | None = None, lastmile: dict | None = None):
        self.lastmile = lastmile or None  # tariffs for the bike taxi / auto to and from the station; None = walking only
        self.fares = (fares or {}).get("fares", {})
        self.updated = str(network.get("updated", ""))
        self.lines = {x["id"]: x for x in network["lines"]}
        self.stations = {s["id"]: s for s in network["stations"]}
        self.order = {lid: list(x["stations"]) for lid, x in self.lines.items()}
        self.pos = {(lid, sid): i for lid, ids in self.order.items() for i, sid in enumerate(ids)}
        self.station_lines = {sid: [lid for lid in self.lines if (lid, sid) in self.pos] for sid in self.stations}
        self.ridership = ridership.get("stations", {})
        self.capacity = segments.get("capacity_per_train", {})
        self.links = {}
        for lid, dirs in segments.get("links", {}).items():
            for key, rows in dirs.items():
                towards = key[len("towards_"):] if key.startswith("towards_") else key
                self.links[(lid, towards)] = {(r["from"], r["to"]): r for r in rows}
        self.has_crowd = bool(self.links)
        self.index: dict[str, set] = {}
        for sid, s in self.stations.items():
            for text in [sid, s.get("name"), s.get("display"), *s.get("aliases", []), *s.get("codes", [])]:
                if text:
                    self.index.setdefault(norm(text), set()).add(sid)

    # ---- stations
    def operational(self, sid: str) -> bool:
        s = self.stations.get(sid)
        return bool(s) and s.get("status", "operational") == "operational" and bool(self.station_lines.get(sid))

    def display(self, sid: str) -> str:
        s = self.stations[sid]
        return s.get("display") or s["name"]

    def summary(self, sid: str) -> dict:
        s = self.stations[sid]
        lines = [{"id": lid, "name": self.lines[lid]["name"], "colour": self.lines[lid]["colour"]} for lid in self.station_lines[sid]]
        return {"id": sid, "name": s["name"], "display": self.display(sid), "aliases": s.get("aliases", []),
                "lat": s["lat"], "lon": s["lon"], "lines": lines, "interchange": len(lines) > 1,
                "status": s.get("status", "operational")}

    def find(self, text: str) -> tuple[str | None, list[str]]:
        """A station id for what the user typed (names, aliases, codes, small spelling mistakes), else None and up to
        three display names that are close. Only operational stations match."""
        n = norm(text)
        if not n:
            return None, []
        live = {k: {i for i in v if self.operational(i)} for k, v in self.index.items()}
        live = {k: v for k, v in live.items() if v}
        if n in live and len(live[n]) == 1:
            return next(iter(live[n])), []
        # whole words only, so "mg road" never matches inside "trinity mg road extension"
        hits = {i for k, v in live.items() if len(k) >= 4 and (f" {k} " in f" {n} " or (len(n) >= 4 and f" {n} " in f" {k} ")) for i in v}
        if len(hits) == 1:
            return next(iter(hits)), []
        close = difflib.get_close_matches(n, list(live), n=3, cutoff=0.6)
        if close and difflib.SequenceMatcher(None, n, close[0]).ratio() >= 0.82 and len(live[close[0]]) == 1:
            return next(iter(live[close[0]])), []
        names = []
        for c in close:
            for i in sorted(live[c]):
                if self.display(i) not in names:
                    names.append(self.display(i))
        return None, names[:3]

    def nearest(self, lat: float, lon: float, limit: int = 3) -> list[tuple[float, str]]:
        """[(straight-line km, station id)] closest first, operational stations only."""
        out = sorted((km_between(lat, lon, s["lat"], s["lon"]), sid) for sid, s in self.stations.items() if self.operational(sid))
        return out[:limit]

    # ---- timetable
    def headway(self, lid: str, day_type: str, minute: float) -> tuple[float, str, str, str]:
        """(minutes between trains, band label, confidence, daytype key used) for that line at that time of day."""
        hw = self.lines[lid].get("headway_min", {})
        key = next((k for k in _HEADWAY_KEYS.get(day_type, ["weekday"]) if hw.get(k)), None)
        bands = hw.get(key) or []
        if not bands:
            return 10.0, "", "unknown", "assumed"
        m = minute % 1440
        span = lambda b: (hm(b["from"]), hm(b["to"]) or 1440)  # noqa: E731
        b = next((b for b in bands if span(b)[0] <= m < span(b)[1]), None) or min(bands, key=lambda b: min(abs(m - span(b)[0]), abs(m - span(b)[1])))
        return float(b["min"]), f"{b['from']}-{b['to']}", b.get("confidence", "unknown"), key

    def first_last(self, lid: str, sid: str, towards_id: str, day_type: str = "weekday") -> tuple[int, int, str] | None:
        """(first train, last train as minutes after midnight, confidence) at this station heading that way. The terminal's
        time plus the stations travelled, so it is derived (never 'verified') for any other station. Sundays and holidays
        use the later Sunday first train when network.yaml has one."""
        line = self.lines[lid]
        ends = line["terminals"]
        d = "a_to_b" if towards_id == ends[1] else "b_to_a"
        f, l = line.get("first_train", {}).get(d), line.get("last_train", {}).get(d)
        if not f or not l:
            return None
        idx = self.pos[(lid, sid)]
        steps = idx if d == "a_to_b" else len(self.order[lid]) - 1 - idx
        off = steps * line.get("avg_interstation_min", 2.0)
        first_at = f.get("sunday") if day_type in ("sunday", "holiday") and f.get("sunday") else f["time"]
        first, last = hm(first_at) + off, hm(l["time"]) + off
        if last < first:
            last += 1440
        conf = min((f.get("confidence", "unknown"), l.get("confidence", "unknown")), key=["verified", "inferred", "unknown"].index)
        return round(first), round(last), ("inferred" if conf == "verified" and steps else conf)

    # ---- crowd
    def cap(self, lid: str) -> float:
        return float(self.capacity.get(lid) or self.lines[lid].get("train", {}).get("capacity") or 1800)

    def seated(self, lid: str) -> float:
        t = self.lines[lid].get("train", {})
        return float(t.get("seated") or 0.15 * self.cap(lid))  # unknown: ~15% of crush, as on the real trains

    def load_at(self, lid: str, towards_id: str, a: str, b: str, day_type: str, minute: float) -> float | None:
        """Passengers per hour on that link at that time (hourly medians, interpolated between half-hours)."""
        row = self.links.get((lid, towards_id), {}).get((a, b))
        arr = row["load"].get(day_type) if row else None
        if not arr:
            return None
        x = minute / 60 - 0.5  # an hourly figure describes the middle of its hour
        i, f = math.floor(x), x - math.floor(x)
        return (1 - f) * arr[i % 24] + f * arr[(i + 1) % 24]


def get_net() -> Net:
    d = Path(DIR)
    with _lock:
        if d not in _cache:
            net = _read_yaml(d / "network.yaml")
            _cache[d] = Net(net, _read_json(d / "ridership.json"), _read_json(d / "segments.json"), _read_json(d / "fares.json"),
                              _read_yaml(d / "lastmile.yaml") if (d / "lastmile.yaml").exists() else None)
        return _cache[d]


def reset():
    with _lock:
        _cache.clear()
