from __future__ import annotations

import pytest

DAY = "2026-10-01"

ZONES = [
    {"code": "mudflat-north", "name": "滩涂北区", "habitat_type": "滩涂", "route_group": "north-loop", "visit_minutes": 60},
    {"code": "mudflat-south", "name": "滩涂南区", "habitat_type": "滩涂", "route_group": "north-loop", "visit_minutes": 60},
    {"code": "reed-west", "name": "芦苇荡西区", "habitat_type": "芦苇荡", "route_group": "west-loop", "visit_minutes": 45},
]

RANGERS = [
    {"code": "ranger-li", "name": "李岚", "skills": ["鸟类监测", "游客疏导"], "avail": ("06:00", "18:00")},
    {"code": "ranger-wang", "name": "王强", "skills": ["游客疏导"], "avail": ("06:00", "18:00")},
    {"code": "ranger-zhao", "name": "赵敏", "skills": ["鸟类监测"], "avail": ("12:00", "20:00")},
]


def register_base(client) -> None:
    for zone in ZONES:
        response = client.post("/api/patrol/zones?actor=admin", json=zone)
        assert response.status_code == 201, response.text
    for ranger in RANGERS:
        response = client.post("/api/patrol/rangers?actor=admin", json={"code": ranger["code"], "name": ranger["name"], "skills": ranger["skills"]})
        assert response.status_code == 201, response.text
        start, end = ranger["avail"]
        response = client.post(
            f"/api/patrol/rangers/{ranger['code']}/availability?actor=admin",
            json={"starts_at": f"{DAY}T{start}:00+00:00", "ends_at": f"{DAY}T{end}:00+00:00"},
        )
        assert response.status_code == 201, response.text
    window = {
        "zone_code": "mudflat-north",
        "kind": "bird_breeding",
        "starts_at": f"{DAY}T00:00:00+00:00",
        "ends_at": "2026-10-02T00:00:00+00:00",
        "required_skill": "鸟类监测",
        "min_interval_minutes": 120,
        "priority_boost": 20,
        "note": "繁殖期避免惊扰",
    }
    response = client.post("/api/patrol/protection-windows?actor=admin", json=window)
    assert response.status_code == 201, response.text
    risk = {"zone_code": "mudflat-north", "kind": "非法垂钓", "severity": 4, "note": "发现钓具"}
    response = client.post("/api/patrol/risk-observations?actor=admin", json=risk)
    assert response.status_code == 201, response.text


def main_batch() -> dict:
    return {
        "requested_by": "dispatcher",
        "batch_key": "batch-20261001-am",
        "title": "10月1日上午巡护",
        "requests": [
            {"zone_code": "mudflat-north", "window_start": f"{DAY}T08:00:00+00:00", "window_end": f"{DAY}T12:00:00+00:00"},
            {"zone_code": "mudflat-south", "window_start": f"{DAY}T08:00:00+00:00", "window_end": f"{DAY}T12:00:00+00:00"},
            {"zone_code": "reed-west", "window_start": f"{DAY}T08:00:00+00:00", "window_end": f"{DAY}T10:00:00+00:00"},
            {"zone_code": "mudflat-north", "window_start": f"{DAY}T09:30:00+00:00", "window_end": f"{DAY}T12:00:00+00:00"},
        ],
    }


def generate_main(client) -> dict:
    response = client.post("/api/patrol/schedules", json=main_batch())
    assert response.status_code == 201, response.text
    return response.json()


def by_request(assignments: list[dict], index: int) -> dict:
    return next(item for item in assignments if item["request_index"] == index)


def test_registration_and_explainable_schedule(client):
    register_base(client)
    schedule = generate_main(client)
    assert schedule["replayed"] is False
    assignments = schedule["assignments"]
    assert len(assignments) == 4
    assert all(item["status"] == "planned" for item in assignments)

    north_first = by_request(assignments, 0)
    assert north_first["ranger_code"] == "ranger-li"
    assert north_first["start_at"] == f"{DAY}T08:00:00+00:00"
    rationale_text = "；".join(north_first["rationale"])
    assert "鸟类繁殖期" in rationale_text
    assert "非法垂钓" in rationale_text
    assert "鸟类监测" in rationale_text
    assert north_first["priority"] > by_request(assignments, 1)["priority"]

    # 路线冲突：同一环线（north-loop）的两个区域不能同时段巡护
    south = by_request(assignments, 1)
    assert south["start_at"] == f"{DAY}T09:00:00+00:00"
    assert south["ranger_code"] == "ranger-wang"

    # 同区域最小巡护间隔 120 分钟：第二次滩涂北区巡护顺延到 11:00
    north_second = by_request(assignments, 3)
    assert north_second["start_at"] == f"{DAY}T11:00:00+00:00"

    west = by_request(assignments, 2)
    assert west["start_at"] == f"{DAY}T08:00:00+00:00"
    assert west["end_at"] == f"{DAY}T08:45:00+00:00"

    detail = client.get(f"/api/patrol/assignments/{north_first['id']}").json()
    assert detail["events"][0]["action"] == "created"
    assert detail["rationale"]


