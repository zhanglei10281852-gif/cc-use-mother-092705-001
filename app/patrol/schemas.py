from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

CODE_PATTERN = r"^[a-z0-9][a-z0-9-]{1,39}$"


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class ZoneCreate(BaseModel):
    code: str = Field(pattern=CODE_PATTERN)
    name: str = Field(min_length=2, max_length=80)
    habitat_type: str = Field(min_length=1, max_length=40)
    route_group: str = Field(min_length=1, max_length=40)
    visit_minutes: int = Field(default=60, ge=10, le=480)


class ProtectionWindowCreate(BaseModel):
    zone_code: str = Field(min_length=2, max_length=40)
    kind: Literal["bird_breeding", "waterlogging", "visitor_peak"]
    starts_at: datetime
    ends_at: datetime
    required_skill: str = Field(default="", max_length=40)
    min_interval_minutes: int = Field(default=0, ge=0, le=1440)
    priority_boost: int = Field(default=0, ge=0, le=50)
    note: str = Field(default="", max_length=300)

    @field_validator("starts_at", "ends_at")
    @classmethod
    def normalize_moment(cls, value: datetime) -> datetime:
        return _as_utc(value)

    @model_validator(mode="after")
    def check_order(self) -> "ProtectionWindowCreate":
        if self.ends_at <= self.starts_at:
            raise ValueError("保护窗口结束时间必须晚于开始时间")
        return self


class RangerCreate(BaseModel):
    code: str = Field(pattern=CODE_PATTERN)
    name: str = Field(min_length=2, max_length=50)
    skills: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("skills")
    @classmethod
    def normalize_skills(cls, value: list[str]) -> list[str]:
        cleaned = sorted({item.strip() for item in value if item.strip()})
        for skill in cleaned:
            if len(skill) > 40:
                raise ValueError("技能名称过长")
        return cleaned


class AvailabilityCreate(BaseModel):
    starts_at: datetime
    ends_at: datetime

    @field_validator("starts_at", "ends_at")
    @classmethod
    def normalize_moment(cls, value: datetime) -> datetime:
        return _as_utc(value)

    @model_validator(mode="after")
    def check_order(self) -> "AvailabilityCreate":
        if self.ends_at <= self.starts_at:
            raise ValueError("可用时段结束时间必须晚于开始时间")
        return self


class RiskObservationCreate(BaseModel):
    zone_code: str = Field(min_length=2, max_length=40)
    kind: str = Field(min_length=1, max_length=40)
    severity: int = Field(ge=1, le=5)
    note: str = Field(default="", max_length=500)
    observed_at: datetime | None = None

    @field_validator("observed_at")
    @classmethod
    def normalize_moment(cls, value: datetime | None) -> datetime | None:
        return _as_utc(value) if value is not None else None


class PatrolRequestItem(BaseModel):
    zone_code: str = Field(min_length=2, max_length=40)
    window_start: datetime
    window_end: datetime
    required_skill: str = Field(default="", max_length=40)
    purpose: str = Field(default="", max_length=200)

    @field_validator("window_start", "window_end")
    @classmethod
    def normalize_moment(cls, value: datetime) -> datetime:
        return _as_utc(value)

    @model_validator(mode="after")
    def check_order(self) -> "PatrolRequestItem":
        if self.window_end <= self.window_start:
            raise ValueError("巡护窗口结束时间必须晚于开始时间")
        return self


class ScheduleGenerate(BaseModel):
    requested_by: str = Field(min_length=1, max_length=80)
    batch_key: str = Field(min_length=6, max_length=120)
    title: str = Field(default="", max_length=120)
    requests: list[PatrolRequestItem] = Field(min_length=1, max_length=100)


class DisruptionCreate(BaseModel):
    actor: str = Field(min_length=1, max_length=80)
    disruption_key: str = Field(min_length=6, max_length=120)
    kind: Literal["heavy_rain", "zone_closed", "ranger_unavailable"]
    zone_code: str | None = Field(default=None, max_length=40)
    ranger_code: str | None = Field(default=None, max_length=40)
    starts_at: datetime
    ends_at: datetime
    reason: str = Field(min_length=2, max_length=500)

    @field_validator("starts_at", "ends_at")
    @classmethod
    def normalize_moment(cls, value: datetime) -> datetime:
        return _as_utc(value)

    @model_validator(mode="after")
    def check_target(self) -> "DisruptionCreate":
        if self.ends_at <= self.starts_at:
            raise ValueError("影响时段结束时间必须晚于开始时间")
        if self.kind == "zone_closed" and not self.zone_code:
            raise ValueError("区域封闭必须提供 zone_code")
        if self.kind == "ranger_unavailable" and not self.ranger_code:
            raise ValueError("人员临时退出必须提供 ranger_code")
        return self


class AssignmentAction(BaseModel):
    actor: str = Field(min_length=1, max_length=80)
    expected_version: int | None = Field(default=None, ge=1)


class AssignmentCancel(BaseModel):
    actor: str = Field(min_length=1, max_length=80)
    reason: str = Field(min_length=2, max_length=500)
    expected_version: int | None = Field(default=None, ge=1)


class AssignmentReassign(BaseModel):
    actor: str = Field(min_length=1, max_length=80)
    reason: str = Field(min_length=2, max_length=500)
    ranger_code: str = Field(min_length=2, max_length=40)
    start_at: datetime
    expected_version: int | None = Field(default=None, ge=1)

    @field_validator("start_at")
    @classmethod
    def normalize_moment(cls, value: datetime) -> datetime:
        return _as_utc(value)
