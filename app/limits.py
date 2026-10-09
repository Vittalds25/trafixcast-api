from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.requests import Request

from app.config import get_settings


def client_ip(request: Request) -> str:
    """The visitor's address for rate limiting. Behind Google Cloud Run every request arrives from Google's own
    servers, so the real address is the LAST entry in X-Forwarded-For: Cloud Run adds it itself, which means a
    visitor cannot fake it by sending their own header. Only switch TRUST_FORWARDED_FOR on when the app is reachable
    through such a proxy and nowhere else, otherwise anyone could pick their own address."""
    if get_settings().trust_forwarded_for:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[-1].strip()
    return get_remote_address(request)


limiter = Limiter(key_func=client_ip, default_limits=["60/minute"])
