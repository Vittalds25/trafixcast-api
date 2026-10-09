import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.config import city_bbox_ok, get_city, get_engine_config
from app.engine.ev import validate_ev
from app.engine.fuel import validate_mileage


class Place(BaseModel):
    model_config = ConfigDict(extra="forbid")
    area_id: str | None = Field(default=None, max_length=40)
    lat: float | None = None
    lon: float | None = None

    @model_validator(mode="after")
    def _one_kind(self):
        pin = self.lat is not None or self.lon is not None
        if (self.area_id is None) == (not pin):
            raise ValueError("Give either area_id or both lat and lon.")
        if pin:
            if self.lat is None or self.lon is None or not (math.isfinite(self.lat) and math.isfinite(self.lon)):
                raise ValueError("Give both lat and lon.")
            if not (-90 <= self.lat <= 90 and -180 <= self.lon <= 180):
                raise ValueError("Location is not on Earth.")
        return self


class VehicleInput(BaseModel):
    """Everything except `vehicle` is optional to keep friction low."""
    model_config = ConfigDict(extra="forbid")
    vehicle: Literal["bike", "scooter", "car"]
    fuel_type: Literal["petrol", "diesel", "cng", "electric"] | None = None
    avg_speed_kmh: float | None = None
    mileage: float | None = None
    ev_range_km: float | None = None
    ev_real_range: bool = False  # True = ev_range_km is the user's own observed real-world range

    @field_validator("avg_speed_kmh")
    @classmethod
    def _speed(cls, v):
        if v is None:
            return v
        r = get_engine_config()["speed_input_kmh"]
        if not (math.isfinite(v) and r["min"] <= v <= r["max"]):
            raise ValueError(f"Speed must be between {r['min']} and {r['max']} km/h.")
        return v

    @model_validator(mode="after")
    def _fuel_rules(self):
        try:
            if self.mileage is not None:
                validate_mileage(self.vehicle, self.mileage)
            if self.ev_range_km is not None:
                validate_ev(self.vehicle, self.ev_range_km)
        except ValueError as e:
            raise ValueError(str(e)) from None
        return self


class EstimateRequest(VehicleInput):
    city: str = Field(default="bengaluru", max_length=20)
    origin: Place
    dest: Place
    fuel_price_override: float | None = Field(default=None, gt=0, le=1000)
    battery_pct: float | None = None
    round_trip: bool = False
    weekdays_only: bool = False  # next 5 Mon-Fri days instead of the next 7 calendar days
    window_start_hour: int = Field(ge=0, le=23)  # required: the departure window drives every slot
    window_end_hour: int = Field(ge=1, le=24)

    @model_validator(mode="after")
    def _city_pins(self):
        try:
            name = get_city(self.city)["name"]
        except KeyError:
            raise ValueError("We do not cover that city yet.") from None
        for p in (self.origin, self.dest):
            if p.lat is not None and not city_bbox_ok(self.city, p.lat, p.lon):
                raise ValueError(f"Location is outside {name}.")
        return self

    @model_validator(mode="after")
    def _battery_window(self):
        if self.battery_pct is not None and not 1 <= self.battery_pct <= 100:
            raise ValueError("Battery level must be between 1 and 100.")
        s, e = self.window_start_hour, self.window_end_hour
        if e <= s or e - s > get_engine_config()["estimate"]["max_window_hours"]:
            raise ValueError("Window must be 1 to 6 hours.")
        return self
import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.config import city_bbox_ok, get_city, get_engine_config
from app.engine.ev import validate_ev
from app.engine.fuel import validate_mileage


class Place(BaseModel):
    model_config = ConfigDict(extra="forbid")
    area_id: str | None = Field(default=None, max_length=40)
    lat: float | None = None
    lon: float | None = None

    @model_validator(mode="after")
    def _one_kind(self):
        pin = self.lat is not None or self.lon is not None
        if (self.area_id is None) == (not pin):
            raise ValueError("Give either area_id or both lat and lon.")
        if pin:
            if self.lat is None or self.lon is None or not (math.isfinite(self.lat) and math.isfinite(self.lon)):
                raise ValueError("Give both lat and lon.")
            if not (-90 <= self.lat <= 90 and -180 <= self.lon <= 180):
                raise ValueError("Location is not on Earth.")
        return self


class VehicleInput(BaseModel):
    """Everything except `vehicle` is optional to keep friction low."""
    model_config = ConfigDict(extra="forbid")
    vehicle: Literal["bike", "scooter", "car"]
    fuel_type: Literal["petrol", "diesel", "cng", "electric"] | None = None
    avg_speed_kmh: float | None = None
    mileage: float | None = None
    ev_range_km: float | None = None
    ev_real_range: bool = False  # True = ev_range_km is the user's own observed real-world range

    @field_validator("avg_speed_kmh")
    @classmethod
    def _speed(cls, v):
        if v is None:
            return v
        r = get_engine_config()["speed_input_kmh"]
        if not (math.isfinite(v) and r["min"] <= v <= r["max"]):
            raise ValueError(f"Speed must be between {r['min']} and {r['max']} km/h.")
        return v

    @model_validator(mode="after")
    def _fuel_rules(self):
        try:
            if self.mileage is not None:
                validate_mileage(self.vehicle, self.mileage)
            if self.ev_range_km is not None:
                validate_ev(self.vehicle, self.ev_range_km)
        except ValueError as e:
            raise ValueError(str(e)) from None
        return self


