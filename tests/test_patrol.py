from __future__ import annotations

DAY1 = "2026-10-01"
DAY2 = "2026-10-02"


def register_sample(client) -> None:
    """登记生境、保护窗口、风险观察与巡护人员。"""
    reed = client.post("/api/patrol/zones", json={
        "code": "reed-north",
        "name": "芦苇荡北区",
        "habitat_type": "芦苇荡",
        "protection_windows": [
            {"kind": "visitor_peak", "rule": "prefer", "starts_at": f"{DAY1}T09:00:00+00:00", "ends_at": f"{DAY1}T11:00:00+00:00", "note": "游客密集时段"},
        ],
    })
    assert reed.status_code == 201, reed.text
    marsh = client.post("/api/patrol/zones", json={
        "code": "marsh-east",
        "name": "浅滩东区",
        "habitat_type": "滩涂",
        "protection_windows": [
            {"kind": "鸟类繁殖期", "rule": "avoid", "starts_at": "2026-09-28T00:00:00+00:00", "ends_at": "2026-10-03T00:00:00+00:00", "note": "繁殖期禁止进入"},
        ],
    })
    assert marsh.status_code == 201, marsh.text
    mudflat = client.post("/api/patrol/zones", json={"code": "mudflat-west", "name": "滩涂西区", "habitat_type": "滩涂"})
    assert mudflat.status_code == 201, mudflat.text
    water = client.post("/api/patrol/observations", json={
        "zone_code": "mudflat-west",
        "kind": "waterlogging",
        "starts_at": f"{DAY1}T00:00:00+00:00",
        "ends_at": f"{DAY2}T12:00:00+00:00",
        "note": "滩涂积水，通行困难",
        "reported_by": "ranger-lead",
    })
    assert water.status_code == 201, water.text
    rangers = [
        {"code": "ranger-a", "name": "张三", "skills": ["鸟类观测", "游客疏导"]},
        {"code": "ranger-b", "name": "李四", "skills": ["水域巡查"]},
        {"code": "ranger-c", "name": "王五", "skills": ["鸟类观测"]},
    ]
    for ranger in rangers:
        ranger["availability"] = [
            {"starts_at": f"{DAY1}T06:00:00+00:00", "ends_at": f"{DAY1}T18:00:00+00:00"},
            {"starts_at": f"{DAY2}T06:00:00+00:00", "ends_at": f"{DAY2}T18:00:00+00:00"},
        ]
        response = client.post("/api/patrol/rangers", json=ranger)
        assert response.status_code == 201, response.text