def test_schedule_generation_is_idempotent(client):
    register_base(client)
    first = generate_main(client)
    replay = client.post("/api/patrol/schedules", json=main_batch())
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert replay.json()["id"] == first["id"]
    assert len(replay.json()["assignments"]) == 4
    schedules = client.get("/api/patrol/schedules").json()["items"]
    assert len(schedules) == 1

    changed = main_batch()
    changed["requests"] = changed["requests"][:1]
    conflict = client.post("/api/patrol/schedules", json=changed)
    assert conflict.status_code == 409


def test_unassigned_when_constraints_unsatisfiable(client):
    register_base(client)
    batch = {
        "requested_by": "dispatcher",
        "batch_key": "batch-impossible",
        "requests": [
            {"zone_code": "reed-west", "window_start": f"{DAY}T08:00:00+00:00", "window_end": f"{DAY}T12:00:00+00:00", "required_skill": "潜水救援"},
            {"zone_code": "reed-west", "window_start": f"{DAY}T21:00:00+00:00", "window_end": f"{DAY}T23:00:00+00:00"},
        ],
    }
    schedule = client.post("/api/patrol/schedules", json=batch).json()
    skill_gap = by_request(schedule["assignments"], 0)
    assert skill_gap["status"] == "unassigned"
    assert any("缺少技能" in entry for entry in skill_gap["rationale"])
    off_hours = by_request(schedule["assignments"], 1)
    assert off_hours["status"] == "unassigned"
    assert any("可用时段不覆盖" in entry for entry in off_hours["rationale"])


def test_heavy_rain_reschedules_only_affected(client):
    register_base(client)
    schedule = generate_main(client)
    assignments = schedule["assignments"]
    rain = {
        "actor": "dispatcher",
        "disruption_key": "rain-20261001-01",
        "kind": "heavy_rain",
        "starts_at": f"{DAY}T07:00:00+00:00",
        "ends_at": f"{DAY}T08:30:00+00:00",
        "reason": "暴雨红色预警，暂停户外巡护",
    }
    response = client.post("/api/patrol/disruptions", json=rain)
    assert response.status_code == 201, response.text
    result = response.json()
    affected_ids = {item["assignment_id"] for item in result["affected"]}
    assert affected_ids == {by_request(assignments, 0)["id"], by_request(assignments, 2)["id"]}
    assert result["skipped"] == []

    after = client.get(f"/api/patrol/schedules/{schedule['id']}").json()
    assert len(after["assignments"]) == 6
    untouched = {item["id"]: item for item in after["assignments"] if item["id"] in {by_request(assignments, 1)["id"], by_request(assignments, 3)["id"]}}
    assert all(item["status"] == "planned" and item["version"] == 1 for item in untouched.values())

    old = client.get(f"/api/patrol/assignments/{by_request(assignments, 0)['id']}").json()
    assert old["status"] == "rescheduled"
    assert old["replaced_by_id"] is not None
    actions = [event["action"] for event in old["events"]]
    assert actions == ["created", "rescheduled"]
    assert "暴雨红色预警" in old["events"][-1]["reason"]

    replacement = client.get(f"/api/patrol/assignments/{old['replaced_by_id']}").json()
    assert replacement["status"] == "planned"
    assert replacement["origin_assignment_id"] == old["id"]
    assert replacement["start_at"] >= f"{DAY}T08:30:00+00:00"
    assert replacement["rationale"]

    replay = client.post("/api/patrol/disruptions", json=rain)
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert replay.json()["affected"] == result["affected"]
    assert len(client.get(f"/api/patrol/schedules/{schedule['id']}").json()["assignments"]) == 6


