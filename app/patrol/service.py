from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from app.core.clock import Clock, SystemClock, from_storage, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.database import get_connection, transaction

SCHEMA = """
CREATE TABLE IF NOT EXISTS patrol_zones (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    habitat_type TEXT NOT NULL,
    route_group TEXT NOT NULL,
    visit_minutes INTEGER NOT NULL CHECK(visit_minutes > 0),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS patrol_protection_windows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    zone_id INTEGER NOT NULL REFERENCES patrol_zones(id) ON DELETE RESTRICT,
    kind TEXT NOT NULL CHECK(kind IN ('bird_breeding','waterlogging','visitor_peak')),
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    required_skill TEXT NOT NULL DEFAULT '',
    min_interval_minutes INTEGER NOT NULL DEFAULT 0 CHECK(min_interval_minutes >= 0),
    priority_boost INTEGER NOT NULL DEFAULT 0 CHECK(priority_boost >= 0),
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_patrol_windows_zone ON patrol_protection_windows(zone_id, starts_at);
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
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_patrol_availability_ranger ON patrol_ranger_availability(ranger_id, starts_at);
CREATE TABLE IF NOT EXISTS patrol_risk_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    zone_id INTEGER NOT NULL REFERENCES patrol_zones(id) ON DELETE RESTRICT,
    kind TEXT NOT NULL,
    severity INTEGER NOT NULL CHECK(severity BETWEEN 1 AND 5),
    note TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','closed')),
    observed_at TEXT NOT NULL,
    closed_at TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_patrol_risks_zone ON patrol_risk_observations(zone_id, status);
CREATE TABLE IF NOT EXISTS patrol_schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_key TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','archived')),
    created_at TEXT NOT NULL,
    UNIQUE(requested_by, batch_key)
);
CREATE TABLE IF NOT EXISTS patrol_assignments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    schedule_id INTEGER NOT NULL REFERENCES patrol_schedules(id) ON DELETE RESTRICT,
    request_index INTEGER NOT NULL DEFAULT 0,
    zone_id INTEGER NOT NULL REFERENCES patrol_zones(id) ON DELETE RESTRICT,
    ranger_id INTEGER REFERENCES patrol_rangers(id) ON DELETE RESTRICT,
    start_at TEXT,
    end_at TEXT,
    status TEXT NOT NULL DEFAULT 'planned' CHECK(status IN ('planned','unassigned','in_progress','completed','cancelled','rescheduled')),
    priority INTEGER NOT NULL DEFAULT 0,
    rationale_json TEXT NOT NULL DEFAULT '[]',
    origin_assignment_id INTEGER REFERENCES patrol_assignments(id),
    replaced_by_id INTEGER,
    request_json TEXT NOT NULL DEFAULT '{}',
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_patrol_assignments_time ON patrol_assignments(status, start_at);
CREATE INDEX IF NOT EXISTS idx_patrol_assignments_zone ON patrol_assignments(zone_id, status);
CREATE INDEX IF NOT EXISTS idx_patrol_assignments_ranger ON patrol_assignments(ranger_id, status);
CREATE TABLE IF NOT EXISTS patrol_assignment_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    assignment_id INTEGER NOT NULL REFERENCES patrol_assignments(id) ON DELETE CASCADE,
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    before_json TEXT NOT NULL DEFAULT '{}',
    after_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_patrol_events_assignment ON patrol_assignment_events(assignment_id, id);
CREATE TABLE IF NOT EXISTS patrol_disruptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    disruption_key TEXT NOT NULL,
    actor TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('heavy_rain','zone_closed','ranger_unavailable')),
    zone_id INTEGER REFERENCES patrol_zones(id),
    ranger_id INTEGER REFERENCES patrol_rangers(id),
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    reason TEXT NOT NULL,
    affected_json TEXT NOT NULL DEFAULT '[]',
    skipped_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    UNIQUE(actor, disruption_key)
);
"""

WINDOW_KIND_LABELS = {
    "bird_breeding": "鸟类繁殖期",
    "waterlogging": "滩涂积水",
    "visitor_peak": "游客密集时段",
}

DISRUPTION_KIND_LABELS = {
    "heavy_rain": "暴雨",
    "zone_closed": "区域封闭",
    "ranger_unavailable": "人员临时退出",
}

SLOT_STEP = timedelta(minutes=15)
REPLAN_HORIZON = timedelta(hours=24)
RISK_PRIORITY_CAP = 15


def ensure_schema() -> None:
    get_connection().executescript(SCHEMA)