def submit_batch(client, batch_key: str = "batch-2026-10-01"):
    payload = {
        "batch_key": batch_key,
        "requested_by": "scheduler-li",
        "requests": [
            {
                "request_key": "req-bird",
                "zone_code": "reed-north",
                "required_skill": "鸟类观测",
                "earliest_start": f"{DAY1}T06:00:00+00:00",
                "latest_end": f"{DAY1}T18:00:00+00:00",
                "duration_minutes": 120,
                "priority": 80,
            },
            {
                "request_key": "req-visitor",
                "zone_code": "reed-north",
                "required_skill": "游客疏导",
                "earliest_start": f"{DAY1}T06:00:00+00:00",
                "latest_end": f"{DAY1}T18:00:00+00:00",
                "duration_minutes": 60,
                "priority": 60,
            },
            {
                "request_key": "req-breeding",
                "zone_code": "marsh-east",
                "required_skill": "鸟类观测",
                "earliest_start": f"{DAY1}T06:00:00+00:00",
                "latest_end": f"{DAY1}T18:00:00+00:00",
                "duration_minutes": 60,
                "priority": 70,
            },
            {
                "request_key": "req-water",
                "zone_code": "mudflat-west",
                "required_skill": "水域巡查",
                "earliest_start": f"{DAY1}T06:00:00+00:00",
                "latest_end": f"{DAY2}T18:00:00+00:00",
                "duration_minutes": 60,
                "priority": 50,
            },
        ],
    }
    response = client.post("/api/patrol/runs", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def current_assignments(run: dict) -> dict[str, dict]:
    """按 request_key 取当前有效的分派。"""
    superseded = {a["replaces_id"] for a in run["assignments"] if a["replaces_id"]}
    by_request = {r["id"]: r["request_key"] for r in run["requests"]}
    return {
        by_request[a["request_id"]]: a
        for a in run["assignments"]
        if a["id"] not in superseded
    }


def overlaps(a_start: str, a_end: str, b_start: str, b_end: str) -> bool:
    return a_start < b_end and b_start < a_end


def test_registration_and_idempotent_observation(client):
    register_sample(client)
    zones = client.get("/api/patrol/zones").json()["items"]
    assert [zone["code"] for zone in zones] == ["marsh-east", "mudflat-west", "reed-north"]
    marsh = next(zone for zone in zones if zone["code"] == "marsh-east")
    assert marsh["protection_windows"][0]["rule"] == "avoid"
    duplicate = client.post("/api/patrol/observations", json={
        "zone_code": "mudflat-west",
        "kind": "waterlogging",
        "starts_at": f"{DAY1}T00:00:00+00:00",
        "ends_at": f"{DAY2}T12:00:00+00:00",
        "note": "重复上报",
        "reported_by": "ranger-lead",
    })
    assert duplicate.status_code == 201
    assert duplicate.json()["replayed"] is True
    observations = client.get("/api/patrol/observations?zone_code=mudflat-west").json()["items"]
    assert len(observations) == 1
    rangers = client.get("/api/patrol/rangers").json()["items"]
    assert {ranger["code"] for ranger in rangers} == {"ranger-a", "ranger-b", "ranger-c"}
    conflict = client.post("/api/patrol/zones", json={"code": "reed-north", "name": "重复生境", "habitat_type": "芦苇荡"})
    assert conflict.status_code == 409


def test_schedule_is_explainable_and_respects_constraints(client):
    register_sample(client)
    run = submit_batch(client)
    assert run["request_count"] == 4
    current = current_assignments(run)

    bird = current["req-bird"]
    assert bird["status"] == "planned"
    assert bird["ranger_code"] in {"ranger-a", "ranger-c"}
    # 优先覆盖游客密集窗口 09:00-11:00
    assert overlaps(bird["planned_start"], bird["planned_end"], f"{DAY1}T09:00:00+00:00", f"{DAY1}T11:00:00+00:00")
    rules = {check["rule"] for check in bird["rationale"]["checks"]}
    assert {"protection_window", "risk_observation", "route_conflict", "ranger_skill", "ranger_availability", "ranger_double_booking", "disruption"} <= rules
    assert all(check["result"] == "pass" for check in bird["rationale"]["checks"])
    assert bird["rationale"]["decision"]["ranger"]["code"] == bird["ranger_code"]

    visitor = current["req-visitor"]
    assert visitor["status"] == "planned"
    # 路线冲突：同一区域的分派时段不能重叠
    assert not overlaps(bird["planned_start"], bird["planned_end"], visitor["planned_start"], visitor["planned_end"])

    # 繁殖期保护窗口内无法安排，必须给出可解释的失败原因
    breeding = current["req-breeding"]
    assert breeding["status"] == "unscheduled"
    assert breeding["ranger_code"] is None
    assert any("保护窗口" in reason for reason in breeding["rationale"]["failure_reasons"])

    # 滩涂积水期间不安排，积水结束后才进入
    water = current["req-water"]
    assert water["status"] == "planned"
    assert water["ranger_code"] == "ranger-b"
    assert not overlaps(water["planned_start"], water["planned_end"], f"{DAY1}T00:00:00+00:00", f"{DAY2}T12:00:00+00:00")


def test_duplicate_batch_does_not_create_second_schedule(client):
    register_sample(client)
    first = submit_batch(client)
    second = submit_batch(client)
    assert second["id"] == first["id"]
    assert second["replayed"] is True
    assert len(second["assignments"]) == len(first["assignments"])
    runs = client.get("/api/patrol/runs").json()["items"]
    assert len(runs) == 1
    changed = client.post("/api/patrol/runs", json={
        "batch_key": "batch-2026-10-01",
        "requested_by": "scheduler-li",
        "requests": [{
            "request_key": "req-other",
            "zone_code": "reed-north",
            "required_skill": "鸟类观测",
            "earliest_start": f"{DAY1}T06:00:00+00:00",
            "latest_end": f"{DAY1}T12:00:00+00:00",
            "duration_minutes": 60,
        }],
    })
    assert changed.status_code == 409


def test_run_validation_errors(client):
    register_sample(client)
    missing_zone = client.post("/api/patrol/runs", json={
        "batch_key": "batch-unknown-zone",
        "requested_by": "scheduler-li",
        "requests": [{
            "request_key": "req-x",
            "zone_code": "nowhere",
            "required_skill": "鸟类观测",
            "earliest_start": f"{DAY1}T06:00:00+00:00",
            "latest_end": f"{DAY1}T12:00:00+00:00",
            "duration_minutes": 60,
        }],
    })
    assert missing_zone.status_code == 404
    too_long = client.post("/api/patrol/runs", json={
        "batch_key": "batch-too-long",
        "requested_by": "scheduler-li",
        "requests": [{
            "request_key": "req-x",
            "zone_code": "reed-north",
            "required_skill": "鸟类观测",
            "earliest_start": f"{DAY1}T06:00:00+00:00",
            "latest_end": f"{DAY1}T07:00:00+00:00",
            "duration_minutes": 120,
        }],
    })
    assert too_long.status_code == 422
    duplicated_keys = client.post("/api/patrol/runs", json={
        "batch_key": "batch-dup-keys",
        "requested_by": "scheduler-li",
        "requests": [
            {"request_key": "req-x", "zone_code": "reed-north", "required_skill": "鸟类观测",
             "earliest_start": f"{DAY1}T06:00:00+00:00", "latest_end": f"{DAY1}T12:00:00+00:00", "duration_minutes": 60},
            {"request_key": "req-x", "zone_code": "reed-north", "required_skill": "鸟类观测",
             "earliest_start": f"{DAY1}T12:00:00+00:00", "latest_end": f"{DAY1}T18:00:00+00:00", "duration_minutes": 60},
        ],
    })
    assert duplicated_keys.status_code == 422


def test_heavy_rain_replans_only_affected_and_keeps_history(client):
    register_sample(client)
    run = submit_batch(client)
    before = current_assignments(run)
    bird_before = before["req-bird"]
    visitor_before = before["req-visitor"]
    water_before = before["req-water"]

    rain = client.post("/api/patrol/disruptions", json={
        "idempotency_key": "rain-2026-10-01-am",
        "kind": "heavy_rain",
        "starts_at": f"{DAY1}T08:00:00+00:00",
        "ends_at": f"{DAY1}T12:00:00+00:00",
        "reason": "气象台发布暴雨橙色预警",
        "actor": "duty-officer",
    })
    assert rain.status_code == 201, rain.text
    report = rain.json()["report"]

    # 只有与暴雨时段重叠的待执行分派被重排
    assert set(report["affected_assignment_ids"]) == {bird_before["id"], visitor_before["id"]}
    assert {item["from_assignment_id"] for item in report["rescheduled"]} == {bird_before["id"], visitor_before["id"]}

    # 原安排保留为已改派，历史里能查到改派原因
    old = client.get(f"/api/patrol/assignments/{bird_before['id']}").json()
    assert old["status"] == "rescheduled"
    assert old["planned_start"] == bird_before["planned_start"]
    actions = [event["action"] for event in old["events"]]
    assert actions == ["created", "rescheduled"]
    assert "暴雨" in old["events"][1]["reason"]
    assert old["replaced_by"] is not None

    # 新分派避开暴雨时段，依据中记录了改派来源
    new_bird = client.get(f"/api/patrol/assignments/{old['replaced_by']}").json()
    assert new_bird["status"] == "planned"
    assert not overlaps(new_bird["planned_start"], new_bird["planned_end"], f"{DAY1}T08:00:00+00:00", f"{DAY1}T12:00:00+00:00")
    assert new_bird["rationale"]["replan"]["kind"] == "heavy_rain"
    assert new_bird["rationale"]["replan"]["replaces_assignment_id"] == bird_before["id"]

    # 不受影响的滩涂分派保持原样
    water_now = client.get(f"/api/patrol/assignments/{water_before['id']}").json()
    assert water_now["status"] == "planned"
    assert water_now["planned_start"] == water_before["planned_start"]

    # 重放同一扰动不会产生第二次改派
    replay = client.post("/api/patrol/disruptions", json={
        "idempotency_key": "rain-2026-10-01-am",
        "kind": "heavy_rain",
        "starts_at": f"{DAY1}T08:00:00+00:00",
        "ends_at": f"{DAY1}T12:00:00+00:00",
        "reason": "气象台发布暴雨橙色预警",
        "actor": "duty-officer",
    })
    assert replay.status_code == 201
    assert replay.json()["replayed"] is True
    assert replay.json()["id"] == rain.json()["id"]
    run_after = client.get(f"/api/patrol/runs/{run['id']}").json()
    assert len(run_after["assignments"]) == len(run["assignments"]) + 2


def test_ranger_withdrawal_moves_assignments_to_other_rangers(client):
    register_sample(client)
    run = submit_batch(client)
    current = current_assignments(run)
    bird = current["req-bird"]
    assert bird["ranger_code"] == "ranger-a"

    withdrawal = client.post("/api/patrol/disruptions", json={
        "idempotency_key": "ranger-a-leave-0001",
        "kind": "ranger_unavailable",
        "ranger_code": "ranger-a",
        "starts_at": bird["planned_start"],
        "ends_at": bird["planned_end"],
        "reason": "张三临时请假",
        "actor": "duty-officer",
    })
    assert withdrawal.status_code == 201, withdrawal.text
    report = withdrawal.json()["report"]
    assert report["affected_assignment_ids"] == [bird["id"]]

    moved = client.get(f"/api/patrol/assignments/{report['rescheduled'][0]['to_assignment_id']}").json()
    assert moved["status"] == "planned"
    assert moved["ranger_code"] == "ranger-c"
    assert moved["rationale"]["replan"]["kind"] == "ranger_unavailable"

    # 改派后时段仍不能与同区域其他分派重叠
    visitor = current["req-visitor"]
    assert not overlaps(moved["planned_start"], moved["planned_end"], visitor["planned_start"], visitor["planned_end"])


def test_zone_closure_replans_only_that_zone(client):
    register_sample(client)
    run = submit_batch(client)
    current = current_assignments(run)
    water = current["req-water"]
    bird = current["req-bird"]

    closure = client.post("/api/patrol/disruptions", json={
        "idempotency_key": "closure-mudflat-0001",
        "kind": "zone_closed",
        "zone_code": "mudflat-west",
        "starts_at": water["planned_start"],
        "ends_at": water["planned_end"],
        "reason": "滩涂西区发现受伤保护动物，临时封闭",
        "actor": "duty-officer",
    })
    assert closure.status_code == 201, closure.text
    report = closure.json()["report"]
    assert report["affected_assignment_ids"] == [water["id"]]

    moved = client.get(f"/api/patrol/assignments/{report['rescheduled'][0]['to_assignment_id']}").json()
    assert moved["status"] == "planned"
    assert not overlaps(moved["planned_start"], moved["planned_end"], water["planned_start"], water["planned_end"])

    untouched = client.get(f"/api/patrol/assignments/{bird['id']}").json()
    assert untouched["status"] == "planned"


def test_started_patrol_is_never_rewritten(client):
    register_sample(client)
    run = submit_batch(client)
    current = current_assignments(run)
    bird = current["req-bird"]

    started = client.post(f"/api/patrol/assignments/{bird['id']}/start", json={"actor": "ranger-a"})
    assert started.status_code == 200
    assert started.json()["status"] == "in_progress"

    # 已开始的巡护不会被扰动静默改写，而是列入 skipped_in_progress
    rain = client.post("/api/patrol/disruptions", json={
        "idempotency_key": "rain-overlap-started",
        "kind": "heavy_rain",
        "starts_at": bird["planned_start"],
        "ends_at": bird["planned_end"],
        "reason": "突发暴雨",
        "actor": "duty-officer",
    })
    assert rain.status_code == 201
    report = rain.json()["report"]
    assert report["affected_assignment_ids"] == []
    assert [item["assignment_id"] for item in report["skipped_in_progress"]] == [bird["id"]]
    still = client.get(f"/api/patrol/assignments/{bird['id']}").json()
    assert still["status"] == "in_progress"
    assert still["planned_start"] == bird["planned_start"]

    # 已开始的巡护不能取消，也不能重复开始
    cancel = client.post(f"/api/patrol/assignments/{bird['id']}/cancel", json={"actor": "duty-officer", "reason": "天气原因"})
    assert cancel.status_code == 409
    restart = client.post(f"/api/patrol/assignments/{bird['id']}/start", json={"actor": "ranger-a"})
    assert restart.status_code == 409

    completed = client.post(f"/api/patrol/assignments/{bird['id']}/complete", json={"actor": "ranger-a"})
    assert completed.status_code == 200
    assert completed.json()["status"] == "completed"
    detail = client.get(f"/api/patrol/assignments/{bird['id']}").json()
    assert [event["action"] for event in detail["events"]] == ["created", "started", "completed"]


def test_assignment_query_and_run_history(client):
    register_sample(client)
    run = submit_batch(client)
    current = current_assignments(run)
    visitor = current["req-visitor"]

    cancelled = client.post(
        f"/api/patrol/assignments/{visitor['id']}/cancel",
        json={"actor": "scheduler-li", "reason": "游客活动取消，巡护需求撤销"},
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"

    detail = client.get(f"/api/patrol/assignments/{visitor['id']}").json()
    assert detail["rationale"]["outcome"] == "planned"
    assert [event["action"] for event in detail["events"]] == ["created", "cancelled"]
    assert detail["events"][1]["reason"] == "游客活动取消，巡护需求撤销"

    listed = client.get("/api/patrol/assignments?zone_code=reed-north&status=cancelled").json()["items"]
    assert [item["id"] for item in listed] == [visitor["id"]]

    history = client.get(f"/api/patrol/runs/{run['id']}/events").json()["items"]
    assert any(event["action"] == "run_created" for event in history)
    assert any(event["action"] == "cancelled" for event in history)

    missing = client.get("/api/patrol/assignments/99999")
    assert missing.status_code == 404


def test_disruption_resolution_keeps_existing_schedule(client):
    register_sample(client)
    run = submit_batch(client)
    current = current_assignments(run)
    bird = current["req-bird"]

    rain = client.post("/api/patrol/disruptions", json={
        "idempotency_key": "rain-resolve-0001",
        "kind": "heavy_rain",
        "starts_at": bird["planned_start"],
        "ends_at": bird["planned_end"],
        "reason": "短时强降雨",
        "actor": "duty-officer",
    }).json()
    resolved = client.post(f"/api/patrol/disruptions/{rain['id']}/resolve", json={"actor": "duty-officer"})
    assert resolved.status_code == 200
    assert resolved.json()["status"] == "resolved"
    again = client.post(f"/api/patrol/disruptions/{rain['id']}/resolve", json={"actor": "duty-officer"})
    assert again.status_code == 409
    # 解除扰动不回滚已经完成的改派
    old = client.get(f"/api/patrol/assignments/{bird['id']}").json()
    assert old["status"] == "rescheduled"