def test_ranger_unavailable_replans_to_backup(client):
    register_base(client)
    schedule = generate_main(client)
    assignments = schedule["assignments"]
    wang_ids = {item["id"] for item in assignments if item["ranger_code"] == "ranger-wang"}
    assert wang_ids
    leave = {
        "actor": "dispatcher",
        "disruption_key": "leave-wang-001",
        "kind": "ranger_unavailable",
        "ranger_code": "ranger-wang",
        "starts_at": f"{DAY}T00:00:00+00:00",
        "ends_at": "2026-10-02T00:00:00+00:00",
        "reason": "王强临时请假",
    }
    result = client.post("/api/patrol/disruptions", json=leave).json()
    assert {item["assignment_id"] for item in result["affected"]} == wang_ids

    planned = client.get(f"/api/patrol/assignments?status=planned&day={DAY}").json()["items"]
    assert planned
    assert all(item["ranger_code"] != "ranger-wang" for item in planned)
    for old_id in wang_ids:
        old = client.get(f"/api/patrol/assignments/{old_id}").json()
        assert old["status"] == "rescheduled"
        assert "王强临时请假" in old["events"][-1]["reason"]
        replacement = client.get(f"/api/patrol/assignments/{old['replaced_by_id']}").json()
        assert replacement["status"] == "planned"
        assert replacement["ranger_code"] != "ranger-wang"


def test_started_patrol_is_never_silently_rewritten(client):
    register_base(client)
    schedule = generate_main(client)
    target = by_request(schedule["assignments"], 3)
    started = client.post(f"/api/patrol/assignments/{target['id']}/start", json={"actor": "ranger-li"})
    assert started.status_code == 200, started.text
    assert started.json()["status"] == "in_progress"

    closure = {
        "actor": "dispatcher",
        "disruption_key": "close-north-001",
        "kind": "zone_closed",
        "zone_code": "mudflat-north",
        "starts_at": f"{DAY}T10:30:00+00:00",
        "ends_at": f"{DAY}T11:30:00+00:00",
        "reason": "滩涂积水封闭",
    }
    result = client.post("/api/patrol/disruptions", json=closure).json()
    assert result["affected"] == []
    assert [item["assignment_id"] for item in result["skipped"]] == [target["id"]]
    current = client.get(f"/api/patrol/assignments/{target['id']}").json()
    assert current["status"] == "in_progress"
    assert current["ranger_code"] == "ranger-li"

    reassign = client.post(
        f"/api/patrol/assignments/{target['id']}/reassign",
        json={"actor": "dispatcher", "reason": "试图改派", "ranger_code": "ranger-wang", "start_at": f"{DAY}T13:00:00+00:00"},
    )
    assert reassign.status_code == 409
    cancel = client.post(f"/api/patrol/assignments/{target['id']}/cancel", json={"actor": "dispatcher", "reason": "试图取消"})
    assert cancel.status_code == 409

    completed = client.post(f"/api/patrol/assignments/{target['id']}/complete", json={"actor": "ranger-li", "expected_version": current["version"]})
    assert completed.status_code == 200
    assert completed.json()["status"] == "completed"
    again = client.post(
        f"/api/patrol/assignments/{target['id']}/reassign",
        json={"actor": "dispatcher", "reason": "事后改写", "ranger_code": "ranger-wang", "start_at": f"{DAY}T14:00:00+00:00"},
    )
    assert again.status_code == 409
    actions = [event["action"] for event in client.get(f"/api/patrol/assignments/{target['id']}").json()["events"]]
    assert actions == ["created", "started", "completed"]


