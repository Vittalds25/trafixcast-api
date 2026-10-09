"""Turn a pasted Google Maps link (or plain 'lat, lon') into a pin. No Google API or key is used: the
coordinates are read from the link itself. Short links (maps.app.goo.gl) need one redirect hop, so the
server follows redirects by hand and only ever to Google's own hosts (no open fetch, no SSRF)."""
import re
from urllib.parse import unquote, unquote_plus, urljoin, urlparse

import httpx

from app.config import get_engine_config

SHORT_HOSTS = {"maps.app.goo.gl", "goo.gl"}
GOOGLE_HOSTS = {"www.google.com", "google.com", "maps.google.com", "www.google.co.in", "google.co.in"}
MAX_HOPS = 4
NUM = r"(-?\d{1,3}\.\d+)"
# !3d..!4d.. is the actual pin; @lat,lon is only the map's centre, so it is the last resort
PATTERNS = [
    rf"!3d{NUM}!4d{NUM}",
    rf"[?&](?:q|ll|query|destination|daddr|center)=(?:loc:)?{NUM},\s*{NUM}",
    rf"@{NUM},{NUM}",
]
RAW = re.compile(rf"^\s*{NUM}\s*[, ]\s*{NUM}\s*$")


def in_bengaluru(lat: float, lon: float) -> bool:
    b = get_engine_config()["bengaluru_bbox"]
    return b["lat"][0] <= lat <= b["lat"][1] and b["lon"][0] <= lon <= b["lon"][1]


class LinkError(ValueError):
    """The text is not a usable location; the message is safe to show the user."""


def coords_from_text(text: str) -> tuple[float, float] | None:
    t = unquote(text)
    for p in PATTERNS:
        if m := re.search(p, t):
            return float(m[1]), float(m[2])
    if m := RAW.match(t):
        return float(m[1]), float(m[2])
    return None


def place_name(url: str) -> str | None:
    m = re.search(r"/maps/place/([^/@?]+)", url)
    return unquote_plus(m[1])[:120] if m else None


def _follow(url: str) -> str:
    for _ in range(MAX_HOPS):
        if coords_from_text(url):
            return url
        try:
            r = httpx.get(url, follow_redirects=False, timeout=5.0, headers={"User-Agent": "Mozilla/5.0"})
        except httpx.HTTPError:
            raise ConnectionError(
                "Could not open that short link. Open it in your browser, copy the full address from the address bar, "
                "and paste that instead."
            ) from None
        if not r.is_redirect:
            return url
        nxt = urljoin(url, r.headers.get("location", ""))
        p = urlparse(nxt)
        if p.scheme != "https" or p.hostname not in SHORT_HOSTS | GOOGLE_HOSTS:
            raise LinkError("That link does not lead to Google Maps.")
        url = nxt
    return url


def resolve(link: str) -> dict:
    """Returns {"lat", "lon", "name"}. Raises LinkError (bad input) or ConnectionError (network)."""
    text = link.strip()
    if text.lower().startswith("http"):
        p = urlparse(text)
        if p.scheme != "https" or p.hostname not in SHORT_HOSTS | GOOGLE_HOSTS:
            raise LinkError("Only Google Maps links are supported.")
        if p.hostname in SHORT_HOSTS:
            text = _follow(text)
    elif not RAW.match(text):
        raise LinkError("Paste a Google Maps link, or coordinates like 12.9352, 77.6245.")
    found = coords_from_text(text)
    if not found:
        raise LinkError("Could not find a location in that link. Tap the map instead.")
    lat, lon = found
    if not in_bengaluru(lat, lon):
        raise LinkError("That location is outside Bengaluru.")
    return {"lat": round(lat, 5), "lon": round(lon, 5), "name": place_name(text)}
