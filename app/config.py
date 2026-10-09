from functools import lru_cache
from pathlib import Path

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "development"  # set to production on the real server; enables the startup safety check
    routing_provider: str = "mock"
    tomtom_api_key: str = ""
    # Every exact pin is a fresh route lookup, so the traffic provider's free quota is the real limit on usage.
    # Past this many lookups a day, estimates fall back to a clearly-flagged rough figure instead of failing.
    daily_route_budget: int = 2000
    cors_origins: str = "http://localhost:5173"  # the website's address(es), comma-separated
    trust_forwarded_for: bool = False  # true only behind Cloud Run or a similar proxy; see app/limits.py


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def load_yaml(name: str):
    return yaml.safe_load((CONFIG_DIR / name).read_text(encoding="utf-8"))


def get_engine_config() -> dict:
    return load_yaml("engine.yaml")

def get_city(key: str) -> dict:
    """The city's settings from cities.yaml; raises KeyError for a city we do not serve."""
    return load_yaml("cities.yaml")[key]


def city_bbox_ok(city: str, lat: float, lon: float) -> bool:
    b = get_city(city)["bbox"]
    return b["lat"][0] <= lat <= b["lat"][1] and b["lon"][0] <= lon <= b["lon"][1]
