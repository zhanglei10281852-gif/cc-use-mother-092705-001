from __future__ import annotations

import argparse
import json

from fastapi.testclient import TestClient

from app.database import database_path, get_connection, init_db
from app.main import app


def command_init() -> int:
    init_db()
    print(json.dumps({"database": str(database_path()), "status": "initialized"}, ensure_ascii=False))
    return 0


def command_check() -> int:
    init_db()
    connection = get_connection()
    result = {
        "database": str(database_path()),
        "integrity": connection.execute("PRAGMA integrity_check").fetchone()[0],
        "foreign_keys": connection.execute("PRAGMA foreign_keys").fetchone()[0],
        "journal_mode": connection.execute("PRAGMA journal_mode").fetchone()[0],
        "tables": connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0],
    }
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["integrity"] == "ok" and result["foreign_keys"] == 1 else 1


def command_smoke() -> int:
    with TestClient(app) as client:
        root = client.get("/")
        health = client.get("/api/system/health")
    result = {"root": root.json(), "health": health.json(), "status_codes": [root.status_code, health.status_code]}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status_codes"] == [200, 200] else 1


def command_compute_demo() -> int:
    template = {
        "code": "monte-carlo-demo",
        "name": "蒙特卡洛演示",
        "algorithm": "monte-carlo",
        "parameter_schema": {
            "samples": {"type": "integer", "required": True, "minimum": 10, "maximum": 1000000},
            "seed": {"type": "integer", "required": True},
        },
        "default_parameters": {},
        "max_runtime_seconds": 60,
        "max_attempts": 3,
    }
    with TestClient(app) as client:
        created = client.post("/api/compute/templates?actor=cli-demo", json=template)
        if created.status_code not in {201, 409}:
            print(created.text)
            return 1
        task = client.post(
            "/api/compute/tasks",
            json={
                "template_code": "monte-carlo-demo",
                "project_code": "demo",
                "requested_by": "cli-user",
                "parameters": {"samples": 1000, "seed": 42},
                "priority": 80,
                "idempotency_key": "compute-demo-000001",
            },
        )
        claimed = client.post(
            "/api/compute/tasks/claim",
            json={"worker_id": "cli-worker", "capabilities": ["monte-carlo"], "lease_seconds": 60},
        )
    result = {"task": task.status_code, "claimed": claimed.status_code, "task_id": task.json().get("id")}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if task.status_code == 202 and claimed.status_code == 200 and claimed.json().get("task") else 1


def command_patrol_demo() -> int:
    day = "2026-10-01"
    with TestClient(app) as client:
        zone = client.post(
            "/api/patrol/zones?actor=cli-demo",
            json={"code": "mudflat-north", "name": "滩涂北区", "habitat_type": "滩涂", "route_group": "north-loop", "visit_minutes": 60},
        )
        if zone.status_code not in {201, 409}:
            print(zone.text)
            return 1
        ranger = client.post(
            "/api/patrol/rangers?actor=cli-demo",
            json={"code": "ranger-li", "name": "李岚", "skills": ["鸟类监测"]},
        )
        if ranger.status_code not in {201, 409}:
            print(ranger.text)
            return 1
        client.post(
            "/api/patrol/rangers/ranger-li/availability?actor=cli-demo",
            json={"starts_at": f"{day}T06:00:00+00:00", "ends_at": f"{day}T18:00:00+00:00"},
        )
        client.post(
            "/api/patrol/protection-windows?actor=cli-demo",
            json={
                "zone_code": "mudflat-north",
                "kind": "bird_breeding",
                "starts_at": f"{day}T00:00:00+00:00",
                "ends_at": f"{day}T23:59:00+00:00",
                "required_skill": "鸟类监测",
                "priority_boost": 20,
            },
        )
        schedule = client.post(
            "/api/patrol/schedules",
            json={
                "requested_by": "cli-demo",
                "batch_key": "cli-demo-batch-0001",
                "title": "演示巡护批次",
                "requests": [{"zone_code": "mudflat-north", "window_start": f"{day}T08:00:00+00:00", "window_end": f"{day}T12:00:00+00:00"}],
            },
        )
        if schedule.status_code not in {200, 201}:
            print(schedule.text)
            return 1
        assignment = schedule.json()["assignments"][0]
        detail = client.get(f"/api/patrol/assignments/{assignment['id']}")
    result = {"schedule_status": schedule.status_code, "assignment": assignment["status"], "rationale": detail.json()["rationale"]}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if assignment["status"] == "planned" and result["rationale"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(prog="compute-operations", description="科学计算任务运营服务维护入口")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init-db", help="初始化 SQLite 数据库")
    subparsers.add_parser("check-db", help="检查数据库完整性")
    subparsers.add_parser("smoke", help="执行本地 API 冒烟检查")
    subparsers.add_parser("compute-demo", help="执行计算任务提交与领取演示")
    subparsers.add_parser("patrol-demo", help="执行湿地巡护排班演示")
    args = parser.parse_args()
    commands = {
        "init-db": command_init,
        "check-db": command_check,
        "smoke": command_smoke,
        "compute-demo": command_compute_demo,
        "patrol-demo": command_patrol_demo,
    }
    return commands[args.command]()


if __name__ == "__main__":
    raise SystemExit(main())