class EstimateRequest(VehicleInput):
    city: str = Field(default="bengaluru", max_length=20)
    origin: Place
    dest: Place
    fuel_price_override: float | None = Field(default=None, gt=0, le=1000)
    battery_pct: float | None = None
    round_trip: bool = False
    weekdays_only: bool = False  # next 5 Mon-Fri days instead of the next 7 calendar days
    window_start_hour: int = Field(ge=0, le=23)  # required: the departure window drives every slot
    window_end_hour: int = Field(ge=1, le=24)

    @model_validator(mode="after")
    def _city_pins(self):
        try:
            name = get_city(self.city)["name"]
        except KeyError:
            raise ValueError("We do not cover that city yet.") from None
        for p in (self.origin, self.dest):
            if p.lat is not None and not city_bbox_ok(self.city, p.lat, p.lon):
                raise ValueError(f"Location is outside {name}.")
        return self

    @model_validator(mode="after")
    def _battery_window(self):
        if self.battery_pct is not None and not 1 <= self.battery_pct <= 100:
            raise ValueError("Battery level must be between 1 and 100.")
        s, e = self.window_start_hour, self.window_end_hour
        if e <= s or e - s > get_engine_config()["estimate"]["max_window_hours"]:
            raise ValueError("Window must be 1 to 6 hours.")
        return self
import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.config import get_engine_config
from app.engine.ev import validate_ev
from app.engine.fuel import validate_mileage


class Place(BaseModel):
    model_config = ConfigDict(extra="forbid")
    area_id: str | None = Field(default=None, max_length=40)
    lat: float | None = None
    lon: float | None = None

    @model_validator(mode="after")
    def _one_kind(self):
        pin = self.lat is not None or self.lon is not None
        if (self.area_id is None) == (not pin):
            raise ValueError("Give either area_id or both lat and lon.")
        if pin:
            if self.lat is None or self.lon is None or not (math.isfinite(self.lat) and math.isfinite(self.lon)):
                raise ValueError("Give both lat and lon.")
            b = get_engine_config()["bengaluru_bbox"]
            if not (b["lat"][0] <= self.lat <= b["lat"][1] and b["lon"][0] <= self.lon <= b["lon"][1]):
                raise ValueError("Location is outside Bengaluru.")
        return self


class VehicleInput(BaseModel):
    """Everything except `vehicle` is optional to keep friction low."""
    model_config = ConfigDict(extra="forbid")
    vehicle: Literal["bike", "scooter", "car"]
    fuel_type: Literal["petrol", "diesel", "cng", "electric"] | None = None
    avg_speed_kmh: float | None = None
    mileage: float | None = None
    ev_range_km: float | None = None
    ev_real_range: bool = False  # True = ev_range_km is the user's own observed real-world range

    @field_validator("avg_speed_kmh")
    @classmethod
    def _speed(cls, v):
        if v is None:
            return v
        r = get_engine_config()["speed_input_kmh"]
        if not (math.isfinite(v) and r["min"] <= v <= r["max"]):
            raise ValueError(f"Speed must be between {r['min']} and {r['max']} km/h.")
        return v

    @model_validator(mode="after")
    def _fuel_rules(self):
        try:
            if self.mileage is not None:
                validate_mileage(self.vehicle, self.mileage)
            if self.ev_range_km is not None:
                validate_ev(self.vehicle, self.ev_range_km)
        except ValueError as e:
            raise ValueError(str(e)) from None
        return self


class EstimateRequest(VehicleInput):
    origin: Place
    dest: Place
    fuel_price_override: float | None = Field(default=None, gt=0, le=1000)
    battery_pct: float | None = None
    round_trip: bool = False
    weekdays_only: bool = False  # next 5 Mon-Fri days instead of the next 7 calendar days
    window_start_hour: int = Field(ge=0, le=23)  # required: the departure window drives every slot
    window_end_hour: int = Field(ge=1, le=24)

    @model_validator(mode="after")
    def _battery_window(self):
        if self.battery_pct is not None and not 1 <= self.battery_pct <= 100:
            raise ValueError("Battery level must be between 1 and 100.")
        s, e = self.window_start_hour, self.window_end_hour
        if e <= s or e - s > get_engine_config()["estimate"]["max_window_hours"]:
            raise ValueError("Window must be 1 to 6 hours.")
        return self
