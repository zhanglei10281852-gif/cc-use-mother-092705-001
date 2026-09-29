from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, Field, model_validator

MAX_HORIZON = timedelta(days=7)


def _normalize_dt(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


UtcDateTime = Annotated[datetime, AfterValidator(_normalize_dt)]


class WindowIn(BaseModel):
    kind: str = Field(min_length=1, max_length=40)
    rule: Literal["avoid", "prefer"]
    starts_at: UtcDateTime
    ends_at: UtcDateTime
    note: str = Field(default="", max_length=300)

    @model_validator(mode="after")
    def check_order(self) -> "WindowIn":
        if not self.starts_at < self.ends_at:
            raise ValueError("窗口开始时间必须早于结束时间")
        return self


class ZoneCreate(BaseModel):
    code: str = Field(min_length=2, max_length=40, pattern=r"^[a-z0-9][a-z0-9._-]+$")
    name: str = Field(min_length=2, max_length=120)
    habitat_type: str = Field(min_length=1, max_length=60)
    description: str = Field(default="", max_length=500)
    protection_windows: list[WindowIn] = Field(default_factory=list, max_length=20)


class AvailabilityIn(BaseModel):
    starts_at: UtcDateTime
    ends_at: UtcDateTime
    note: str = Field(default="", max_length=300)

    @model_validator(mode="after")
    def check_order(self) -> "AvailabilityIn":
        if not self.starts_at < self.ends_at:
            raise ValueError("可用时段开始时间必须早于结束时间")
        return self


class RangerCreate(BaseModel):
    code: str = Field(min_length=2, max_length=40, pattern=r"^[a-z0-9][a-z0-9._-]+$")
    name: str = Field(min_length=2, max_length=120)
    skills: list[str] = Field(min_length=1, max_length=20)
    availability: list[AvailabilityIn] = Field(default_factory=list, max_length=50)


class ObservationCreate(BaseModel):
    zone_code: str = Field(min_length=2, max_length=40)
    kind: Literal["waterlogging", "visitor_peak", "other"]
    starts_at: UtcDateTime
    ends_at: UtcDateTime
    severity: Literal["low", "medium", "high"] = "medium"
    note: str = Field(default="", max_length=300)
    reported_by: str = Field(min_length=1, max_length=80)

    @model_validator(mode="after")
    def check_order(self) -> "ObservationCreate":
        if not self.starts_at < self.ends_at:
            raise ValueError("观察开始时间必须早于结束时间")
        return self


class RunRequestIn(BaseModel):
    request_key: str = Field(min_length=1, max_length=80)
    zone_code: str = Field(min_length=2, max_length=40)
    required_skill: str = Field(min_length=1, max_length=60)
    earliest_start: UtcDateTime
    latest_end: UtcDateTime
    duration_minutes: int = Field(ge=15, le=720)
    priority: int = Field(default=50, ge=0, le=100)
    note: str = Field(default="", max_length=300)

    @model_validator(mode="after")
    def check_window(self) -> "RunRequestIn":
        if not self.earliest_start < self.latest_end:
            raise ValueError("需求窗口开始时间必须早于结束时间")
        if self.latest_end - self.earliest_start > MAX_HORIZON:
            raise ValueError("需求窗口跨度不能超过 7 天")
        if self.latest_end - self.earliest_start < timedelta(minutes=self.duration_minutes):
            raise ValueError("巡护时长超过需求窗口跨度")
        return self


class RunCreate(BaseModel):
    batch_key: str = Field(min_length=6, max_length=120)
    requested_by: str = Field(min_length=1, max_length=80)
    requests: list[RunRequestIn] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def check_unique_keys(self) -> "RunCreate":
        keys = [item.request_key for item in self.requests]
        if len(keys) != len(set(keys)):
            raise ValueError("同一批次内 request_key 不能重复")
        return self


class DisruptionCreate(BaseModel):
    idempotency_key: str = Field(min_length=6, max_length=160)
    kind: Literal["heavy_rain", "zone_closed", "ranger_unavailable"]
    zone_code: str | None = Field(default=None, min_length=2, max_length=40)
    ranger_code: str | None = Field(default=None, min_length=2, max_length=40)
    starts_at: UtcDateTime
    ends_at: UtcDateTime
    reason: str = Field(min_length=2, max_length=500)
    actor: str = Field(min_length=1, max_length=80)

    @model_validator(mode="after")
    def check_scope(self) -> "DisruptionCreate":
        if not self.starts_at < self.ends_at:
            raise ValueError("扰动开始时间必须早于结束时间")
        if self.kind == "zone_closed" and not self.zone_code:
            raise ValueError("区域封闭扰动必须提供 zone_code")
        if self.kind == "ranger_unavailable" and not self.ranger_code:
            raise ValueError("人员退出扰动必须提供 ranger_code")
        return self


class AssignmentAction(BaseModel):
    actor: str = Field(min_length=1, max_length=80)
    reason: str = Field(default="", max_length=500)
