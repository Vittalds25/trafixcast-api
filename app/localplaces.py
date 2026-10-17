"""Instant place suggestions from a pre-built OpenStreetMap index: config/places/<city>.json (built by tools_osm_places.py).

Why: a live geocoder takes 1 to 5 s per new text and may not offer a suggestion until the text is complete. This index holds the named malls,
hospitals, colleges, offices and tech parks, stations, parks, hotels, temples, landmarks and neighbourhoods of each city, so typing "forum", "manipal" or
"embassy tech" shows the place at once. The live geocoder is still the fallback for anything not in the index (see places.search).
A missing file just means no local suggestions for that city."""
import json
import math
import re
import unicodedata
from functools import lru_cache

from app.config import CONFIG_DIR

PLACES_DIR = CONFIG_DIR / "places"
LABEL = {"area": "Neighbourhood", "station": "Station", "airport": "Airport", "mall": "Mall", "hospital": "Hospital", "college": "College",
         "office": "Office or tech park", "landmark": "Landmark", "park": "Park or stadium", "cinema": "Cinema or theatre",
         "market": "Market", "hotel": "Hotel", "worship": "Place of worship", "apartment": "Apartments or layout",
         "restaurant": "Restaurant", "cafe": "Cafe or bakery", "workplace": "Office or workplace", "supermarket": "Supermarket", "school": "School"}
# a nudge so that, among equal text matches, the places people travel to most come first
WEIGHT = {"airport": 9, "station": 9, "mall": 8, "office": 8, "workplace": 8, "hospital": 7, "college": 7, "area": 6, "apartment": 6, "landmark": 6,
          "restaurant": 6, "park": 5, "cafe": 5, "supermarket": 5, "cinema": 4, "market": 4, "school": 4, "hotel": 3, "worship": 2}
MIN_CHARS = 2
_SPLIT = re.compile(r"[^a-z0-9]+")


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return _SPLIT.sub(" ", s).strip()


def _rows(name: str) -> list:
    try:
        return json.loads((PLACES_DIR / name).read_text(encoding="utf-8"))["items"]
    except (OSError, ValueError, KeyError):
        return []


@lru_cache(maxsize=8)
def _index(city: str) -> list:
    # <city>.json is rebuilt by tools_osm_places.py; <city>.custom.json is kept by hand for places OpenStreetMap does not have yet
    rows = _rows(f"{city}.json") + _rows(f"{city}.custom.json")
    out = []
    for name, lat, lon, kind in rows:
        n = norm(name)
        if n:
            out.append((n, tuple(n.split()), name, lat, lon, kind))
    return out


def _km(a_lat, a_lon, b_lat, b_lon) -> float:
    return math.hypot((a_lat - b_lat) * 111.0, (a_lon - b_lon) * 111.0 * math.cos(math.radians(a_lat)))


def _score(q: str, toks: list, n: str, words: tuple, kind: str) -> float:
    """0 = no match. Every typed word must start a word of the name (a longer typed word may also sit inside a name word)."""
    pos = 0
    for t in toks:
        hit = next((i for i, w in enumerate(words) if w.startswith(t)), None)
        if hit is None and len(t) >= 4:
            hit = next((i for i, w in enumerate(words) if t in w), None)
        if hit is None:
            return 0.0
        pos += hit
    base = 100.0 if n == q else 80.0 if n.startswith(q) else 55.0 - min(pos, 10)
    return base + WEIGHT.get(kind, 0) - min(len(n), 60) / 30.0


def search(q: str, city: str, limit: int = 6, area_list=None) -> list[dict]:
    """[{name, detail, lat, lon}], best first. detail = what it is and the nearest known area (when area_list = [{name, lat, lon}] is given)."""
    nq = norm(q)
    if len(nq) < MIN_CHARS:
        return []
    toks = nq.split()
    scored = []
    for n, words, name, lat, lon, kind in _index(city):
        s = _score(nq, toks, n, words, kind)
        if s:
            scored.append((s, name, lat, lon, kind))
    scored.sort(key=lambda r: -r[0])
    out = []
    for s, name, lat, lon, kind in scored:
        if any(o["name"].lower() == name.lower() and _km(o["lat"], o["lon"], lat, lon) < 0.3 for o in out):
            continue
        near = ""
        if area_list:
            a = min(area_list, key=lambda x: _km(lat, lon, x["lat"], x["lon"]))
            near = f", near {a['name']}"
        out.append({"name": name[:80], "detail": f"{LABEL.get(kind, 'Place')}{near}"[:90], "lat": round(lat, 5), "lon": round(lon, 5)})
        if len(out) >= limit:
            break
    return out
