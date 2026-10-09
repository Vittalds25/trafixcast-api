import time

import httpx

from app.providers.base import RouteEstimate

URL = "https://api.tomtom.com/routing/1/calculateRoute/{o}:{d}/json"


class TomTomRouting:
    """TomTom Routing API with departAt (predictive traffic). Key is server-side only."""

    def __init__(self, api_key: str):
        if not api_key:
            raise ValueError("TOMTOM_API_KEY is not set")
        self._key = api_key

    def route(self, origin, dest, depart_at):
        params = {
            "key": self._key,
            "departAt": depart_at.strftime("%Y-%m-%dT%H:%M:%S"),
            "traffic": "true",
            "computeTravelTimeFor": "all",
        }
        url = URL.format(o=f"{origin.lat},{origin.lon}", d=f"{dest.lat},{dest.lon}")
        try:
            for attempt in range(4):
                r = httpx.get(url, params=params, timeout=5.0)
                if r.status_code not in (429, 500, 502, 503, 504):
                    break
                time.sleep(0.4 * 2**attempt)  # rate limit / transient error: back off and retry
            r.raise_for_status()
            s = r.json()["routes"][0]["summary"]
        except (httpx.HTTPError, KeyError, IndexError, ValueError):
            return None  # caller falls back to a degraded estimate
        return RouteEstimate(
            round(s["lengthInMeters"] / 1000, 2),
            round(s["travelTimeInSeconds"] / 60, 1),
            round(s["noTrafficTravelTimeInSeconds"] / 60, 1),
        )
