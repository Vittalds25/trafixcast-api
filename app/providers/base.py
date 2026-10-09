"""Provider interfaces. Engine depends on these only, so sources can be swapped (PRD section 8)."""
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol


@dataclass(frozen=True)
class Point:
    lat: float
    lon: float


@dataclass(frozen=True)
class RouteEstimate:
    distance_km: float
    traffic_minutes: float  # predicted for the given departure time
    free_flow_minutes: float
    degraded: bool = False  # True when this is a fallback -> lower confidence


class RoutingProvider(Protocol):
    def route(self, origin: Point, dest: Point, depart_at: datetime) -> RouteEstimate | None:
        """None means the provider failed; the caller falls back to a degraded estimate."""
        ...


class Forecast(Protocol):
    def at(self, when: datetime) -> tuple[float, float] | None:
        """(rain_mm_per_hour, temp_c) for that hour, or None if outside the forecast."""
        ...


class WeatherProvider(Protocol):
    def forecast(self, at: Point) -> Forecast | None: ...


class HolidayProvider(Protocol):
    def is_holiday(self, day: date) -> bool: ...


class FuelPriceProvider(Protocol):
    def price(self, fuel: str) -> tuple[float, str]:
        """(price_inr, as_of_date_iso)."""
        ...
