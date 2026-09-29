from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.clock import Clock, SystemClock, from_storage, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.database import get_connection, transaction
from app.patrol.repository import PatrolRepository

SCHEMA = """
CREATE TABLE IF NOT EXISTS patrol_zones (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    habitat_type TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS patrol_zone_windows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    zone_id INTEGER NOT NULL REFERENCES patrol_zones(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    rule TEXT NOT NULL CHECK(rule IN ('avoid','prefer')),
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_patrol_windows_zone ON patrol_zone_windows(zone_id);
CREATE TABLE IF NOT EXISTS patrol_rangers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    skills_json TEXT NOT NULL DEFAULT '[]',
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS patrol_ranger_availability (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ranger_id INTEGER NOT NULL REFERENCES patrol_rangers(id) ON DELETE CASCADE,
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_patrol_availability_ranger ON patrol_ranger_availability(ranger_id);
CREATE TABLE IF NOT EXISTS patrol_risk_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    zone_id INTEGER NOT NULL REFERENCES patrol_zones(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK(kind IN ('waterlogging','visitor_peak','other')),
    effect TEXT NOT NULL CHECK(effect IN ('avoid','prefer')),
    severity TEXT NOT NULL DEFAULT 'medium' CHECK(severity IN ('low','medium','high')),
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    reported_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(zone_id, kind, starts_at, ends_at)
);
CREATE TABLE IF NOT EXISTS patrol_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_key TEXT NOT NULL UNIQUE,
    request_digest TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','closed')),
    request_count INTEGER NOT NULL,
    scheduled_count INTEGER NOT NULL DEFAULT 0,
    unscheduled_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS patrol_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES patrol_runs(id) ON DELETE CASCADE,
    request_key TEXT NOT NULL,
    zone_id INTEGER NOT NULL REFERENCES patrol_zones(id),
    required_skill TEXT NOT NULL,
    earliest_start TEXT NOT NULL,
    latest_end TEXT NOT NULL,
    duration_minutes INTEGER NOT NULL CHECK(duration_minutes BETWEEN 15 AND 720),
    priority INTEGER NOT NULL DEFAULT 50 CHECK(priority BETWEEN 0 AND 100),
    note TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','scheduled','unscheduled')),
    UNIQUE(run_id, request_key)
);
CREATE INDEX IF NOT EXISTS idx_patrol_requests_run ON patrol_requests(run_id);
CREATE TABLE IF NOT EXISTS patrol_assignments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES patrol_runs(id) ON DELETE CASCADE,
    request_id INTEGER NOT NULL REFERENCES patrol_requests(id) ON DELETE CASCADE,
    zone_id INTEGER NOT NULL REFERENCES patrol_zones(id),
    ranger_id INTEGER REFERENCES patrol_rangers(id),
    planned_start TEXT,
    planned_end TEXT,
    status TEXT NOT NULL DEFAULT 'planned' CHECK(status IN ('planned','in_progress','completed','cancelled','rescheduled','unscheduled')),
    rationale_json TEXT NOT NULL DEFAULT '{}',
    replaces_id INTEGER REFERENCES patrol_assignments(id),
    version INTEGER NOT NULL DEFAULT 1,
    started_at TEXT,
    finished_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_patrol_assignments_run ON patrol_assignments(run_id);
CREATE INDEX IF NOT EXISTS idx_patrol_assignments_zone ON patrol_assignments(zone_id, planned_start);
CREATE INDEX IF NOT EXISTS idx_patrol_assignments_ranger ON patrol_assignments(ranger_id, planned_start);
CREATE INDEX IF NOT EXISTS idx_patrol_assignments_request ON patrol_assignments(request_id);
CREATE TABLE IF NOT EXISTS patrol_disruptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK(kind IN ('heavy_rain','zone_closed','ranger_unavailable')),
    zone_id INTEGER REFERENCES patrol_zones(id),
    ranger_id INTEGER REFERENCES patrol_rangers(id),
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','resolved')),
    report_json TEXT NOT NULL DEFAULT '{}',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS patrol_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER REFERENCES patrol_runs(id) ON DELETE CASCADE,
    assignment_id INTEGER REFERENCES patrol_assignments(id) ON DELETE CASCADE,
    disruption_id INTEGER REFERENCES patrol_disruptions(id) ON DELETE CASCADE,
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    before_json TEXT NOT NULL DEFAULT '{}',
    after_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_patrol_events_assignment ON patrol_events(assignment_id, id);
CREATE INDEX IF NOT EXISTS idx_patrol_events_run ON patrol_events(run_id, id);
"""

SLOT_STEP = timedelta(minutes=15)
MAX_REJECTED_SAMPLES = 5

OBSERVATION_EFFECTS = {"waterlogging": "avoid", "visitor_peak": "prefer", "other": "avoid"}
OBSERVATION_LABELS = {"waterlogging": "滩涂积水", "visitor_peak": "游客密集", "other": "其他风险"}
DISRUPTION_LABELS = {"heavy_rain": "暴雨", "zone_closed": "区域封闭", "ranger_unavailable": "人员临时退出"}
WINDOW_RULE_LABELS = {"avoid": "回避", "prefer": "重点覆盖"}
ASSIGNMENT_STATUS_LABELS = {
    "planned": "待执行",
    "in_progress": "进行中",
    "completed": "已完成",
    "cancelled": "已取消",
    "rescheduled": "已改派",
    "unscheduled": "未排定",
}


