"""Metro endpoints: station list and detail, nearest station, and the 7-day plan. Estimates only, never live."""
import math
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from datetime import date as Date
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app import places
from app.limits import limiter
from app.metro import planner
from app.metro.data import get_net, norm
from app.providers import get_providers
from app.service import IST, areas

router = APIRouter(prefix="/v1/metro")
UNKNOWN_STATION = "We do not know that station."


class Ref(BaseModel):
    """A station by id, or a place by coordinates (snapped to the nearest station)."""
    model_config = ConfigDict(extra="forbid")
    station_id: str | None = Field(default=None, max_length=120)
    lat: float | None = None
    lon: float | None = None
    name: str | None = Field(default=None, max_length=80)

    @model_validator(mode="after")
    def _one_kind(self):
        pin = self.lat is not None or self.lon is not None
        if (self.station_id is None) == (not pin):
            raise ValueError("Give either station_id or both lat and lon.")
        if pin and (self.lat is None or self.lon is None or not (math.isfinite(self.lat) and math.isfinite(self.lon))
                    or not (-90 <= self.lat <= 90 and -180 <= self.lon <= 180)):
            raise ValueError("Give a real lat and lon.")
        return self


class PlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    origin: Ref
    dest: Ref
    window_start_hour: int = Field(ge=0, le=23)
    window_end_hour: int = Field(ge=1, le=24)
    weekdays_only: bool = False
    date: Date | None = None
    last_mile: Literal["best", "walk", "bike_taxi", "auto"] = "best"  # how to get to and from the station

    @model_validator(mode="after")
    def _window(self):
        if self.window_end_hour <= self.window_start_hour:
            raise ValueError("The end of the window must be after the start.")
        return self


def _net():
    try:
        return get_net()
    except (OSError, KeyError, ValueError):
        raise HTTPException(503, "Metro data is not available right now.") from None


def _day(d: Date | None, now: datetime) -> Date:
    d = d or now.date()
    if not 0 <= (d - now.date()).days <= 6:
        raise HTTPException(422, "I can only look up to 7 days ahead.")
    return d


@router.get("/stations")
@limiter.limit("60/minute")
def list_stations(request: Request):
    net = _net()
    return {"updated": net.updated, "stations": [net.summary(sid) for sid in net.stations]}


_areas_cache: dict = {}  # id(net) -> list; the network never changes while the process runs
_remote = ThreadPoolExecutor(4)  # place lookups run here so a slow map service can be given up on after a hard budget
REMOTE_BUDGET_S = 3.0


def _areas(net) -> list[dict]:
    """Every Bengaluru area with its nearest operational stations (straight-line, no lookups), computed once."""
    if id(net) not in _areas_cache:
        out = []
        for a in areas("bengaluru").values():
            r = planner.nearest(net, a["lat"], a["lon"], a["name"], 3)["results"]
            out.append({"id": a["id"], "name": a["name"], "lat": a["lat"], "lon": a["lon"],
                        "nearest": r[0] if r else None, "alternatives": r[1:]})
        _areas_cache.clear()
        _areas_cache[id(net)] = out
    return _areas_cache[id(net)]


def warm():
    """Called at startup: loads every metro file and builds the areas table, so the first visitor does not wait for it."""
    _areas(get_net())


def _match_area(net, text: str):
    """An area for what was typed: same forgiving matching as the app (case, spaces, hyphens, word prefixes)."""
    n = norm(text)
    if len(n) < 3:
        return None
    rows = [(norm(a["name"]), a) for a in _areas(net)]
    for test in (lambda k: k == n, lambda k: k.startswith(n),
                 lambda k: all(any(w.startswith(q) for w in k.split()) for q in n.split())):
        hits = [a for k, a in rows if test(k)]
        if hits:
            return min(hits, key=lambda a: len(a["name"]))
    return None


@router.get("/areas")
@limiter.limit("30/minute")
def list_areas(request: Request):
    return {"areas": _areas(_net())}


@router.get("/stations/{station_id}")
@limiter.limit("60/minute")
def station(request: Request, station_id: str, date: Date | None = None, providers=Depends(get_providers)):
    net = _net()
    now = datetime.now(IST)
    if not net.operational(station_id):
        raise HTTPException(404, UNKNOWN_STATION)
    return planner.station_info(net, station_id, _day(date, now), providers, now)


@router.get("/nearest")
@limiter.limit("30/minute;300/hour")  # a text query is a place lookup on a shared free map service
def nearest(request: Request, lat: float | None = Query(None, ge=-90, le=90), lon: float | None = Query(None, ge=-180, le=180),
            q: str | None = Query(None, min_length=2, max_length=100), city: str = Query("bengaluru", max_length=20),
            limit: int = Query(3, ge=1, le=5)):
    if city != "bengaluru":
        raise HTTPException(422, "The metro planner covers Bengaluru only.")
    net = _net()
    if (lat is None) != (lon is None) or (lat is None and q is None):
        raise HTTPException(422, "Give lat and lon, or a place name in q.")
    name = None
    if lat is None:
        sid, _ = net.find(q)
        area = None if sid else _match_area(net, q)
        if sid:  # a station name needs no map lookup
            lat, lon, name = net.stations[sid]["lat"], net.stations[sid]["lon"], net.display(sid)
        elif area:  # nor does a known area
            lat, lon, name = area["lat"], area["lon"], area["name"]
        else:
            try:
                found = _remote.submit(places.search, q, city).result(timeout=REMOTE_BUDGET_S)["results"]
            except ConnectionError as e:
                raise HTTPException(503, str(e)) from None
            except FuturesTimeout:
                raise HTTPException(503, "Place search is taking too long. Try again, or pick a station or area.") from None
            if not found:
                raise HTTPException(404, "We could not find that place in Bengaluru.")
            lat, lon, name = found[0]["lat"], found[0]["lon"], found[0]["name"]
    return planner.nearest(net, lat, lon, name, limit)


@router.post("/plan")
@limiter.limit("10/minute;120/hour")  # one weather lookup per plan
def plan(request: Request, body: PlanRequest, providers=Depends(get_providers)):
    now = datetime.now(IST)
    d = _day(body.date, now) if body.date else None
    return planner.plan(_net(), body.origin.model_dump(), body.dest.model_dump(), body.window_start_hour, body.window_end_hour,
                        body.weekdays_only, d, providers, now, body.last_mile)
