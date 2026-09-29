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
        zone = client.post("/api/patrol/zones", json={
            "code": "reed-north",
            "name": "芦苇荡北区",
            "habitat_type": "芦苇荡",
            "protection_windows": [
                {"kind": "visitor_peak", "rule": "prefer", "starts_at": f"{day}T09:00:00+00:00", "ends_at": f"{day}T11:00:00+00:00", "note": "游客密集时段"},
            ],
        })
        if zone.status_code not in {201, 409}:
            print(zone.text)
            return 1
        ranger = client.post("/api/patrol/rangers", json={
            "code": "ranger-a",
            "name": "张三",
            "skills": ["鸟类观测"],
            "availability": [{"starts_at": f"{day}T06:00:00+00:00", "ends_at": f"{day}T18:00:00+00:00"}],
        })
        if ranger.status_code not in {201, 409}:
            print(ranger.text)
            return 1
        run = client.post("/api/patrol/runs", json={
            "batch_key": "patrol-demo-000001",
            "requested_by": "cli-demo",
            "requests": [{
                "request_key": "req-bird",
                "zone_code": "reed-north",
                "required_skill": "鸟类观测",
                "earliest_start": f"{day}T06:00:00+00:00",
                "latest_end": f"{day}T18:00:00+00:00",
                "duration_minutes": 120,
                "priority": 80,
            }],
        })
        if run.status_code != 201:
            print(run.text)
            return 1
        detail = client.get(f"/api/patrol/runs/{run.json()['id']}")
    planned = [item for item in detail.json()["assignments"] if item["status"] == "planned"]
    result = {"run": run.status_code, "run_id": run.json()["id"], "planned": len(planned)}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if planned else 1


def main() -> int:
    parser = argparse.ArgumentParser(prog="compute-operations", description="科学计算任务运营服务维护入口")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init-db", help="初始化 SQLite 数据库")
    subparsers.add_parser("check-db", help="检查数据库完整性")
    subparsers.add_parser("smoke", help="执行本地 API 冒烟检查")
    subparsers.add_parser("compute-demo", help="执行计算任务提交与领取演示")
    subparsers.add_parser("patrol-demo", help="执行湿地巡护排班演示")
    args = parser.parse_args()
    return {
        "init-db": command_init,
        "check-db": command_check,
        "smoke": command_smoke,
        "compute-demo": command_compute_demo,
        "patrol-demo": command_patrol_demo,
    }[args.command]()


if __name__ == "__main__":
    raise SystemExit(main())