def ensure_schema() -> None:
    get_connection().executescript(SCHEMA)


def _digest(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def _overlaps(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    return a_start < b_end and b_start < a_end


@dataclass(frozen=True)
class ZoneWindow:
    kind: str
    rule: str
    start: datetime
    end: datetime
    note: str


@dataclass(frozen=True)
class RiskObservation:
    kind: str
    effect: str
    start: datetime
    end: datetime
    note: str


@dataclass(frozen=True)
class RangerInfo:
    id: int
    code: str
    name: str
    skills: frozenset[str]
    availability: tuple[tuple[datetime, datetime], ...]


@dataclass(frozen=True)
class Blocker:
    assignment_id: int
    zone_id: int
    ranger_id: int | None
    start: datetime
    end: datetime


@dataclass(frozen=True)
class DisruptionInfo:
    id: int
    kind: str
    zone_id: int | None
    ranger_id: int | None
    start: datetime
    end: datetime
    reason: str


@dataclass
class PlanningContext:
    windows: dict[int, list[ZoneWindow]] = field(default_factory=dict)
    observations: dict[int, list[RiskObservation]] = field(default_factory=dict)
    rangers: list[RangerInfo] = field(default_factory=list)
    blockers: list[Blocker] = field(default_factory=list)
    disruptions: list[DisruptionInfo] = field(default_factory=list)


class PatrolService:
    """湿地巡护排班：生境与人员登记、可解释排班、扰动改派与变更历史。"""

    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        self.clock = clock or SystemClock()
        ensure_schema()

    # ------------------------------------------------------------------
    # 登记：生境、保护窗口、人员、可用时段、风险观察
    # ------------------------------------------------------------------
    def create_zone(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = PatrolRepository(connection)
            if repository.zone_by_code(payload["code"]):
                raise ConflictError("生境编码已存在")
            zone = repository.create_zone(
                code=payload["code"], name=payload["name"], habitat_type=payload["habitat_type"],
                description=payload.get("description", ""), now=now,
            )
            for window in payload.get("protection_windows", []):
                repository.add_window(
                    zone_id=zone["id"], kind=window["kind"], rule=window["rule"],
                    starts_at=to_storage(window["starts_at"]), ends_at=to_storage(window["ends_at"]),
                    note=window.get("note", ""), now=now,
                )
            return self._zone_payload(repository, zone["id"])

    def list_zones(self) -> list[dict[str, Any]]:
        repository = PatrolRepository(self.connection)
        return [self._zone_payload(repository, zone["id"]) for zone in repository.list_zones()]

    def get_zone(self, zone_id: int) -> dict[str, Any]:
        repository = PatrolRepository(self.connection)
        if repository.zone_by_id(zone_id) is None:
            raise NotFoundError("巡护生境不存在")
        return self._zone_payload(repository, zone_id)

    def add_window(self, zone_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = PatrolRepository(connection)
            if repository.zone_by_id(zone_id) is None:
                raise NotFoundError("巡护生境不存在")
            return repository.add_window(
                zone_id=zone_id, kind=payload["kind"], rule=payload["rule"],
                starts_at=to_storage(payload["starts_at"]), ends_at=to_storage(payload["ends_at"]),
                note=payload.get("note", ""), now=now,
            )

    def create_ranger(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        skills = sorted({skill.strip() for skill in payload["skills"] if skill.strip()})
        if not skills:
            raise ValidationError("巡护人员至少需要一项技能")
        with transaction(immediate=True) as connection:
            repository = PatrolRepository(connection)
            if repository.ranger_by_code(payload["code"]):
                raise ConflictError("巡护人员编码已存在")
            ranger = repository.create_ranger(code=payload["code"], name=payload["name"], skills=skills, now=now)
            for window in payload.get("availability", []):
                repository.add_availability(
                    ranger_id=ranger["id"], starts_at=to_storage(window["starts_at"]),
                    ends_at=to_storage(window["ends_at"]), note=window.get("note", ""), now=now,
                )
            return self._ranger_payload(repository, ranger["id"])

    def list_rangers(self) -> list[dict[str, Any]]:
        repository = PatrolRepository(self.connection)
        return [self._ranger_payload(repository, ranger["id"]) for ranger in repository.list_rangers()]

    def add_availability(self, ranger_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = PatrolRepository(connection)
            if repository.ranger_by_id(ranger_id) is None:
                raise NotFoundError("巡护人员不存在")
            return repository.add_availability(
                ranger_id=ranger_id, starts_at=to_storage(payload["starts_at"]),
                ends_at=to_storage(payload["ends_at"]), note=payload.get("note", ""), now=now,
            )

    def create_observation(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        starts_at = to_storage(payload["starts_at"])
        ends_at = to_storage(payload["ends_at"])
        with transaction(immediate=True) as connection:
            repository = PatrolRepository(connection)
            zone = repository.zone_by_code(payload["zone_code"])
            if zone is None:
                raise NotFoundError(f"巡护生境不存在：{payload['zone_code']}")
            existing = repository.observation_by_natural_key(zone["id"], payload["kind"], starts_at, ends_at)
            if existing is not None:
                result = dict(existing)
                result["replayed"] = True
                return result
            observation = repository.create_observation(
                zone_id=zone["id"], kind=payload["kind"], effect=OBSERVATION_EFFECTS[payload["kind"]],
                severity=payload.get("severity", "medium"), starts_at=starts_at, ends_at=ends_at,
                note=payload.get("note", ""), reported_by=payload["reported_by"], now=now,
            )
            observation["replayed"] = False
            return observation

    def list_observations(self, zone_code: str | None = None) -> list[dict[str, Any]]:
        repository = PatrolRepository(self.connection)
        zone_id = None
        if zone_code is not None:
            zone = repository.zone_by_code(zone_code)
            if zone is None:
                raise NotFoundError(f"巡护生境不存在：{zone_code}")
            zone_id = zone["id"]
        return repository.list_observations(zone_id)

    # ------------------------------------------------------------------
    # 排班批次
    # ------------------------------------------------------------------
    def submit_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        canonical = [
            {
                "request_key": item["request_key"],
                "zone_code": item["zone_code"],
                "required_skill": item["required_skill"],
                "earliest_start": to_storage(item["earliest_start"]),
                "latest_end": to_storage(item["latest_end"]),
                "duration_minutes": item["duration_minutes"],
                "priority": item["priority"],
                "note": item.get("note", ""),
            }
            for item in payload["requests"]
        ]
        request_digest = _digest(sorted(canonical, key=lambda item: item["request_key"]))
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = PatrolRepository(connection)
            existing = repository.run_by_batch_key(payload["batch_key"])
            if existing is not None:
                if existing["request_digest"] != request_digest:
                    raise ConflictError("同一批次键对应了不同的巡护要求")
                return self._run_detail(repository, existing["id"], replayed=True)
            zones: dict[str, sqlite3.Row] = {}
            for item in canonical:
                if item["zone_code"] in zones:
                    continue
                zone = repository.zone_by_code(item["zone_code"])
                if zone is None or not zone["active"]:
                    raise NotFoundError(f"巡护生境不存在或已停用：{item['zone_code']}")
                zones[item["zone_code"]] = zone
            run = repository.create_run(
                batch_key=payload["batch_key"], request_digest=request_digest,
                requested_by=payload["requested_by"], request_count=len(canonical), now=now,
            )
            context = self._load_context(repository)
            ordered = sorted(canonical, key=lambda item: (-item["priority"], item["earliest_start"], item["request_key"]))
            scheduled = 0
            unscheduled = 0
            for item in ordered:
                zone = zones[item["zone_code"]]
                request = repository.create_request(
                    run_id=run["id"], request_key=item["request_key"], zone_id=zone["id"],
                    required_skill=item["required_skill"], earliest_start=item["earliest_start"],
                    latest_end=item["latest_end"], duration_minutes=item["duration_minutes"],
                    priority=item["priority"], note=item["note"],
                )
                choice, rationale = self._plan_request(context, self._request_spec(item, zone))
                if choice is not None:
                    assignment = repository.create_assignment(
                        run_id=run["id"], request_id=request["id"], zone_id=zone["id"],
                        ranger_id=choice["ranger"].id, planned_start=to_storage(choice["start"]),
                        planned_end=to_storage(choice["end"]), status="planned",
                        rationale=rationale, replaces_id=None, now=now,
                    )
                    context.blockers.append(Blocker(assignment["id"], zone["id"], choice["ranger"].id, choice["start"], choice["end"]))
                    repository.update_request_status(request["id"], "scheduled")
                    repository.add_event(
                        run_id=run["id"], assignment_id=assignment["id"], disruption_id=None,
                        action="created", actor=payload["requested_by"], reason="批量排班生成",
                        before={}, after=assignment, now=now,
                    )
                    scheduled += 1
                else:
                    assignment = repository.create_assignment(
                        run_id=run["id"], request_id=request["id"], zone_id=zone["id"],
                        ranger_id=None, planned_start=None, planned_end=None, status="unscheduled",
                        rationale=rationale, replaces_id=None, now=now,
                    )
                    repository.update_request_status(request["id"], "unscheduled")
                    repository.add_event(
                        run_id=run["id"], assignment_id=assignment["id"], disruption_id=None,
                        action="unscheduled", actor=payload["requested_by"], reason="需求窗口内没有可行时段",
                        before={}, after=assignment, now=now,
                    )
                    unscheduled += 1
            repository.set_run_counts(run["id"], scheduled, unscheduled)
            repository.add_event(
                run_id=run["id"], assignment_id=None, disruption_id=None, action="run_created",
                actor=payload["requested_by"], reason=f"批次提交 {len(canonical)} 项巡护要求",
                before={}, after={"batch_key": payload["batch_key"], "request_digest": request_digest}, now=now,
            )
            return self._run_detail(repository, run["id"], replayed=False)

    def list_runs(self, limit: int = 100) -> list[dict[str, Any]]:
        return PatrolRepository(self.connection).list_runs(max(1, min(limit, 500)))

    def get_run(self, run_id: int) -> dict[str, Any]:
        repository = PatrolRepository(self.connection)
        if repository.run_by_id(run_id) is None:
            raise NotFoundError("排班批次不存在")
        return self._run_detail(repository, run_id, replayed=False)

    def run_events(self, run_id: int) -> list[dict[str, Any]]:
        repository = PatrolRepository(self.connection)
        if repository.run_by_id(run_id) is None:
            raise NotFoundError("排班批次不存在")
        return [self._event_payload(row) for row in repository.events_for_run(run_id)]

    # ------------------------------------------------------------------
    # 分派查询与生命周期
    # ------------------------------------------------------------------
    def list_assignments(self, *, status: str | None = None, zone_code: str | None = None, ranger_code: str | None = None, run_id: int | None = None, limit: int = 200) -> list[dict[str, Any]]:
        repository = PatrolRepository(self.connection)
        zone_id = None
        if zone_code is not None:
            zone = repository.zone_by_code(zone_code)
            if zone is None:
                raise NotFoundError(f"巡护生境不存在：{zone_code}")
            zone_id = zone["id"]
        ranger_id = None
        if ranger_code is not None:
            ranger = repository.ranger_by_code(ranger_code)
            if ranger is None:
                raise NotFoundError(f"巡护人员不存在：{ranger_code}")
            ranger_id = ranger["id"]
        rows = repository.list_assignments(status=status, zone_id=zone_id, ranger_id=ranger_id, run_id=run_id, limit=max(1, min(limit, 500)))
        return [self._assignment_payload(row) for row in rows]

    def get_assignment(self, assignment_id: int) -> dict[str, Any]:
        repository = PatrolRepository(self.connection)
        row = repository.assignment_by_id(assignment_id)
        if row is None:
            raise NotFoundError("巡护分派不存在")
        result = self._assignment_payload(dict(row))
        successor = repository.successor_of(assignment_id)
        result["replaced_by"] = successor["id"] if successor is not None else None
        result["events"] = [self._event_payload(event) for event in repository.events_for_assignment(assignment_id)]
        return result

    def start_assignment(self, assignment_id: int, actor: str) -> dict[str, Any]:
        return self._transition(assignment_id, actor, "started", "planned", "in_progress", "巡护开始")

    def complete_assignment(self, assignment_id: int, actor: str) -> dict[str, Any]:
        return self._transition(assignment_id, actor, "completed", "in_progress", "completed", "巡护完成")

    def cancel_assignment(self, assignment_id: int, actor: str, reason: str) -> dict[str, Any]:
        if not reason.strip():
            raise ValidationError("取消巡护必须填写原因")
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = PatrolRepository(connection)
            row = repository.assignment_by_id(assignment_id)
            if row is None:
                raise NotFoundError("巡护分派不存在")
            if row["status"] in {"in_progress", "completed"}:
                raise ConflictError("巡护已开始，不能被取消或改写")
            if row["status"] != "planned":
                raise ConflictError(f"当前状态为 {ASSIGNMENT_STATUS_LABELS.get(row['status'], row['status'])}，不允许取消")
            before = dict(row)
            cursor = connection.execute(
                "UPDATE patrol_assignments SET status='cancelled',finished_at=?,updated_at=?,version=version+1 WHERE id=? AND status='planned'",
                (now, now, assignment_id),
            )
            if cursor.rowcount != 1:
                raise ConflictError("巡护分派状态已变化，请刷新后重试")
            after = dict(repository.assignment_by_id(assignment_id))
            repository.add_event(
                run_id=after["run_id"], assignment_id=assignment_id, disruption_id=None,
                action="cancelled", actor=actor, reason=reason, before=before, after=after, now=now,
            )
            return self._assignment_payload(after)

    # ------------------------------------------------------------------
    # 扰动与改派
    # ------------------------------------------------------------------
    def create_disruption(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        starts_at = to_storage(payload["starts_at"])
        ends_at = to_storage(payload["ends_at"])
        with transaction(immediate=True) as connection:
            repository = PatrolRepository(connection)
            existing = repository.disruption_by_key(payload["idempotency_key"])
            if existing is not None:
                return self._disruption_payload(dict(existing), replayed=True)
            zone_id = None
            if payload.get("zone_code"):
                zone = repository.zone_by_code(payload["zone_code"])
                if zone is None:
                    raise NotFoundError(f"巡护生境不存在：{payload['zone_code']}")
                zone_id = zone["id"]
            ranger_id = None
            if payload.get("ranger_code"):
                ranger = repository.ranger_by_code(payload["ranger_code"])
                if ranger is None:
                    raise NotFoundError(f"巡护人员不存在：{payload['ranger_code']}")
                ranger_id = ranger["id"]
            disruption = repository.create_disruption(
                idempotency_key=payload["idempotency_key"], kind=payload["kind"], zone_id=zone_id,
                ranger_id=ranger_id, starts_at=starts_at, ends_at=ends_at,
                reason=payload["reason"], created_by=payload["actor"], now=now,
            )
            report = self._replan(repository, disruption, payload["actor"], now)
            repository.set_disruption_report(disruption["id"], report)
            repository.add_event(
                run_id=None, assignment_id=None, disruption_id=disruption["id"], action="disruption_applied",
                actor=payload["actor"], reason=payload["reason"],
                before={}, after={"affected": report["affected_assignment_ids"], "skipped_in_progress": [item["assignment_id"] for item in report["skipped_in_progress"]]},
                now=now,
            )
            return self._disruption_payload(dict(repository.disruption_by_id(disruption["id"])), replayed=False)

    def list_disruptions(self) -> list[dict[str, Any]]:
        repository = PatrolRepository(self.connection)
        return [self._disruption_payload(row, replayed=False) for row in repository.list_disruptions()]

    def get_disruption(self, disruption_id: int) -> dict[str, Any]:
        repository = PatrolRepository(self.connection)
        row = repository.disruption_by_id(disruption_id)
        if row is None:
            raise NotFoundError("扰动事件不存在")
        return self._disruption_payload(dict(row), replayed=False)

    def resolve_disruption(self, disruption_id: int, actor: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = PatrolRepository(connection)
            row = repository.disruption_by_id(disruption_id)
            if row is None:
                raise NotFoundError("扰动事件不存在")
            if row["status"] != "open":
                raise ConflictError("扰动事件已经解除")
            before = dict(row)
            connection.execute("UPDATE patrol_disruptions SET status='resolved' WHERE id=? AND status='open'", (disruption_id,))
            after = dict(repository.disruption_by_id(disruption_id))
            repository.add_event(
                run_id=None, assignment_id=None, disruption_id=disruption_id, action="disruption_resolved",
                actor=actor, reason="扰动解除，既有分派保持现状", before=before, after=after, now=now,
            )
            return self._disruption_payload(after, replayed=False)

    # ------------------------------------------------------------------
    # 内部：排班引擎
    # ------------------------------------------------------------------
    def _load_context(self, repository: PatrolRepository) -> PlanningContext:
        context = PlanningContext()
        for row in repository.all_windows():
            context.windows.setdefault(row["zone_id"], []).append(
                ZoneWindow(row["kind"], row["rule"], from_storage(row["starts_at"]), from_storage(row["ends_at"]), row["note"])
            )
        for row in repository.list_observations():
            context.observations.setdefault(row["zone_id"], []).append(
                RiskObservation(row["kind"], row["effect"], from_storage(row["starts_at"]), from_storage(row["ends_at"]), row["note"])
            )
        availability: dict[int, list[tuple[datetime, datetime]]] = {}
        for row in repository.all_availability():
            availability.setdefault(row["ranger_id"], []).append((from_storage(row["starts_at"]), from_storage(row["ends_at"])))
        for row in repository.list_rangers():
            if not row["active"]:
                continue
            context.rangers.append(
                RangerInfo(row["id"], row["code"], row["name"], frozenset(json.loads(row["skills_json"])), tuple(availability.get(row["id"], [])))
            )
        context.rangers.sort(key=lambda ranger: ranger.code)
        for row in repository.current_assignments():
            context.blockers.append(
                Blocker(row["id"], row["zone_id"], row["ranger_id"], from_storage(row["planned_start"]), from_storage(row["planned_end"]))
            )
        for row in repository.open_disruptions():
            context.disruptions.append(
                DisruptionInfo(row["id"], row["kind"], row["zone_id"], row["ranger_id"], from_storage(row["starts_at"]), from_storage(row["ends_at"]), row["reason"])
            )
        return context

    @staticmethod
    def _request_spec(item: dict[str, Any], zone: sqlite3.Row) -> dict[str, Any]:
        return {
            "request_key": item["request_key"],
            "zone_id": zone["id"],
            "zone_code": zone["code"],
            "zone_name": zone["name"],
            "required_skill": item["required_skill"],
            "earliest_start": from_storage(item["earliest_start"]),
            "latest_end": from_storage(item["latest_end"]),
            "duration_minutes": item["duration_minutes"],
            "priority": item["priority"],
        }

    def _plan_request(self, context: PlanningContext, request: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        """为单条巡护要求选择时段与人员，并给出可解释的依据。"""
        zone_id = request["zone_id"]
        duration = timedelta(minutes=request["duration_minutes"])
        horizon_start = request["earliest_start"]
        horizon_end = request["latest_end"]
        zone_windows = [w for w in context.windows.get(zone_id, []) if _overlaps(horizon_start, horizon_end, w.start, w.end)]
        zone_observations = [o for o in context.observations.get(zone_id, []) if _overlaps(horizon_start, horizon_end, o.start, o.end)]
        avoid_windows = [w for w in zone_windows if w.rule == "avoid"]
        prefer_windows = [w for w in zone_windows if w.rule == "prefer"]
        avoid_observations = [o for o in zone_observations if o.effect == "avoid"]
        prefer_observations = [o for o in zone_observations if o.effect == "prefer"]
        skilled = [ranger for ranger in context.rangers if request["required_skill"] in ranger.skills]
        base = {
            "request": {
                "request_key": request["request_key"],
                "zone_code": request["zone_code"],
                "zone_name": request["zone_name"],
                "required_skill": request["required_skill"],
                "earliest_start": to_storage(horizon_start),
                "latest_end": to_storage(horizon_end),
                "duration_minutes": request["duration_minutes"],
                "priority": request["priority"],
            }
        }
        if not skilled:
            return None, {
                **base,
                "outcome": "unscheduled",
                "failure_reasons": [f"没有登记技能『{request['required_skill']}』的可用巡护人员"],
                "rejected_counts": {},
                "rejected_samples": [],
            }
        rejected_counts: dict[str, int] = {}
        rejected_samples: list[dict[str, Any]] = []

        def reject(reason: str, slot: datetime) -> None:
            rejected_counts[reason] = rejected_counts.get(reason, 0) + 1
            if len(rejected_samples) < MAX_REJECTED_SAMPLES:
                rejected_samples.append({"slot_start": to_storage(slot), "reason": reason})

        candidates: list[dict[str, Any]] = []
        slot = horizon_start
        while slot + duration <= horizon_end:
            end = slot + duration
            zone_reason = self._zone_block_reason(context, zone_id, slot, end, avoid_windows, avoid_observations)
            if zone_reason is not None:
                reject(zone_reason, slot)
            else:
                feasible = []
                for ranger in skilled:
                    ranger_reason = self._ranger_block_reason(context, ranger, slot, end)
                    if ranger_reason is None:
                        feasible.append(ranger)
                    else:
                        reject(ranger_reason, slot)
                if feasible:
                    prefer_hits = [w.note or w.kind for w in prefer_windows if _overlaps(slot, end, w.start, w.end)]
                    prefer_hits += [o.note or OBSERVATION_LABELS[o.kind] for o in prefer_observations if _overlaps(slot, end, o.start, o.end)]
                    candidates.append({"start": slot, "end": end, "rangers": feasible, "prefer_hits": prefer_hits})
            slot += SLOT_STEP
        if not candidates:
            ordered = sorted(rejected_counts.items(), key=lambda item: (-item[1], item[0]))
            return None, {
                **base,
                "outcome": "unscheduled",
                "failure_reasons": [f"{reason}（排除 {count} 个候选时段）" for reason, count in ordered[:3]] or ["需求窗口内没有可行时段"],
                "rejected_counts": rejected_counts,
                "rejected_samples": rejected_samples,
            }
        best = min(candidates, key=lambda item: (-len(item["prefer_hits"]), item["start"]))
        loads = {ranger.code: sum(1 for blocker in context.blockers if blocker.ranger_id == ranger.id) for ranger in best["rangers"]}
        ranger = min(best["rangers"], key=lambda item: (loads[item.code], item.code))
        covering = next((window for window in ranger.availability if window[0] <= best["start"] and window[1] >= best["end"]), None)
        reasons = [f"需求窗口内共 {len(candidates)} 个可行时段，所选时段重点窗口覆盖数最高且开始最早"]
        if best["prefer_hits"]:
            reasons.append(f"所选时段覆盖重点窗口：{'、'.join(best['prefer_hits'])}")
        reasons.append(f"巡护员 {ranger.name}（{ranger.code}）具备技能『{request['required_skill']}』")
        if covering is not None:
            reasons.append(f"可用时段 {to_storage(covering[0])} ~ {to_storage(covering[1])} 完整覆盖巡护时段")
        if len(best["rangers"]) > 1:
            reasons.append(f"该巡护员当前在途任务 {loads[ranger.code]} 项，为候选人员中最低")
        checks = [
            {
                "rule": "protection_window",
                "result": "pass",
                "detail": "；".join(f"避开保护窗口『{w.kind}』（{WINDOW_RULE_LABELS[w.rule]}，{to_storage(w.start)} ~ {to_storage(w.end)}）" for w in avoid_windows)
                if avoid_windows else "需求窗口内没有回避类保护窗口",
            },
            {
                "rule": "risk_observation",
                "result": "pass",
                "detail": "；".join(f"避开风险观察『{OBSERVATION_LABELS[o.kind]}』（{to_storage(o.start)} ~ {to_storage(o.end)}）" for o in avoid_observations)
                if avoid_observations else "需求窗口内没有需要回避的风险观察",
            },
            {
                "rule": "route_conflict",
                "result": "pass",
                "detail": (
                    f"已核对区域内 {len(zone_blockers)} 个在途分派，所选时段无重叠"
                    if (zone_blockers := [b for b in context.blockers if b.zone_id == zone_id])
                    else "区域内当前没有在途巡护分派"
                ),
            },
            {"rule": "ranger_skill", "result": "pass", "detail": f"{ranger.name} 具备技能『{request['required_skill']}』"},
            {"rule": "ranger_availability", "result": "pass", "detail": "巡护员可用时段覆盖所选巡护时段"},
            {"rule": "ranger_double_booking", "result": "pass", "detail": "巡护员在所选时段没有其他任务"},
            {
                "rule": "disruption",
                "result": "pass",
                "detail": f"已核对 {len(context.disruptions)} 个生效中的扰动，所选时段不受影响" if context.disruptions else "当前没有生效中的扰动",
            },
        ]
        rationale = {
            **base,
            "outcome": "planned",
            "decision": {
                "planned_start": to_storage(best["start"]),
                "planned_end": to_storage(best["end"]),
                "ranger": {"id": ranger.id, "code": ranger.code, "name": ranger.name},
                "prefer_coverage": best["prefer_hits"],
                "candidates_considered": len(candidates),
            },
            "reasons": reasons,
            "checks": checks,
            "rejected_counts": rejected_counts,
            "rejected_samples": rejected_samples,
        }
        return {"start": best["start"], "end": best["end"], "ranger": ranger}, rationale

    def _zone_block_reason(self, context: PlanningContext, zone_id: int, start: datetime, end: datetime, avoid_windows: list[ZoneWindow], avoid_observations: list[RiskObservation]) -> str | None:
        for window in avoid_windows:
            if _overlaps(start, end, window.start, window.end):
                return f"与保护窗口『{window.kind}』冲突"
        for observation in avoid_observations:
            if _overlaps(start, end, observation.start, observation.end):
                return f"与风险观察『{OBSERVATION_LABELS[observation.kind]}』冲突"
        for disruption in context.disruptions:
            if not _overlaps(start, end, disruption.start, disruption.end):
                continue
            if disruption.kind == "heavy_rain" and (disruption.zone_id is None or disruption.zone_id == zone_id):
                return "暴雨扰动时段禁止安排"
            if disruption.kind == "zone_closed" and disruption.zone_id == zone_id:
                return "区域封闭扰动时段禁止安排"
        for blocker in context.blockers:
            if blocker.zone_id == zone_id and _overlaps(start, end, blocker.start, blocker.end):
                return f"与已有巡护分派 #{blocker.assignment_id} 路线冲突"
        return None

    def _ranger_block_reason(self, context: PlanningContext, ranger: RangerInfo, start: datetime, end: datetime) -> str | None:
        if not any(window_start <= start and window_end >= end for window_start, window_end in ranger.availability):
            return f"巡护员 {ranger.code} 可用时段不覆盖"
        for blocker in context.blockers:
            if blocker.ranger_id == ranger.id and _overlaps(start, end, blocker.start, blocker.end):
                return f"巡护员 {ranger.code} 同时段已有任务 #{blocker.assignment_id}"
        for disruption in context.disruptions:
            if disruption.kind == "ranger_unavailable" and disruption.ranger_id == ranger.id and _overlaps(start, end, disruption.start, disruption.end):
                return f"巡护员 {ranger.code} 在扰动时段退出"
        return None

    # ------------------------------------------------------------------
    # 内部：扰动改派
    # ------------------------------------------------------------------
    def _replan(self, repository: PatrolRepository, disruption: dict[str, Any], actor: str, now: str) -> dict[str, Any]:
        """只重排受扰动影响且尚未开始的任务，保留原安排并记录原因。"""
        start = from_storage(disruption["starts_at"])
        end = from_storage(disruption["ends_at"])
        label = DISRUPTION_LABELS[disruption["kind"]]
        affected: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for row in repository.current_assignments():
            if not _overlaps(from_storage(row["planned_start"]), from_storage(row["planned_end"]), start, end):
                continue
            if not self._in_scope(disruption, row):
                continue
            if row["status"] == "planned":
                affected.append(row)
            else:
                skipped.append(row)
        report: dict[str, Any] = {
            "disruption_id": disruption["id"],
            "kind": disruption["kind"],
            "kind_label": label,
            "reason": disruption["reason"],
            "affected_assignment_ids": [],
            "rescheduled": [],
            "unscheduled": [],
            "skipped_in_progress": [
                {"assignment_id": row["id"], "zone_code": row["zone_code"], "note": "巡护已开始，保留原安排不自动改派"}
                for row in skipped
            ],
        }
        for row in sorted(affected, key=lambda item: (item["planned_start"], item["id"])):
            before = dict(row)
            cursor = repository.connection.execute(
                "UPDATE patrol_assignments SET status='rescheduled',updated_at=?,version=version+1 WHERE id=? AND status='planned'",
                (now, row["id"]),
            )
            if cursor.rowcount != 1:
                continue
            report["affected_assignment_ids"].append(row["id"])
            after = dict(repository.assignment_by_id(row["id"]))
            repository.add_event(
                run_id=row["run_id"], assignment_id=row["id"], disruption_id=disruption["id"],
                action="rescheduled", actor=actor, reason=f"扰动改派：{label}——{disruption['reason']}",
                before=before, after=after, now=now,
            )
            context = self._load_context(repository)
            request_row = repository.request_by_id(row["request_id"])
            zone = repository.zone_by_id(row["zone_id"])
            request = {
                "request_key": request_row["request_key"],
                "zone_id": zone["id"],
                "zone_code": zone["code"],
                "zone_name": zone["name"],
                "required_skill": request_row["required_skill"],
                "earliest_start": from_storage(request_row["earliest_start"]),
                "latest_end": from_storage(request_row["latest_end"]),
                "duration_minutes": request_row["duration_minutes"],
                "priority": request_row["priority"],
            }
            choice, rationale = self._plan_request(context, request)
            rationale["replan"] = {
                "disruption_id": disruption["id"],
                "kind": disruption["kind"],
                "kind_label": label,
                "reason": disruption["reason"],
                "replaces_assignment_id": row["id"],
            }
            if choice is not None:
                assignment = repository.create_assignment(
                    run_id=row["run_id"], request_id=request_row["id"], zone_id=zone["id"],
                    ranger_id=choice["ranger"].id, planned_start=to_storage(choice["start"]),
                    planned_end=to_storage(choice["end"]), status="planned",
                    rationale=rationale, replaces_id=row["id"], now=now,
                )
                repository.update_request_status(request_row["id"], "scheduled")
                repository.add_event(
                    run_id=row["run_id"], assignment_id=assignment["id"], disruption_id=disruption["id"],
                    action="created", actor=actor, reason=f"扰动改派生成：{label}——{disruption['reason']}",
                    before={}, after=assignment, now=now,
                )
                report["rescheduled"].append({
                    "from_assignment_id": row["id"],
                    "to_assignment_id": assignment["id"],
                    "ranger_code": assignment["ranger_code"],
                    "planned_start": assignment["planned_start"],
                    "planned_end": assignment["planned_end"],
                })
            else:
                assignment = repository.create_assignment(
                    run_id=row["run_id"], request_id=request_row["id"], zone_id=zone["id"],
                    ranger_id=None, planned_start=None, planned_end=None, status="unscheduled",
                    rationale=rationale, replaces_id=row["id"], now=now,
                )
                repository.update_request_status(request_row["id"], "unscheduled")
                repository.add_event(
                    run_id=row["run_id"], assignment_id=assignment["id"], disruption_id=disruption["id"],
                    action="unscheduled", actor=actor, reason=f"扰动改派未找到可行时段：{label}——{disruption['reason']}",
                    before={}, after=assignment, now=now,
                )
                report["unscheduled"].append({
                    "from_assignment_id": row["id"],
                    "assignment_id": assignment["id"],
                    "failure_reasons": rationale.get("failure_reasons", []),
                })
        return report

    @staticmethod
    def _in_scope(disruption: dict[str, Any], assignment: dict[str, Any]) -> bool:
        kind = disruption["kind"]
        if kind == "heavy_rain":
            return disruption["zone_id"] is None or disruption["zone_id"] == assignment["zone_id"]
        if kind == "zone_closed":
            return disruption["zone_id"] == assignment["zone_id"]
        if kind == "ranger_unavailable":
            return disruption["ranger_id"] == assignment["ranger_id"]
        return False

    # ------------------------------------------------------------------
    # 内部：状态迁移与序列化
    # ------------------------------------------------------------------
    def _transition(self, assignment_id: int, actor: str, action: str, from_status: str, to_status: str, reason: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = PatrolRepository(connection)
            row = repository.assignment_by_id(assignment_id)
            if row is None:
                raise NotFoundError("巡护分派不存在")
            if row["status"] != from_status:
                raise ConflictError(f"当前状态为 {ASSIGNMENT_STATUS_LABELS.get(row['status'], row['status'])}，不允许该操作")
            before = dict(row)
            if to_status == "in_progress":
                cursor = connection.execute(
                    "UPDATE patrol_assignments SET status=?,started_at=?,updated_at=?,version=version+1 WHERE id=? AND status=?",
                    (to_status, now, now, assignment_id, from_status),
                )
            else:
                cursor = connection.execute(
                    "UPDATE patrol_assignments SET status=?,finished_at=?,updated_at=?,version=version+1 WHERE id=? AND status=?",
                    (to_status, now, now, assignment_id, from_status),
                )
            if cursor.rowcount != 1:
                raise ConflictError("巡护分派状态已变化，请刷新后重试")
            after = dict(repository.assignment_by_id(assignment_id))
            repository.add_event(
                run_id=after["run_id"], assignment_id=assignment_id, disruption_id=None,
                action=action, actor=actor, reason=reason, before=before, after=after, now=now,
            )
            return self._assignment_payload(after)

    def _run_detail(self, repository: PatrolRepository, run_id: int, replayed: bool) -> dict[str, Any]:
        run = dict(repository.run_by_id(run_id))
        requests = repository.requests_for_run(run_id)
        assignments = [self._assignment_payload(row) for row in repository.assignments_for_run(run_id)]
        superseded = {assignment["replaces_id"] for assignment in assignments if assignment["replaces_id"]}
        current = [assignment for assignment in assignments if assignment["id"] not in superseded]
        states: dict[str, int] = {}
        for assignment in current:
            states[assignment["status"]] = states.get(assignment["status"], 0) + 1
        current_by_request = {assignment["request_id"]: assignment["id"] for assignment in current}
        for request in requests:
            request["current_assignment_id"] = current_by_request.get(request["id"])
        run["requests"] = requests
        run["assignments"] = assignments
        run["states"] = states
        run["replayed"] = replayed
        return run

    def _zone_payload(self, repository: PatrolRepository, zone_id: int) -> dict[str, Any]:
        zone = dict(repository.zone_by_id(zone_id))
        zone["protection_windows"] = repository.windows_for_zone(zone_id)
        zone["risk_observations"] = repository.list_observations(zone_id)
        return zone

    def _ranger_payload(self, repository: PatrolRepository, ranger_id: int) -> dict[str, Any]:
        ranger = dict(repository.ranger_by_id(ranger_id))
        ranger["skills"] = json.loads(ranger.pop("skills_json"))
        ranger["availability"] = repository.availability_for_ranger(ranger_id)
        return ranger

    @staticmethod
    def _assignment_payload(row: dict[str, Any]) -> dict[str, Any]:
        payload = dict(row)
        payload["rationale"] = json.loads(payload.pop("rationale_json") or "{}")
        return payload

    @staticmethod
    def _event_payload(row: dict[str, Any]) -> dict[str, Any]:
        payload = dict(row)
        payload["before"] = json.loads(payload.pop("before_json") or "{}")
        payload["after"] = json.loads(payload.pop("after_json") or "{}")
        return payload

    @staticmethod
    def _disruption_payload(row: dict[str, Any], replayed: bool) -> dict[str, Any]:
        payload = dict(row)
        payload["kind_label"] = DISRUPTION_LABELS.get(payload["kind"], payload["kind"])
        payload["report"] = json.loads(payload.pop("report_json") or "{}")
        payload["replayed"] = replayed
        return payload
