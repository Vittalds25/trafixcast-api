"""Refuses to start in production with unsafe settings. Also runnable by hand:
    python -m app.preflight          (checks the settings in backend/.env as if APP_ENV=production)"""
import sys

from app.config import Settings, get_settings


def production_problems(s: Settings) -> list[str]:
    p = []
    if s.routing_provider != "tomtom" or not s.tomtom_api_key:
        p.append("ROUTING_PROVIDER must be tomtom with a TOMTOM_API_KEY: the mock provider shows made-up travel times.")
    if any(h in s.cors_origins for h in ("localhost", "127.0.0.1")):
        p.append("CORS_ORIGINS still points at localhost; set it to your real site address.")
    return p


def enforce(s: Settings) -> None:
    if s.app_env == "production" and (problems := production_problems(s)):
        raise RuntimeError("Unsafe production settings:\n - " + "\n - ".join(problems))


if __name__ == "__main__":
    found = production_problems(get_settings())
    print("Ready for production." if not found else "Not ready for production:\n - " + "\n - ".join(found))
    sys.exit(1 if found else 0)
