"""File-backed providers (holidays, fuel prices) and Open-Meteo weather."""
from datetime import date, datetime

import httpx

from app.config import get_city, load_yaml
from app.providers.base import Point


class YamlHolidays:
    def is_holiday(self, day: date, city: str = "bengaluru") -> bool:
        return day.isoformat() in {str(d) for d in load_yaml(get_city(city)["holidays"])}


class YamlFuelPrices:
    def price(self, fuel: str, city: str = "bengaluru") -> tuple[float, str]:
        d = load_yaml(get_city(city)["fuel"])
        return float(d[fuel]), str(d["as_of"])


class _HourlyForecast:
    def __init__(self, hours: dict[str, tuple[float, float]]):
        self._h = hours

    def at(self, when: datetime):
        return self._h.get(when.strftime("%Y-%m-%dT%H:00"))


class OpenMeteoWeather:
    URL = "https://api.open-meteo.com/v1/forecast"

    def forecast(self, at: Point):
        params = {
            "latitude": at.lat, "longitude": at.lon, "forecast_days": 7,
            "hourly": "precipitation,temperature_2m", "timezone": "Asia/Kolkata",
        }
        try:
            r = httpx.get(self.URL, params=params, timeout=5.0)
            r.raise_for_status()
            h = r.json()["hourly"]
            return _HourlyForecast(
                {t: (p or 0.0, c or 0.0) for t, p, c in
                 zip(h["time"], h["precipitation"], h["temperature_2m"], strict=True)}
            )
        except (httpx.HTTPError, KeyError, ValueError):
            return None  # estimate proceeds with lower confidence
