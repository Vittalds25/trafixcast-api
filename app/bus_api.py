"""Bus endpoint: POST /v1/bus/plan. BMTC buses between two places: which bus, when, where to change, the fare, ordinary against AC.
Scheduled data from the BMTC app feed, never live."""
import re
from datetime import date as Date
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app.bus import planner
from app.bus.data import get_bus
from app.limits import limiter
from app.providers import get_providers
from app.schemas import Place
from app.service import IST, resolve

router = APIRouter(prefix="/v1/bus")
NO_DATA = "Bus data is not available right now."


class BusPlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    origin: Place
    dest: Place
    depart_time: str | None = Field(default=None, max_length=5, description="HH:MM, 24-hour; default: now")
    last_mile: Literal["best", "walk", "bike_taxi", "auto"] = "best"  # how to get to the first bus stop and from the last one
    date: Date | None = None  # the day you travel (today up to 6 days ahead); holidays and weekends are quieter


@router.post("/plan")
@limiter.limit("30/minute;300/hour")
def bus_plan(request: Request, body: BusPlanRequest, providers=Depends(get_providers)):
    try:
        bus = get_bus()
    except (OSError, KeyError, ValueError):
        raise HTTPException(503, NO_DATA) from None
    if body.depart_time:
        m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", body.depart_time.strip())
        if not m:
            raise HTTPException(422, "Give the time as HH:MM, for example 08:30.")
        depart = int(m[1]) * 60 + int(m[2])
    else:
        now = datetime.now(IST)
        depart = now.hour * 60 + now.minute
    today = datetime.now(IST).date()
    if body.date is not None and not 0 <= (body.date - today).days <= 6:
        raise HTTPException(422, "I can only look up to 7 days ahead.")
    (op, on), (dp, dn) = resolve(body.origin), resolve(body.dest)
    return planner.plan(bus, {"lat": op.lat, "lon": op.lon, "name": on}, {"lat": dp.lat, "lon": dp.lon, "name": dn}, depart, body.date or today, body.last_mile, providers)
