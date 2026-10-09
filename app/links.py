"""Turn a pasted Google Maps link (or plain 'lat, lon') into a pin. No Google API or key is used: the
coordinates are read from the link itself. Short links (maps.app.goo.gl) need one redirect hop, so the
server follows redirects by hand and only ever to Google's own hosts (no open fetch, no SSRF)."""
import re
from urllib.parse import parse_qs, unquote, unquote_plus, urljoin, urlparse

import httpx

from app.config import city_bbox_ok, get_city

SHORT_HOSTS = {"maps.app.goo.gl", "goo.gl"}
GOOGLE_HOSTS = {"www.google.com", "google.com", "maps.google.com", "www.google.co.in", "google.co.in"}
MAX_HOPS = 4
GEOCODER = "https://nominatim.openstreetmap.org/search"
NUM = r"(-?\d{1,3}\.\d+)"
# !3d..!4d.. is the actual pin; @lat,lon is only the map's centre, so it is the last resort
PATTERNS = [
    rf"!3d{NUM}!4d{NUM}",
    rf"[?&](?:q|ll|query|destination|daddr|center)=(?:loc:)?{NUM},\s*{NUM}",
    rf"@{NUM},{NUM}",
]
RAW = re.compile(rf"^\s*{NUM}\s*[, ]\s*{NUM}\s*$")


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


def query_text(url: str) -> str | None:
    """The place name and address in a link like google.com/maps?q=Raheja+Towers,+..., if there is one."""
    qs = parse_qs(urlparse(url).query)
    q = (qs.get("q") or qs.get("query") or [""])[0].strip()
    return q[:200] if len(q) >= 3 else None


def geocode(q: str, city: str) -> tuple[float, float] | None:
    """Looks a place name up on OpenStreetMap (Nominatim), only inside the chosen city. Tries the whole address, then
    shorter forms, because a full postal address often matches nothing. shortcut: Nominatim asks for about one
    request a second and light use; move to a paid geocoder if traffic grows."""
    c = get_city(city)
    city_name = c["name"]
    parts = [x.strip() for x in q.split(",") if x.strip() and not x.strip().replace(" ", "").isdigit()]
    tries = [q]
    if parts:
        tries.append(f"{parts[0]}, {city_name}")
        if len(parts) > 2:
            tries.append(f"{parts[0]}, {parts[1]}, {city_name}")
    b = c["bbox"]
    for t in tries:
        try:
            r = httpx.get(GEOCODER, timeout=5.0, headers={"User-Agent": "Trafixcast/1.0 (trafixcast@gmail.com)"}, params={
                "format": "jsonv2", "limit": 1, "countrycodes": "in", "bounded": 1, "q": t,
                "viewbox": f"{b['lon'][0]},{b['lat'][1]},{b['lon'][1]},{b['lat'][0]}"})
            r.raise_for_status()
            hit = r.json()
        except (httpx.HTTPError, ValueError):
            raise ConnectionError("The place lookup is busy right now. Try again in a moment, or tap the map.") from None
        if hit:
            return float(hit[0]["lat"]), float(hit[0]["lon"])
    return None


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


def resolve(link: str, city: str = "bengaluru") -> dict:
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
    try:
        city_name = get_city(city)["name"]
    except KeyError:
        raise LinkError("We do not cover that city yet.") from None
    found, name = coords_from_text(text), place_name(text)
    if not found and (q := query_text(text)):
        # links copied from the phone app carry only the place name and address, no coordinates
        found, name = geocode(q, city), q.split(",")[0].strip()[:120]
    if not found:
        raise LinkError("Could not find that place. Open the link in your browser, copy the long address from the "
                        "address bar and paste that, or tap the map instead.")
    lat, lon = found
    if not city_bbox_ok(city, lat, lon):
        raise LinkError(f"That location is outside {city_name}.")
    return {"lat": round(lat, 5), "lon": round(lon, 5), "name": name}
