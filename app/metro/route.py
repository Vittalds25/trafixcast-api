"""Shortest route on the line graph. A state is (station, line you are on); riding one stop costs the line's
minutes-per-station, changing line at an interchange costs the walk plus a penalty so fewer changes win."""
import heapq
from dataclasses import dataclass, field

from app.metro.data import Net, engine


@dataclass
class Leg:
    line: str
    stations: list[str]
    towards_id: str


@dataclass
class Change:
    station_id: str
    from_line: str
    to_line: str
    walk_min: float
    confidence: str


@dataclass
class Route:
    legs: list[Leg] = field(default_factory=list)
    changes: list[Change] = field(default_factory=list)

    @property
    def stops(self) -> int:
        return sum(len(leg.stations) - 1 for leg in self.legs)


def transfer_walk(net: Net, sid: str, a: str, b: str) -> tuple[float, str]:
    for x in net.stations[sid].get("interchange") or []:
        if {x.get("from"), x.get("to")} == {a, b} and x.get("walk_min") is not None:
            return float(x["walk_min"]), x.get("confidence", "unknown")
    return float(engine()["default_transfer_walk_min"]), "unknown"


def find_route(net: Net, origin: str, dest: str) -> Route | None:
    cfg = engine()
    heap, seen, prev, n = [], {}, {}, 0
    for lid in net.station_lines[origin]:
        heap.append((0.0, n, (origin, lid), None))
        n += 1
    heapq.heapify(heap)
    goal = None
    while heap:
        cost, _, state, parent = heapq.heappop(heap)
        if state in seen:
            continue
        seen[state], prev[state] = cost, parent
        sid, lid = state
        if sid == dest:
            goal = state
            break
        i = net.pos[(lid, sid)]
        step = net.lines[lid].get("avg_interstation_min", 2.0)
        for j in (i - 1, i + 1):
            if 0 <= j < len(net.order[lid]) and net.operational(net.order[lid][j]):
                n += 1
                heapq.heappush(heap, (cost + step, n, (net.order[lid][j], lid), state))
        for other in net.station_lines[sid]:
            if other != lid:
                n += 1
                heapq.heappush(heap, (cost + transfer_walk(net, sid, lid, other)[0] + cfg["transfer_penalty_min"], n, (sid, other), state))
    if goal is None:
        return None
    path = []
    while goal is not None:
        path.append(goal)
        goal = prev[goal]
    path.reverse()
    route = Route()
    for (sid, lid) in path:
        if route.legs and route.legs[-1].line == lid:
            route.legs[-1].stations.append(sid)
        elif route.legs and route.legs[-1].stations[-1] == sid:  # changed line here
            walk, conf = transfer_walk(net, sid, route.legs[-1].line, lid)
            route.changes.append(Change(sid, route.legs[-1].line, lid, walk, conf))
            route.legs.append(Leg(lid, [sid], ""))
        else:
            route.legs.append(Leg(lid, [sid], ""))
    for leg in route.legs:
        ends = net.lines[leg.line]["terminals"]
        leg.towards_id = ends[1] if net.pos[(leg.line, leg.stations[-1])] > net.pos[(leg.line, leg.stations[0])] else ends[0]
    return route
