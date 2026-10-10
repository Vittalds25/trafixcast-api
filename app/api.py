from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app import chat, links, places
from app.config import get_city
from app.limits import limiter
from app.providers import get_providers
from app.schemas import EstimateRequest
from app.service import areas, build_estimate

router = APIRouter(prefix="/v1")


def _city(key: str) -> str:
    try:
        get_city(key)
    except KeyError:
        raise HTTPException(422, "We do not cover that city yet.") from None
    return key


@router.get("/areas")
def list_areas(city: str = Query("bengaluru", max_length=20)):
    # centres are public; the browser matches the user's GPS to the nearest one, so GPS never leaves the device
    return [{"id": a["id"], "name": a["name"], "lat": a["lat"], "lon": a["lon"]} for a in areas(_city(city)).values()]


@router.post("/estimate")
@limiter.limit("6/minute;60/hour")  # each uncached estimate costs real traffic-provider lookups
def estimate(request: Request, body: EstimateRequest, providers=Depends(get_providers)):
    return build_estimate(body, providers)


@router.post("/chat")
@limiter.limit("8/minute;60/hour")  # each message is a paid model call
def chat_message(request: Request, body: chat.ChatRequest, providers=Depends(get_providers)):
    return chat.reply(body, providers)


@router.get("/locate-link")
@limiter.limit("20/minute")
def locate_link(request: Request, link: str = Query(min_length=3, max_length=500), city: str = Query("bengaluru", max_length=20)):
    """Pin from a pasted Google Maps link."""
    try:
        return links.resolve(link, _city(city))
    except links.LinkError as e:
        raise HTTPException(422, str(e)) from None
    except ConnectionError as e:
        raise HTTPException(503, str(e)) from None


@router.get("/search-place")
@limiter.limit("30/minute;300/hour")
def search_place(request: Request, q: str = Query(min_length=2, max_length=100), city: str = Query("bengaluru", max_length=20)):
    """Exact-spot suggestions for the From and To boxes (map search, tidied up by the AI when needed)."""
    try:
        return places.search(q, _city(city))
    except ConnectionError as e:
        raise HTTPException(503, str(e)) from None