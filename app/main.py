from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.api import router
from app.config import get_settings
from app.limits import limiter
from app.preflight import enforce


@asynccontextmanager
async def lifespan(app: FastAPI):
    enforce(get_settings())  # production only: fails fast on unsafe settings
    yield


class BodyLimit:
    """Rejects request bodies over 64 KB (every real request here is a few hundred bytes). Counts bytes as they
    arrive, so chunked uploads without a Content-Length are caught too."""

    def __init__(self, app, limit: int = 64 * 1024):
        self.app, self.limit = app, limit

    async def _reject(self, send):
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": b'{"detail":"Request too large."}'})

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        length = dict(scope["headers"]).get(b"content-length", b"0")
        if not length.isdigit() or int(length) > self.limit:
            return await self._reject(send)
        seen = 0

        async def counted():
            nonlocal seen
            msg = await receive()
            if msg["type"] == "http.request":
                seen += len(msg.get("body", b""))
                if seen > self.limit:
                    raise OverflowError
            return msg

        try:
            await self.app(scope, counted, send)
        except OverflowError:
            await self._reject(send)  # shortcut: if the app already began a response this cannot replace it


# no /docs, /redoc or /openapi.json: they would hand anyone a complete map of the API
app = FastAPI(title="Trafixcast", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.add_middleware(BodyLimit)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in get_settings().cors_origins.split(",") if o.strip()],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],  # no cookies or tokens are used any more, so no credentials either
)
app.include_router(router)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


@app.get("/health")
def health():
    return {"ok": True}
