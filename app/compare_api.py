from fastapi import APIRouter, Depends, Request

from app import compare
from app.limits import limiter
from app.providers import get_providers

router = APIRouter(prefix="/v1")


@router.post("/compare")
@limiter.limit("6/minute;60/hour")  # does the work of an estimate and a metro plan
def compare_trip(request: Request, body: compare.CompareRequest, providers=Depends(get_providers)):
    return compare.compare(body, providers)
