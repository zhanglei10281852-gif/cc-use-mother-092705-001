from __future__ import annotations

import json
import sqlite3
from typing import Any

ASSIGNMENT_SELECT = (
    "SELECT a.*, z.code AS zone_code, z.name AS zone_name, "
    "r.code AS ranger_code, r.name AS ranger_name "
    "FROM patrol_assignments a "
    "JOIN patrol_zones z ON z.id = a.zone_id "
    "LEFT JOIN patrol_rangers r ON r.id = a.ranger_id"
)


class PatrolRepository:
    """封装湿地巡护排班领域的 SQLite 读写。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    # 生境与保护窗口
    def zone_by_code(self, code: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM patrol_zones WHERE code=?", (code,)).fetchone()

    def zone_by_id(self, zone_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM patrol_zones WHERE id=?", (zone_id,)).fetchone()

    def list_zones(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute("SELECT * FROM patrol_zones ORDER BY code").fetchall()]

    def create_zone(self, *, code: str, name: str, habitat_type: str, description: str, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO patrol_zones(code,name,habitat_type,description,active,created_at,updated_at) VALUES(?,?,?,?,1,?,?)",
            (code, name, habitat_type, description, now, now),
        )
        return dict(self.zone_by_id(cursor.lastrowid))

    def add_window(self, *, zone_id: int, kind: str, rule: str, starts_at: str, ends_at: str, note: str, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO patrol_zone_windows(zone_id,kind,rule,starts_at,ends_at,note,created_at) VALUES(?,?,?,?,?,?,?)",
            (zone_id, kind, rule, starts_at, ends_at, note, now),
        )
        return dict(self.connection.execute("SELECT * FROM patrol_zone_windows WHERE id=?", (cursor.lastrowid,)).fetchone())

    def windows_for_zone(self, zone_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM patrol_zone_windows WHERE zone_id=? ORDER BY starts_at,id", (zone_id,)).fetchall()
        return [dict(row) for row in rows]

    def all_windows(self) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM patrol_zone_windows ORDER BY zone_id,starts_at,id").fetchall()
        return [dict(row) for row in rows]

    # 巡护人员与可用时段
    def ranger_by_code(self, code: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM patrol_rangers WHERE code=?", (code,)).fetchone()

    def ranger_by_id(self, ranger_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM patrol_rangers WHERE id=?", (ranger_id,)).fetchone()

    def list_rangers(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute("SELECT * FROM patrol_rangers ORDER BY code").fetchall()]

    def create_ranger(self, *, code: str, name: str, skills: list[str], now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO patrol_rangers(code,name,skills_json,active,created_at,updated_at) VALUES(?,?,?,1,?,?)",
            (code, name, json.dumps(skills, ensure_ascii=False), now, now),
        )
        return dict(self.ranger_by_id(cursor.lastrowid))

    def add_availability(self, *, ranger_id: int, starts_at: str, ends_at: str, note: str, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO patrol_ranger_availability(ranger_id,starts_at,ends_at,note,created_at) VALUES(?,?,?,?,?)",
            (ranger_id, starts_at, ends_at, note, now),
        )
        return dict(self.connection.execute("SELECT * FROM patrol_ranger_availability WHERE id=?", (cursor.lastrowid,)).fetchone())

    def availability_for_ranger(self, ranger_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM patrol_ranger_availability WHERE ranger_id=? ORDER BY starts_at,id", (ranger_id,)).fetchall()
        return [dict(row) for row in rows]

    def all_availability(self) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM patrol_ranger_availability ORDER BY ranger_id,starts_at,id").fetchall()
        return [dict(row) for row in rows]

    # 风险观察
    def observation_by_natural_key(self, zone_id: int, kind: str, starts_at: str, ends_at: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM patrol_risk_observations WHERE zone_id=? AND kind=? AND starts_at=? AND ends_at=?",
            (zone_id, kind, starts_at, ends_at),
        ).fetchone()

    def create_observation(self, *, zone_id: int, kind: str, effect: str, severity: str, starts_at: str, ends_at: str, note: str, reported_by: str, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO patrol_risk_observations(zone_id,kind,effect,severity,starts_at,ends_at,note,reported_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (zone_id, kind, effect, severity, starts_at, ends_at, note, reported_by, now),
        )
        return dict(self.connection.execute("SELECT * FROM patrol_risk_observations WHERE id=?", (cursor.lastrowid,)).fetchone())

    def list_observations(self, zone_id: int | None = None) -> list[dict[str, Any]]:
        if zone_id is None:
            rows = self.connection.execute("SELECT * FROM patrol_risk_observations ORDER BY starts_at,id").fetchall()
        else:
            rows = self.connection.execute("SELECT * FROM patrol_risk_observations WHERE zone_id=? ORDER BY starts_at,id", (zone_id,)).fetchall()
        return [dict(row) for row in rows]

    # 排班批次与巡护要求
    def run_by_batch_key(self, batch_key: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM patrol_runs WHERE batch_key=?", (batch_key,)).fetchone()

    def run_by_id(self, run_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM patrol_runs WHERE id=?", (run_id,)).fetchone()

    def list_runs(self, limit: int) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM patrol_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def create_run(self, *, batch_key: str, request_digest: str, requested_by: str, request_count: int, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO patrol_runs(batch_key,request_digest,requested_by,status,request_count,scheduled_count,unscheduled_count,created_at) VALUES(?,?,?,'active',?,0,0,?)",
            (batch_key, request_digest, requested_by, request_count, now),
        )
        return dict(self.run_by_id(cursor.lastrowid))

    def set_run_counts(self, run_id: int, scheduled: int, unscheduled: int) -> None:
        self.connection.execute(
            "UPDATE patrol_runs SET scheduled_count=?,unscheduled_count=? WHERE id=?",
            (scheduled, unscheduled, run_id),
        )

    def create_request(self, *, run_id: int, request_key: str, zone_id: int, required_skill: str, earliest_start: str, latest_end: str, duration_minutes: int, priority: int, note: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO patrol_requests(run_id,request_key,zone_id,required_skill,earliest_start,latest_end,duration_minutes,priority,note,status) VALUES(?,?,?,?,?,?,?,?,?,'pending')",
            (run_id, request_key, zone_id, required_skill, earliest_start, latest_end, duration_minutes, priority, note),
        )
        return dict(self.request_by_id(cursor.lastrowid))

    def request_by_id(self, request_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM patrol_requests WHERE id=?", (request_id,)).fetchone()

    def requests_for_run(self, run_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT q.*, z.code AS zone_code, z.name AS zone_name FROM patrol_requests q JOIN patrol_zones z ON z.id=q.zone_id WHERE q.run_id=? ORDER BY q.id",
            (run_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def update_request_status(self, request_id: int, status: str) -> None:
        self.connection.execute("UPDATE patrol_requests SET status=? WHERE id=?", (status, request_id))

    # 巡护分派
    def assignment_by_id(self, assignment_id: int) -> sqlite3.Row | None:
        return self.connection.execute(ASSIGNMENT_SELECT + " WHERE a.id=?", (assignment_id,)).fetchone()

    def create_assignment(self, *, run_id: int, request_id: int, zone_id: int, ranger_id: int | None, planned_start: str | None, planned_end: str | None, status: str, rationale: dict[str, Any], replaces_id: int | None, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO patrol_assignments(run_id,request_id,zone_id,ranger_id,planned_start,planned_end,status,rationale_json,replaces_id,version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,1,?,?)",
            (run_id, request_id, zone_id, ranger_id, planned_start, planned_end, status, json.dumps(rationale, ensure_ascii=False, sort_keys=True), replaces_id, now, now),
        )
        return dict(self.assignment_by_id(cursor.lastrowid))

    def assignments_for_run(self, run_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(ASSIGNMENT_SELECT + " WHERE a.run_id=? ORDER BY a.id", (run_id,)).fetchall()
        return [dict(row) for row in rows]

    def list_assignments(self, *, status: str | None, zone_id: int | None, ranger_id: int | None, run_id: int | None, limit: int) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if status:
            clauses.append("a.status=?")
            values.append(status)
        if zone_id is not None:
            clauses.append("a.zone_id=?")
            values.append(zone_id)
        if ranger_id is not None:
            clauses.append("a.ranger_id=?")
            values.append(ranger_id)
        if run_id is not None:
            clauses.append("a.run_id=?")
            values.append(run_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        values.append(limit)
        rows = self.connection.execute(ASSIGNMENT_SELECT + where + " ORDER BY a.planned_start,a.id LIMIT ?", values).fetchall()
        return [dict(row) for row in rows]

    def current_assignments(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            ASSIGNMENT_SELECT + " WHERE a.status IN ('planned','in_progress') AND a.planned_start IS NOT NULL ORDER BY a.planned_start,a.id"
        ).fetchall()
        return [dict(row) for row in rows]

    def successor_of(self, assignment_id: int) -> sqlite3.Row | None:
        return self.connection.execute(ASSIGNMENT_SELECT + " WHERE a.replaces_id=? ORDER BY a.id DESC LIMIT 1", (assignment_id,)).fetchone()

    # 扰动事件
    def disruption_by_key(self, idempotency_key: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM patrol_disruptions WHERE idempotency_key=?", (idempotency_key,)).fetchone()

    def disruption_by_id(self, disruption_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM patrol_disruptions WHERE id=?", (disruption_id,)).fetchone()

    def create_disruption(self, *, idempotency_key: str, kind: str, zone_id: int | None, ranger_id: int | None, starts_at: str, ends_at: str, reason: str, created_by: str, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO patrol_disruptions(idempotency_key,kind,zone_id,ranger_id,starts_at,ends_at,reason,status,created_by,created_at) VALUES(?,?,?,?,?,?,?,'open',?,?)",
            (idempotency_key, kind, zone_id, ranger_id, starts_at, ends_at, reason, created_by, now),
        )
        return dict(self.disruption_by_id(cursor.lastrowid))

    def set_disruption_report(self, disruption_id: int, report: dict[str, Any]) -> None:
        self.connection.execute(
            "UPDATE patrol_disruptions SET report_json=? WHERE id=?",
            (json.dumps(report, ensure_ascii=False, sort_keys=True), disruption_id),
        )

    def list_disruptions(self) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM patrol_disruptions ORDER BY id DESC").fetchall()
        return [dict(row) for row in rows]

    def open_disruptions(self) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM patrol_disruptions WHERE status='open' ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    # 变更历史
    def add_event(self, *, run_id: int | None, assignment_id: int | None, disruption_id: int | None, action: str, actor: str, reason: str, before: dict[str, Any], after: dict[str, Any], now: str) -> None:
        self.connection.execute(
            "INSERT INTO patrol_events(run_id,assignment_id,disruption_id,action,actor,reason,before_json,after_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                assignment_id,
                disruption_id,
                action,
                actor,
                reason,
                json.dumps(before, ensure_ascii=False, sort_keys=True),
                json.dumps(after, ensure_ascii=False, sort_keys=True),
                now,
            ),
        )

    def events_for_assignment(self, assignment_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM patrol_events WHERE assignment_id=? ORDER BY id", (assignment_id,)).fetchall()
        return [dict(row) for row in rows]

    def events_for_run(self, run_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM patrol_events WHERE run_id=? ORDER BY id", (run_id,)).fetchall()
        return [dict(row) for row in rows]
