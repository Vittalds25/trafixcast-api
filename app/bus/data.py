"""BMTC bus data: stops, routes, the usual stop sequence of each route and direction with scheduled minutes and departures, and
stop-pair fares. Built by tools_bus_build.py into config/bus/bus.json.gz (community GTFS, ODbL 1.0). Scheduled data, never live."""
import gzip
import json
import math
from collections import defaultdict
from functools import lru_cache

from app.config import CONFIG_DIR

FILE = CONFIG_DIR / "bus" / "bus.json.gz"
CELL = 0.003  # degrees, about 330 m: the grid used to find stops near a point
CLASS_LABEL = {"ordinary": "Ordinary (non-AC)", "vajra": "Vajra (AC)", "vayu_vajra": "Vayu Vajra (AC, airport)"}
AC = ("vajra", "vayu_vajra")
# BMTC adult fare charts effective 4/5 Jan 2025, rupees per stage (a stage is 2 km; 25 stages = 50 km). Vajra includes 5% GST. No Vayu Vajra chart was readable.
STAGES = {
    "ordinary": [6, 12, 18, 23, 23, 24, 24, 28, 28, 28] + [30] * 10 + [32] * 5,
    "vajra": [15, 20, 25, 30, 30, 35, 35, 40, 40, 40, 45, 45, 45, 50, 50, 50, 55, 55, 55, 60, 60, 60, 65, 65, 65],
}


class Bus:
    def __init__(self, raw: dict):
        self.source, self.feed = raw["source"], raw["feed"]
        self.stops = raw["stops"]  # [name, lat, lon, zone, towards]
        self.routes = raw["routes"]  # rid -> [short, long, class]
        self.patterns = raw["patterns"]  # [rid, direction, headsign, [stop idx], [offset min], [departures min]]
        self.zone_fare, self.route_fare = raw["fares"]["zone"], raw["fares"]["route"]
        self.stop_pats = defaultdict(list)
        for pi, p in enumerate(self.patterns):
            for pos, s in enumerate(p[3]):
                self.stop_pats[s].append((pi, pos))
        self.grid = defaultdict(list)
        for i, s in enumerate(self.stops):
            self.grid[(int(s[1] / CELL), int(s[2] / CELL))].append(i)

    def near(self, lat: float, lon: float, max_m: float, limit: int | None = None) -> list[tuple[float, int]]:
        """[(metres, stop index)] nearest first, within max_m (straight line)."""
        r = int(max_m / 111000 / CELL) + 1
        cx, cy = int(lat / CELL), int(lon / CELL)
        out = []
        for x in range(cx - r, cx + r + 1):
            for y in range(cy - r, cy + r + 1):
                for i in self.grid.get((x, y), ()):
                    m = metres(lat, lon, self.stops[i][1], self.stops[i][2])
                    if m <= max_m:
                        out.append((m, i))
        out.sort()
        return out[:limit] if limit else out

    def fare(self, pi: int, i: int, j: int):
        """(rupees, source) for riding pattern pi from position i to j. "app": the stop-pair fare in the BMTC app data. "table": that pair is
        missing, so it is read off BMTC's January 2025 stage charts at one stage per 2 km along the route (an estimate). (None, None) if neither."""
        rid, _, _, stops, _, _ = self.patterns[pi]
        cls = self.routes[rid][2]
        key = f"{self.stops[stops[i]][3]}|{self.stops[stops[j]][3]}"
        f = self.route_fare.get(rid, {}).get(key) if cls in AC else self.zone_fare.get(key)
        if f is not None:
            return (int(f) if f == int(f) else round(f, 1)), "app"
        table = STAGES.get(cls)
        if table is None:
            return None, None
        km = sum(metres(*self.stops[stops[x]][1:3], *self.stops[stops[x + 1]][1:3]) for x in range(i, j)) / 1000
        return table[min(25, max(1, math.ceil(km / 2))) - 1], "table"


def metres(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    dy = (lat1 - lat2) * 111320
    dx = (lon1 - lon2) * 111320 * math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot(dx, dy)


@lru_cache
def get_bus() -> Bus:
    with gzip.open(FILE, "rt", encoding="utf-8") as f:
        return Bus(json.load(f))
