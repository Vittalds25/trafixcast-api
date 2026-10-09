from dataclasses import dataclass

from app.config import get_settings
from app.providers.curated import OpenMeteoWeather, YamlFuelPrices, YamlHolidays
from app.providers.mock import MockRouting
from app.providers.tomtom import TomTomRouting


@dataclass
class Providers:
    routing: object
    weather: object
    holidays: object
    fuel: object


def get_providers() -> Providers:
    s = get_settings()
    routing = TomTomRouting(s.tomtom_api_key) if s.routing_provider == "tomtom" else MockRouting()
    return Providers(routing, OpenMeteoWeather(), YamlHolidays(), YamlFuelPrices())