def test_manual_reassign_validates_constraints_and_version(client):
    register_base(client)
    generate_main(client)
    batch = {
        "requested_by": "dispatcher",
        "batch_key": "batch-late-night",
        "requests": [{"zone_code": "reed-west", "window_start": f"{DAY}T21:00:00+00:00", "window_end": f"{DAY}T23:00:00+00:00"}],
    }
    schedule = client.post("/api/patrol/schedules", json=batch).json()
    unassigned = schedule["assignments"][0]
    assert unassigned["status"] == "unassigned"

    conflict = client.post(
        f"/api/patrol/assignments/{unassigned['id']}/reassign",
        json={"actor": "dispatcher", "reason": "补充人手", "ranger_code": "ranger-li", "start_at": f"{DAY}T08:30:00+00:00"},
    )
    assert conflict.status_code == 422
    assert conflict.json()["error"]["context"]["reasons"]

    moved = client.post(
        f"/api/patrol/assignments/{unassigned['id']}/reassign",
        json={"actor": "dispatcher", "reason": "补充人手", "ranger_code": "ranger-li", "start_at": f"{DAY}T15:00:00+00:00"},
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["status"] == "planned"
    assert moved.json()["ranger_code"] == "ranger-li"
    assert any("人工改派" in entry for entry in moved.json()["rationale"])

    stale = client.post(
        f"/api/patrol/assignments/{unassigned['id']}/reassign",
        json={"actor": "dispatcher", "reason": "并发修改", "ranger_code": "ranger-wang", "start_at": f"{DAY}T16:00:00+00:00", "expected_version": 1},
    )
    assert stale.status_code == 409


def test_cancel_and_change_history(client):
    register_base(client)
    schedule = generate_main(client)
    target = by_request(schedule["assignments"], 2)
    cancelled = client.post(
        f"/api/patrol/assignments/{target['id']}/cancel",
        json={"actor": "dispatcher", "reason": "芦苇荡水位上涨"},
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    detail = client.get(f"/api/patrol/assignments/{target['id']}").json()
    assert [event["action"] for event in detail["events"]] == ["created", "cancelled"]
    assert detail["events"][-1]["reason"] == "芦苇荡水位上涨"
    assert detail["events"][-1]["before"]["status"] == "planned"
    assert detail["events"][-1]["after"]["status"] == "cancelled"


def test_summary_filters_and_risk_close(client):
    register_base(client)
    schedule = generate_main(client)
    summary = client.get("/api/patrol/summary").json()
    assert summary["assignments_by_status"] == {"planned": 4}
    assert summary["open_risks"] == 1
    assert summary["active_zones"] == 3
    assert summary["active_rangers"] == 3

    by_zone = client.get("/api/patrol/assignments?zone_code=mudflat-north").json()["items"]
    assert len(by_zone) == 2
    by_ranger = client.get("/api/patrol/assignments?ranger_code=ranger-wang").json()["items"]
    assert len(by_ranger) == 2
    none_next_day = client.get("/api/patrol/assignments?day=2026-10-02").json()["items"]
    assert none_next_day == []

    risk_id = client.get("/api/patrol/risk-observations?status=open").json()["items"][0]["id"]
    closed = client.post(f"/api/patrol/risk-observations/{risk_id}/close?actor=admin")
    assert closed.status_code == 200
    assert closed.json()["status"] == "closed"
    assert client.get("/api/patrol/summary").json()["open_risks"] == 0

    disruptions = client.get("/api/patrol/disruptions").json()["items"]
    assert disruptions == []
    schedules = client.get("/api/patrol/schedules").json()["items"]
    assert schedules[0]["assignment_count"] == 4


def test_registration_and_request_validation(client):
    register_base(client)
    duplicate = client.post("/api/patrol/zones?actor=admin", json=ZONES[0])
    assert duplicate.status_code == 409

    bad_window = {
        "zone_code": "reed-west",
        "kind": "visitor_peak",
        "starts_at": f"{DAY}T10:00:00+00:00",
        "ends_at": f"{DAY}T09:00:00+00:00",
    }
    assert client.post("/api/patrol/protection-windows?actor=admin", json=bad_window).status_code == 422

    bad_disruption = {
        "actor": "dispatcher",
        "disruption_key": "bad-000001",
        "kind": "zone_closed",
        "starts_at": f"{DAY}T10:00:00+00:00",
        "ends_at": f"{DAY}T11:00:00+00:00",
        "reason": "缺少区域",
    }
    assert client.post("/api/patrol/disruptions", json=bad_disruption).status_code == 422

    unknown_zone = {
        "requested_by": "dispatcher",
        "batch_key": "batch-unknown-zone",
        "requests": [{"zone_code": "nowhere", "window_start": f"{DAY}T08:00:00+00:00", "window_end": f"{DAY}T10:00:00+00:00"}],
    }
    assert client.post("/api/patrol/schedules", json=unknown_zone).status_code == 422

    too_short = {
        "requested_by": "dispatcher",
        "batch_key": "batch-too-short",
        "requests": [{"zone_code": "mudflat-north", "window_start": f"{DAY}T08:00:00+00:00", "window_end": f"{DAY}T08:30:00+00:00"}],
    }
    assert client.post("/api/patrol/schedules", json=too_short).status_code == 422

    missing = client.get("/api/patrol/assignments/99999")
    assert missing.status_code == 404