def _digest(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def _overlaps(start_a: str, end_a: str, start_b: str, end_b: str) -> bool:
    return start_a < end_b and end_a > start_b


@dataclass
class _PlanContext:
    """一次排班/改派事务内共享的只读登记数据与实时负荷。"""

    zones_by_code: dict[str, dict[str, Any]]
    zones_by_id: dict[int, dict[str, Any]]
    rangers: list[dict[str, Any]]
    availability: dict[int, list[dict[str, Any]]]
    windows: list[dict[str, Any]]
    risks: dict[int, list[dict[str, Any]]]
    loads: dict[int, int] = field(default_factory=dict)


class PatrolService:
    """湿地巡护的登记、排班、改派与查询事务服务。"""

    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        self.clock = clock or SystemClock()
        ensure_schema()

    # ------------------------------------------------------------------
    # 登记：生境、保护窗口、人员技能与可用时段、风险观察
    # ------------------------------------------------------------------

    def create_zone(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            if connection.execute("SELECT 1 FROM patrol_zones WHERE code=?", (payload["code"],)).fetchone():
                raise ConflictError("生境编码已存在")
            cursor = connection.execute(
                "INSERT INTO patrol_zones(code,name,habitat_type,route_group,visit_minutes,active,created_at,updated_at) VALUES(?,?,?,?,?,1,?,?)",
                (payload["code"], payload["name"], payload["habitat_type"], payload["route_group"], payload["visit_minutes"], now, now),
            )
            return dict(connection.execute("SELECT * FROM patrol_zones WHERE id=?", (cursor.lastrowid,)).fetchone())

    def list_zones(self) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM patrol_zones ORDER BY code").fetchall()
        return [dict(row) for row in rows]

    def create_protection_window(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            zone = self._zone_by_code(connection, payload["zone_code"])
            cursor = connection.execute(
                "INSERT INTO patrol_protection_windows(zone_id,kind,starts_at,ends_at,required_skill,min_interval_minutes,priority_boost,note,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    zone["id"],
                    payload["kind"],
                    to_storage(payload["starts_at"]),
                    to_storage(payload["ends_at"]),
                    payload["required_skill"],
                    payload["min_interval_minutes"],
                    payload["priority_boost"],
                    payload["note"],
                    now,
                ),
            )
            return self._window_view(connection, cursor.lastrowid)

    def list_protection_windows(self, zone_code: str | None = None) -> list[dict[str, Any]]:
        sql = (
            "SELECT w.*, z.code AS zone_code, z.name AS zone_name FROM patrol_protection_windows w "
            "JOIN patrol_zones z ON z.id=w.zone_id"
        )
        params: list[Any] = []
        if zone_code:
            sql += " WHERE z.code=?"
            params.append(zone_code)
        sql += " ORDER BY w.starts_at, w.id"
        rows = self.connection.execute(sql, params).fetchall()
        return [self._window_dict(row) for row in rows]

    def create_ranger(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            if connection.execute("SELECT 1 FROM patrol_rangers WHERE code=?", (payload["code"],)).fetchone():
                raise ConflictError("巡护员编码已存在")
            cursor = connection.execute(
                "INSERT INTO patrol_rangers(code,name,skills_json,active,created_at,updated_at) VALUES(?,?,?,1,?,?)",
                (payload["code"], payload["name"], json.dumps(payload["skills"], ensure_ascii=False), now, now),
            )
            return self._ranger_view(connection, cursor.lastrowid)

    def list_rangers(self) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM patrol_rangers ORDER BY code").fetchall()
        return [self._ranger_dict(self.connection, row) for row in rows]

    def add_availability(self, ranger_code: str, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            ranger = self._ranger_by_code(connection, ranger_code)
            cursor = connection.execute(
                "INSERT INTO patrol_ranger_availability(ranger_id,starts_at,ends_at,created_at) VALUES(?,?,?,?)",
                (ranger["id"], to_storage(payload["starts_at"]), to_storage(payload["ends_at"]), now),
            )
            row = connection.execute("SELECT * FROM patrol_ranger_availability WHERE id=?", (cursor.lastrowid,)).fetchone()
            result = dict(row)
            result["ranger_code"] = ranger["code"]
            return result

    def create_risk_observation(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        observed_at = to_storage(payload["observed_at"]) if payload.get("observed_at") else now
        with transaction(immediate=True) as connection:
            zone = self._zone_by_code(connection, payload["zone_code"])
            cursor = connection.execute(
                "INSERT INTO patrol_risk_observations(zone_id,kind,severity,note,status,observed_at,created_at) VALUES(?,?,?,?,'open',?,?)",
                (zone["id"], payload["kind"], payload["severity"], payload["note"], observed_at, now),
            )
            return self._risk_view(connection, cursor.lastrowid)

    def list_risk_observations(self, zone_code: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        sql = (
            "SELECT r.*, z.code AS zone_code, z.name AS zone_name FROM patrol_risk_observations r "
            "JOIN patrol_zones z ON z.id=r.zone_id"
        )
        clauses: list[str] = []
        params: list[Any] = []
        if zone_code:
            clauses.append("z.code=?")
            params.append(zone_code)
        if status:
            clauses.append("r.status=?")
            params.append(status)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY r.observed_at DESC, r.id DESC"
        rows = self.connection.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def close_risk_observation(self, risk_id: int, actor: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            row = connection.execute("SELECT * FROM patrol_risk_observations WHERE id=?", (risk_id,)).fetchone()
            if row is None:
                raise NotFoundError("风险观察不存在")
            if row["status"] == "closed":
                return self._risk_view(connection, risk_id)
            connection.execute("UPDATE patrol_risk_observations SET status='closed', closed_at=? WHERE id=?", (now, risk_id))
            return self._risk_view(connection, risk_id)

    # ------------------------------------------------------------------
    # 排班生成（幂等）
    # ------------------------------------------------------------------

    def generate_schedule(self, payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
        now = to_storage(self.clock.now())
        normalized = [
            {
                "zone_code": item["zone_code"],
                "window_start": to_storage(item["window_start"]),
                "window_end": to_storage(item["window_end"]),
                "required_skill": item["required_skill"],
                "purpose": item["purpose"],
            }
            for item in payload["requests"]
        ]
        request_digest = _digest({"title": payload["title"], "requests": normalized})
        with transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT * FROM patrol_schedules WHERE requested_by=? AND batch_key=?",
                (payload["requested_by"], payload["batch_key"]),
            ).fetchone()
            if existing is not None:
                if existing["request_digest"] != request_digest:
                    raise ConflictError("同一批次键对应了不同的巡护要求")
                view = self._schedule_view(connection, existing["id"])
                view["replayed"] = True
                return view, 200
            context = self._load_context(connection)
            for index, item in enumerate(normalized):
                zone = context.zones_by_code.get(item["zone_code"])
                if zone is None:
                    raise ValidationError("巡护要求引用了未登记或已停用的生境", context={"zone_code": item["zone_code"], "index": index})
                start = from_storage(item["window_start"])
                end = from_storage(item["window_end"])
                if (end - start) < timedelta(minutes=zone["visit_minutes"]):
                    raise ValidationError(
                        "巡护窗口短于该区域所需巡护时长",
                        context={"zone_code": item["zone_code"], "visit_minutes": zone["visit_minutes"], "index": index},
                    )
            cursor = connection.execute(
                "INSERT INTO patrol_schedules(batch_key,requested_by,request_digest,title,status,created_at) VALUES(?,?,?,?,'active',?)",
                (payload["batch_key"], payload["requested_by"], request_digest, payload["title"], now),
            )
            schedule_id = cursor.lastrowid
            ordered = sorted(
                enumerate(normalized),
                key=lambda pair: (
                    -self._request_priority(context, pair[1]),
                    pair[1]["window_start"],
                    pair[0],
                ),
            )
            for request_index, item in ordered:
                zone = context.zones_by_code[item["zone_code"]]
                plan = self._plan_slot(
                    connection,
                    context,
                    zone,
                    item,
                    horizon_end=from_storage(item["window_end"]),
                )
                self._insert_assignment(
                    connection,
                    context,
                    schedule_id=schedule_id,
                    request_index=request_index,
                    zone=zone,
                    plan=plan,
                    request_payload=item,
                    origin_assignment_id=None,
                    actor=payload["requested_by"],
                    action="created",
                    reason="",
                )
            view = self._schedule_view(connection, schedule_id)
            view["replayed"] = False
            return view, 201

    # ------------------------------------------------------------------
    # 扰动改派：暴雨、区域封闭、人员临时退出
    # ------------------------------------------------------------------

    def report_disruption(self, payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
        now = to_storage(self.clock.now())
        starts_at = to_storage(payload["starts_at"])
        ends_at = to_storage(payload["ends_at"])
        with transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT * FROM patrol_disruptions WHERE actor=? AND disruption_key=?",
                (payload["actor"], payload["disruption_key"]),
            ).fetchone()
            if existing is not None:
                return self._disruption_view(connection, existing["id"]) | {"replayed": True}, 200
            context = self._load_context(connection)
            zone = self._zone_by_code(connection, payload["zone_code"]) if payload.get("zone_code") else None
            ranger = self._ranger_by_code(connection, payload["ranger_code"]) if payload.get("ranger_code") else None
            clauses = ["a.status IN ('planned','in_progress')", "a.start_at < ?", "a.end_at > ?"]
            params: list[Any] = [ends_at, starts_at]
            if payload["kind"] == "zone_closed":
                clauses.append("a.zone_id=?")
                params.append(zone["id"])
            elif payload["kind"] == "ranger_unavailable":
                clauses.append("a.ranger_id=?")
                params.append(ranger["id"])
            elif zone is not None:
                clauses.append("a.zone_id=?")
                params.append(zone["id"])
            rows = connection.execute(
                "SELECT a.* FROM patrol_assignments a WHERE " + " AND ".join(clauses) + " ORDER BY a.start_at, a.id",
                params,
            ).fetchall()
            affected: list[dict[str, Any]] = []
            skipped: list[dict[str, Any]] = []
            kind_label = DISRUPTION_KIND_LABELS[payload["kind"]]
            for row in rows:
                if row["status"] != "planned":
                    skipped.append(
                        {
                            "assignment_id": row["id"],
                            "status": row["status"],
                            "note": "巡护已开始，保留原安排不静默改写",
                        }
                    )
                    continue
                before = self._assignment_view(connection, row["id"])
                connection.execute(
                    "UPDATE patrol_assignments SET status='rescheduled', updated_at=?, version=version+1 WHERE id=?",
                    (now, row["id"]),
                )
                if row["ranger_id"] is not None:
                    old_ranger = int(row["ranger_id"])
                    context.loads[old_ranger] = max(0, context.loads.get(old_ranger, 1) - 1)
                self._add_event(
                    connection,
                    assignment_id=row["id"],
                    action="rescheduled",
                    actor=payload["actor"],
                    reason=f"{kind_label}：{payload['reason']}",
                    before=before,
                    after=self._assignment_view(connection, row["id"]),
                )
                request_payload = json.loads(row["request_json"])
                target_zone = context.zones_by_id[row["zone_id"]]
                blocked_intervals: list[tuple[str, str]] = []
                excluded_rangers: set[int] = set()
                if payload["kind"] in {"heavy_rain", "zone_closed"}:
                    blocked_intervals.append((starts_at, ends_at))
                if payload["kind"] == "ranger_unavailable" and row["ranger_id"] is not None:
                    excluded_rangers.add(int(row["ranger_id"]))
                plan = self._plan_slot(
                    connection,
                    context,
                    target_zone,
                    request_payload,
                    horizon_end=from_storage(request_payload["window_end"]) + REPLAN_HORIZON,
                    blocked_intervals=blocked_intervals,
                    excluded_rangers=excluded_rangers,
                )
                new_id = self._insert_assignment(
                    connection,
                    context,
                    schedule_id=row["schedule_id"],
                    request_index=row["request_index"],
                    zone=target_zone,
                    plan=plan,
                    request_payload=request_payload,
                    origin_assignment_id=row["id"],
                    actor=payload["actor"],
                    action="created",
                    reason=f"改派自分派 #{row['id']}：{kind_label}，{payload['reason']}",
                )
                connection.execute("UPDATE patrol_assignments SET replaced_by_id=? WHERE id=?", (new_id, row["id"]))
                affected.append(
                    {
                        "assignment_id": row["id"],
                        "action": "rescheduled",
                        "replaced_by_id": new_id,
                        "new_status": plan["status"],
                    }
                )
            cursor = connection.execute(
                "INSERT INTO patrol_disruptions(disruption_key,actor,kind,zone_id,ranger_id,starts_at,ends_at,reason,affected_json,skipped_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    payload["disruption_key"],
                    payload["actor"],
                    payload["kind"],
                    zone["id"] if zone else None,
                    ranger["id"] if ranger else None,
                    starts_at,
                    ends_at,
                    payload["reason"],
                    json.dumps(affected, ensure_ascii=False),
                    json.dumps(skipped, ensure_ascii=False),
                    now,
                ),
            )
            return self._disruption_view(connection, cursor.lastrowid) | {"replayed": False}, 201

    # ------------------------------------------------------------------
    # 分派生命周期
    # ------------------------------------------------------------------

    def start_assignment(self, assignment_id: int, actor: str, expected_version: int | None) -> dict[str, Any]:
        def mutate(connection: sqlite3.Connection, row: sqlite3.Row, now: str) -> None:
            if row["status"] != "planned":
                raise ConflictError("只有待执行的分派可以开始巡护")
            connection.execute(
                "UPDATE patrol_assignments SET status='in_progress', updated_at=?, version=version+1 WHERE id=?",
                (now, assignment_id),
            )

        return self._mutate_assignment(assignment_id, actor, expected_version, mutate, "started", "")

    def complete_assignment(self, assignment_id: int, actor: str, expected_version: int | None) -> dict[str, Any]:
        def mutate(connection: sqlite3.Connection, row: sqlite3.Row, now: str) -> None:
            if row["status"] != "in_progress":
                raise ConflictError("只有进行中的巡护可以办结")
            connection.execute(
                "UPDATE patrol_assignments SET status='completed', updated_at=?, version=version+1 WHERE id=?",
                (now, assignment_id),
            )

        return self._mutate_assignment(assignment_id, actor, expected_version, mutate, "completed", "")

    def cancel_assignment(self, assignment_id: int, actor: str, reason: str, expected_version: int | None) -> dict[str, Any]:
        def mutate(connection: sqlite3.Connection, row: sqlite3.Row, now: str) -> None:
            if row["status"] not in {"planned", "unassigned"}:
                raise ConflictError("已开始的巡护不能取消，请按扰动流程处理")
            connection.execute(
                "UPDATE patrol_assignments SET status='cancelled', updated_at=?, version=version+1 WHERE id=?",
                (now, assignment_id),
            )

        return self._mutate_assignment(assignment_id, actor, expected_version, mutate, "cancelled", reason)

    def reassign_assignment(self, assignment_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        def mutate(connection: sqlite3.Connection, row: sqlite3.Row, now: str) -> None:
            if row["status"] not in {"planned", "unassigned"}:
                raise ConflictError("已开始的巡护不能静默改写，只有未开始的分派可以改派")
            context = self._load_context(connection)
            ranger_row = self._ranger_by_code(connection, payload["ranger_code"])
            if not ranger_row["active"]:
                raise ValidationError("巡护员已停用")
            ranger = dict(ranger_row)
            ranger["skills"] = set(json.loads(ranger_row["skills_json"]))
            zone = context.zones_by_id[row["zone_id"]]
            start = payload["start_at"]
            end = start + timedelta(minutes=zone["visit_minutes"])
            start_str, end_str = to_storage(start), to_storage(end)
            slot_windows = self._slot_windows(context, zone["id"], start_str, end_str)
            required = self._required_skills(json.loads(row["request_json"]), slot_windows)
            reasons = self._ranger_blocking(context, connection, ranger, required, start_str, end_str, set(), exclude_assignment_id=assignment_id)
            reasons += self._slot_blocking(context, connection, zone, start_str, end_str, [], exclude_assignment_id=assignment_id)
            if reasons:
                raise ValidationError("改派时段不可行", context={"reasons": reasons})
            rationale = json.loads(row["rationale_json"])
            rationale.append(f"人工改派：{payload['reason']}")
            rationale.append(f"巡护员 {ranger['name']}（{ranger['code']}）于 {start_str} 接手")
            connection.execute(
                "UPDATE patrol_assignments SET ranger_id=?, start_at=?, end_at=?, status='planned', rationale_json=?, updated_at=?, version=version+1 WHERE id=?",
                (ranger["id"], start_str, end_str, json.dumps(rationale, ensure_ascii=False), now, assignment_id),
            )

        return self._mutate_assignment(assignment_id, payload["actor"], payload["expected_version"], mutate, "reassigned", payload["reason"])

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def list_schedules(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT s.*, (SELECT COUNT(*) FROM patrol_assignments a WHERE a.schedule_id=s.id) AS assignment_count "
            "FROM patrol_schedules s ORDER BY s.id DESC LIMIT ?",
            (max(1, min(limit, 500)),),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_schedule(self, schedule_id: int) -> dict[str, Any]:
        view = self._schedule_view(self.connection, schedule_id)
        if view is None:
            raise NotFoundError("巡护日程不存在")
        return view

    def get_assignment(self, assignment_id: int) -> dict[str, Any]:
        view = self._assignment_view(self.connection, assignment_id)
        if view is None:
            raise NotFoundError("巡护分派不存在")
        rows = self.connection.execute(
            "SELECT * FROM patrol_assignment_events WHERE assignment_id=? ORDER BY id",
            (assignment_id,),
        ).fetchall()
        view["events"] = [
            {
                **{key: value for key, value in dict(row).items() if key not in {"before_json", "after_json"}},
                "before": json.loads(row["before_json"]),
                "after": json.loads(row["after_json"]),
            }
            for row in rows
        ]
        return view

    def list_assignments(
        self,
        *,
        status: str | None = None,
        zone_code: str | None = None,
        ranger_code: str | None = None,
        day: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        sql = (
            "SELECT a.*, z.code AS zone_code, z.name AS zone_name, z.habitat_type, z.route_group, "
            "r.code AS ranger_code, r.name AS ranger_name "
            "FROM patrol_assignments a JOIN patrol_zones z ON z.id=a.zone_id "
            "LEFT JOIN patrol_rangers r ON r.id=a.ranger_id"
        )
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("a.status=?")
            params.append(status)
        if zone_code:
            clauses.append("z.code=?")
            params.append(zone_code)
        if ranger_code:
            clauses.append("r.code=?")
            params.append(ranger_code)
        if day:
            clauses.append("a.start_at LIKE ?")
            params.append(day + "%")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY a.start_at IS NULL, a.start_at, a.id LIMIT ?"
        params.append(max(1, min(limit, 500)))
        rows = self.connection.execute(sql, params).fetchall()
        return [self._assignment_dict(row) for row in rows]

    def list_disruptions(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT id FROM patrol_disruptions ORDER BY id DESC LIMIT ?",
            (max(1, min(limit, 500)),),
        ).fetchall()
        return [self._disruption_view(self.connection, row["id"]) for row in rows]

    def summary(self) -> dict[str, Any]:
        states = {
            row["status"]: row["amount"]
            for row in self.connection.execute("SELECT status, COUNT(*) AS amount FROM patrol_assignments GROUP BY status").fetchall()
        }
        now = to_storage(self.clock.now())
        horizon = to_storage(self.clock.now() + timedelta(hours=24))
        upcoming = self.connection.execute(
            "SELECT COUNT(*) FROM patrol_assignments WHERE status='planned' AND start_at>=? AND start_at<=?",
            (now, horizon),
        ).fetchone()[0]
        return {
            "assignments_by_status": states,
            "open_risks": self.connection.execute("SELECT COUNT(*) FROM patrol_risk_observations WHERE status='open'").fetchone()[0],
            "active_zones": self.connection.execute("SELECT COUNT(*) FROM patrol_zones WHERE active=1").fetchone()[0],
            "active_rangers": self.connection.execute("SELECT COUNT(*) FROM patrol_rangers WHERE active=1").fetchone()[0],
            "upcoming_24h": upcoming,
        }

    # ------------------------------------------------------------------
    # 排班引擎
    # ------------------------------------------------------------------

    def _load_context(self, connection: sqlite3.Connection) -> _PlanContext:
        zones = [dict(row) for row in connection.execute("SELECT * FROM patrol_zones WHERE active=1").fetchall()]
        rangers = []
        for row in connection.execute("SELECT * FROM patrol_rangers WHERE active=1").fetchall():
            data = dict(row)
            data["skills"] = set(json.loads(data["skills_json"]))
            rangers.append(data)
        availability: dict[int, list[dict[str, Any]]] = {}
        for row in connection.execute("SELECT * FROM patrol_ranger_availability").fetchall():
            availability.setdefault(int(row["ranger_id"]), []).append(dict(row))
        windows = [dict(row) for row in connection.execute("SELECT * FROM patrol_protection_windows").fetchall()]
        risks: dict[int, list[dict[str, Any]]] = {}
        for row in connection.execute("SELECT * FROM patrol_risk_observations WHERE status='open'").fetchall():
            risks.setdefault(int(row["zone_id"]), []).append(dict(row))
        loads = {
            int(row["ranger_id"]): int(row["amount"])
            for row in connection.execute(
                "SELECT ranger_id, COUNT(*) AS amount FROM patrol_assignments WHERE status IN ('planned','in_progress') AND ranger_id IS NOT NULL GROUP BY ranger_id"
            ).fetchall()
        }
        return _PlanContext(
            zones_by_code={zone["code"]: zone for zone in zones},
            zones_by_id={int(zone["id"]): zone for zone in zones},
            rangers=rangers,
            availability=availability,
            windows=windows,
            risks=risks,
            loads=loads,
        )

    def _request_priority(self, context: _PlanContext, request: dict[str, Any]) -> int:
        zone = context.zones_by_code.get(request["zone_code"])
        if zone is None:
            return 0
        priority = 10
        for window in context.windows:
            if window["zone_id"] == zone["id"] and _overlaps(window["starts_at"], window["ends_at"], request["window_start"], request["window_end"]):
                priority += int(window["priority_boost"])
        risk_bonus = sum(int(risk["severity"]) for risk in context.risks.get(zone["id"], []))
        return priority + min(risk_bonus, RISK_PRIORITY_CAP)

    def _slot_windows(self, context: _PlanContext, zone_id: int, start: str, end: str) -> list[dict[str, Any]]:
        return [
            window
            for window in context.windows
            if window["zone_id"] == zone_id and _overlaps(window["starts_at"], window["ends_at"], start, end)
        ]

    @staticmethod
    def _required_skills(request: dict[str, Any], slot_windows: list[dict[str, Any]]) -> set[str]:
        required = {request["required_skill"]} if request.get("required_skill") else set()
        required |= {window["required_skill"] for window in slot_windows if window["required_skill"]}
        return required

    def _covering_availability(self, context: _PlanContext, ranger_id: int, start: str, end: str) -> dict[str, Any] | None:
        for window in context.availability.get(ranger_id, []):
            if window["starts_at"] <= start and window["ends_at"] >= end:
                return window
        return None

    def _overlapping_assignments(
        self,
        connection: sqlite3.Connection,
        start: str,
        end: str,
        exclude_assignment_id: int | None = None,
    ) -> list[sqlite3.Row]:
        sql = (
            "SELECT a.id, a.ranger_id, a.start_at, a.end_at, z.route_group, z.name AS zone_name "
            "FROM patrol_assignments a JOIN patrol_zones z ON z.id=a.zone_id "
            "WHERE a.status IN ('planned','in_progress') AND a.start_at < ? AND a.end_at > ?"
        )
        params: list[Any] = [end, start]
        if exclude_assignment_id is not None:
            sql += " AND a.id<>?"
            params.append(exclude_assignment_id)
        return connection.execute(sql, params).fetchall()

    def _slot_blocking(
        self,
        context: _PlanContext,
        connection: sqlite3.Connection,
        zone: dict[str, Any],
        start: str,
        end: str,
        blocked_intervals: list[tuple[str, str]],
        exclude_assignment_id: int | None = None,
    ) -> list[str]:
        reasons: list[str] = []
        for blocked_start, blocked_end in blocked_intervals:
            if _overlaps(start, end, blocked_start, blocked_end):
                reasons.append(f"时段落入扰动窗口（{blocked_start}~{blocked_end}）")
        for other in self._overlapping_assignments(connection, start, end, exclude_assignment_id):
            if other["route_group"] == zone["route_group"]:
                reasons.append(f"路线冲突：与分派 #{other['id']}（{other['zone_name']}）同时段")
        slot_windows = self._slot_windows(context, zone["id"], start, end)
        min_interval = max((int(window["min_interval_minutes"]) for window in slot_windows), default=0)
        if min_interval > 0:
            start_dt = from_storage(start) - timedelta(minutes=min_interval)
            end_dt = from_storage(end) + timedelta(minutes=min_interval)
            sql = (
                "SELECT id FROM patrol_assignments WHERE zone_id=? AND status IN ('planned','in_progress') "
                "AND start_at < ? AND end_at > ?"
            )
            params: list[Any] = [zone["id"], to_storage(end_dt), to_storage(start_dt)]
            if exclude_assignment_id is not None:
                sql += " AND id<>?"
                params.append(exclude_assignment_id)
            for row in connection.execute(sql, params).fetchall():
                reasons.append(f"同区域最小巡护间隔 {min_interval} 分钟：与分派 #{row['id']} 过近")
        return reasons

    def _ranger_blocking(
        self,
        context: _PlanContext,
        connection: sqlite3.Connection,
        ranger: dict[str, Any],
        required: set[str],
        start: str,
        end: str,
        excluded_rangers: set[int],
        exclude_assignment_id: int | None = None,
    ) -> list[str]:
        if int(ranger["id"]) in excluded_rangers:
            return ["该巡护员在扰动时段内不可用"]
        missing = sorted(required - ranger["skills"])
        if missing:
            return [f"缺少技能：{'、'.join(missing)}"]
        if self._covering_availability(context, ranger["id"], start, end) is None:
            return ["可用时段不覆盖巡护时段"]
        for other in self._overlapping_assignments(connection, start, end, exclude_assignment_id):
            if other["ranger_id"] == ranger["id"]:
                return [f"与分派 #{other['id']}（{other['zone_name']}）时段重叠"]
        return []

    def _plan_slot(
        self,
        connection: sqlite3.Connection,
        context: _PlanContext,
        zone: dict[str, Any],
        request: dict[str, Any],
        *,
        horizon_end: datetime,
        blocked_intervals: list[tuple[str, str]] | None = None,
        excluded_rangers: set[int] | None = None,
    ) -> dict[str, Any]:
        blocked_intervals = blocked_intervals or []
        excluded_rangers = excluded_rangers or set()
        window_start = from_storage(request["window_start"])
        window_end = from_storage(request["window_end"])
        duration = timedelta(minutes=zone["visit_minutes"])
        priority = self._request_priority(context, request)
        base_rationale = [
            f"巡护窗口 {request['window_start']}~{request['window_end']}，时长 {zone['visit_minutes']} 分钟",
        ]
        zone_risks = context.risks.get(zone["id"], [])
        for risk in zone_risks:
            base_rationale.append(f"开放风险观察：{risk['kind']}（严重度 {risk['severity']}），优先级已提升")
        skip_reasons: list[str] = []
        slot = window_start
        while slot + duration <= horizon_end:
            slot_end = slot + duration
            start_str, end_str = to_storage(slot), to_storage(slot_end)
            slot_windows = self._slot_windows(context, zone["id"], start_str, end_str)
            required = self._required_skills(request, slot_windows)
            slot_reasons = self._slot_blocking(context, connection, zone, start_str, end_str, blocked_intervals)
            if slot_reasons:
                skip_reasons.extend(slot_reasons)
            else:
                candidates: list[tuple[int, str, dict[str, Any]]] = []
                for ranger in context.rangers:
                    reasons = self._ranger_blocking(context, connection, ranger, required, start_str, end_str, excluded_rangers)
                    if reasons:
                        skip_reasons.extend(f"{ranger['name']}：{reason}" for reason in reasons)
                    else:
                        candidates.append((context.loads.get(int(ranger["id"]), 0), ranger["code"], ranger))
                if candidates:
                    candidates.sort(key=lambda item: (item[0], item[1]))
                    chosen = candidates[0][2]
                    rationale = list(base_rationale)
                    for window in slot_windows:
                        label = WINDOW_KIND_LABELS[window["kind"]]
                        entry = f"命中保护窗口：{label}（{window['starts_at']}~{window['ends_at']}）"
                        if window["required_skill"]:
                            entry += f"，要求技能：{window['required_skill']}"
                        rationale.append(entry)
                    if required:
                        rationale.append(f"巡护员 {chosen['name']}（{chosen['code']}）技能匹配：{'、'.join(sorted(required))}")
                    else:
                        rationale.append(f"巡护员 {chosen['name']}（{chosen['code']}）当前负荷最低")
                    covering = self._covering_availability(context, chosen["id"], start_str, end_str)
                    rationale.append(f"可用时段 {covering['starts_at']}~{covering['ends_at']} 覆盖巡护时段")
                    if slot >= window_end:
                        rationale.append("原巡护窗口内无可行空档，顺延至窗口外时段")
                    for reason in list(dict.fromkeys(skip_reasons))[:3]:
                        rationale.append(f"已避开：{reason}")
                    return {
                        "status": "planned",
                        "ranger": chosen,
                        "start_at": start_str,
                        "end_at": end_str,
                        "priority": priority,
                        "rationale": rationale,
                    }
            slot += SLOT_STEP
        rationale = list(base_rationale)
        if not context.rangers:
            rationale.append("没有可用的巡护员")
        deduped = list(dict.fromkeys(skip_reasons))
        rationale.append("窗口内无可行排班空档")
        rationale.extend(f"受阻原因：{reason}" for reason in deduped[:5])
        return {
            "status": "unassigned",
            "ranger": None,
            "start_at": None,
            "end_at": None,
            "priority": priority,
            "rationale": rationale,
        }

    def _insert_assignment(
        self,
        connection: sqlite3.Connection,
        context: _PlanContext,
        *,
        schedule_id: int,
        request_index: int,
        zone: dict[str, Any],
        plan: dict[str, Any],
        request_payload: dict[str, Any],
        origin_assignment_id: int | None,
        actor: str,
        action: str,
        reason: str,
    ) -> int:
        now = to_storage(self.clock.now())
        ranger = plan["ranger"]
        cursor = connection.execute(
            "INSERT INTO patrol_assignments(schedule_id,request_index,zone_id,ranger_id,start_at,end_at,status,priority,rationale_json,origin_assignment_id,request_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                schedule_id,
                request_index,
                zone["id"],
                ranger["id"] if ranger else None,
                plan["start_at"],
                plan["end_at"],
                plan["status"],
                plan["priority"],
                json.dumps(plan["rationale"], ensure_ascii=False),
                origin_assignment_id,
                json.dumps(request_payload, ensure_ascii=False, sort_keys=True),
                now,
                now,
            ),
        )
        assignment_id = int(cursor.lastrowid)
        if ranger is not None and plan["status"] == "planned":
            context.loads[int(ranger["id"])] = context.loads.get(int(ranger["id"]), 0) + 1
        self._add_event(
            connection,
            assignment_id=assignment_id,
            action=action,
            actor=actor,
            reason=reason,
            before={},
            after=self._assignment_view(connection, assignment_id),
        )
        return assignment_id

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _mutate_assignment(
        self,
        assignment_id: int,
        actor: str,
        expected_version: int | None,
        mutation: Any,
        action: str,
        reason: str,
    ) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            row = connection.execute("SELECT * FROM patrol_assignments WHERE id=?", (assignment_id,)).fetchone()
            if row is None:
                raise NotFoundError("巡护分派不存在")
            if expected_version is not None and int(row["version"]) != expected_version:
                raise ConflictError("分派版本已变化，请刷新后重试")
            before = self._assignment_view(connection, assignment_id)
            mutation(connection, row, now)
            after = self._assignment_view(connection, assignment_id)
            self._add_event(
                connection,
                assignment_id=assignment_id,
                action=action,
                actor=actor,
                reason=reason,
                before=before,
                after=after,
            )
            return after

    def _add_event(
        self,
        connection: sqlite3.Connection,
        *,
        assignment_id: int,
        action: str,
        actor: str,
        reason: str,
        before: dict[str, Any],
        after: dict[str, Any],
    ) -> None:
        connection.execute(
            "INSERT INTO patrol_assignment_events(assignment_id,action,actor,reason,before_json,after_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (
                assignment_id,
                action,
                actor,
                reason,
                json.dumps(before, ensure_ascii=False, sort_keys=True),
                json.dumps(after, ensure_ascii=False, sort_keys=True),
                to_storage(self.clock.now()),
            ),
        )

    @staticmethod
    def _zone_by_code(connection: sqlite3.Connection, code: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM patrol_zones WHERE code=?", (code,)).fetchone()
        if row is None:
            raise NotFoundError("生境不存在")
        return row

    @staticmethod
    def _ranger_by_code(connection: sqlite3.Connection, code: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM patrol_rangers WHERE code=?", (code,)).fetchone()
        if row is None:
            raise NotFoundError("巡护员不存在")
        return row

    def _window_view(self, connection: sqlite3.Connection, window_id: int) -> dict[str, Any]:
        row = connection.execute(
            "SELECT w.*, z.code AS zone_code, z.name AS zone_name FROM patrol_protection_windows w JOIN patrol_zones z ON z.id=w.zone_id WHERE w.id=?",
            (window_id,),
        ).fetchone()
        return self._window_dict(row)

    @staticmethod
    def _window_dict(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["kind_label"] = WINDOW_KIND_LABELS[data["kind"]]
        return data

    def _ranger_view(self, connection: sqlite3.Connection, ranger_id: int) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM patrol_rangers WHERE id=?", (ranger_id,)).fetchone()
        return self._ranger_dict(connection, row)

    @staticmethod
    def _ranger_dict(connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["skills"] = json.loads(data.pop("skills_json"))
        data["availability"] = [
            {"starts_at": item["starts_at"], "ends_at": item["ends_at"]}
            for item in connection.execute(
                "SELECT starts_at, ends_at FROM patrol_ranger_availability WHERE ranger_id=? ORDER BY starts_at",
                (row["id"],),
            ).fetchall()
        ]
        return data

    def _risk_view(self, connection: sqlite3.Connection, risk_id: int) -> dict[str, Any]:
        row = connection.execute(
            "SELECT r.*, z.code AS zone_code, z.name AS zone_name FROM patrol_risk_observations r JOIN patrol_zones z ON z.id=r.zone_id WHERE r.id=?",
            (risk_id,),
        ).fetchone()
        return dict(row)

    def _schedule_view(self, connection: sqlite3.Connection, schedule_id: int) -> dict[str, Any] | None:
        row = connection.execute("SELECT * FROM patrol_schedules WHERE id=?", (schedule_id,)).fetchone()
        if row is None:
            return None
        data = dict(row)
        rows = connection.execute(
            "SELECT id FROM patrol_assignments WHERE schedule_id=? ORDER BY start_at IS NULL, start_at, id",
            (schedule_id,),
        ).fetchall()
        data["assignments"] = [self._assignment_view(connection, item["id"]) for item in rows]
        return data

    def _assignment_view(self, connection: sqlite3.Connection, assignment_id: int) -> dict[str, Any] | None:
        row = connection.execute(
            "SELECT a.*, z.code AS zone_code, z.name AS zone_name, z.habitat_type, z.route_group, "
            "r.code AS ranger_code, r.name AS ranger_name "
            "FROM patrol_assignments a JOIN patrol_zones z ON z.id=a.zone_id "
            "LEFT JOIN patrol_rangers r ON r.id=a.ranger_id WHERE a.id=?",
            (assignment_id,),
        ).fetchone()
        if row is None:
            return None
        return self._assignment_dict(row)

    @staticmethod
    def _assignment_dict(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["rationale"] = json.loads(data.pop("rationale_json"))
        data["request"] = json.loads(data.pop("request_json"))
        return data

    def _disruption_view(self, connection: sqlite3.Connection, disruption_id: int) -> dict[str, Any]:
        row = connection.execute(
            "SELECT d.*, z.code AS zone_code, r.code AS ranger_code FROM patrol_disruptions d "
            "LEFT JOIN patrol_zones z ON z.id=d.zone_id LEFT JOIN patrol_rangers r ON r.id=d.ranger_id WHERE d.id=?",
            (disruption_id,),
        ).fetchone()
        data = dict(row)
        data["kind_label"] = DISRUPTION_KIND_LABELS[data["kind"]]
        data["affected"] = json.loads(data.pop("affected_json"))
        data["skipped"] = json.loads(data.pop("skipped_json"))
        return data
