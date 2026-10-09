import math
from datetime import datetime

from app.providers.base import Point, RouteEstimate


class MockRouting:
    """Deterministic fake: straight-line distance x1.3, 30 km/h free flow, peak-hour slowdown."""

    def route(self, origin: Point, dest: Point, depart_at: datetime) -> RouteEstimate:
        dlat = math.radians(dest.lat - origin.lat)
        dlon = math.radians(dest.lon - origin.lon) * math.cos(math.radians(origin.lat))
        km = 6371 * math.hypot(dlat, dlon) * 1.3
        free = km / 30 * 60
        peak = depart_at.weekday() < 5 and depart_at.hour in (8, 9, 10, 17, 18, 19)
        return RouteEstimate(round(km, 2), round(free * (1.8 if peak else 1.1), 1), round(free, 1))


class MockForecast:
    def __init__(self, rain_mm: float = 0.0, temp_c: float = 28.0):
        self._v = (rain_mm, temp_c)

    def at(self, when):
        return self._v


class MockWeather:
    def __init__(self, rain_mm: float = 0.0, temp_c: float = 28.0):
        self._f = MockForecast(rain_mm, temp_c)

    def forecast(self, at):
        return self._f
