from __future__ import annotations

from fastapi import APIRouter, Query, Response

from app.patrol.schemas import (
    AssignmentAction,
    AssignmentCancel,
    AssignmentReassign,
    AvailabilityCreate,
    DisruptionCreate,
    ProtectionWindowCreate,
    RangerCreate,
    RiskObservationCreate,
    ScheduleGenerate,
    ZoneCreate,
)
from app.patrol.service import PatrolService

router = APIRouter(prefix="/api/patrol", tags=["湿地巡护排班"])


def service() -> PatrolService:
    return PatrolService()


@router.post("/zones", status_code=201)
def create_zone(payload: ZoneCreate, actor: str = Query(..., min_length=1)):
    return service().create_zone(payload.model_dump(), actor)


@router.get("/zones")
def list_zones():
    return {"items": service().list_zones()}


@router.post("/protection-windows", status_code=201)
def create_protection_window(payload: ProtectionWindowCreate, actor: str = Query(..., min_length=1)):
    return service().create_protection_window(payload.model_dump(), actor)


@router.get("/protection-windows")
def list_protection_windows(zone_code: str | None = None):
    return {"items": service().list_protection_windows(zone_code)}


@router.post("/rangers", status_code=201)
def create_ranger(payload: RangerCreate, actor: str = Query(..., min_length=1)):
    return service().create_ranger(payload.model_dump(), actor)


@router.get("/rangers")
def list_rangers():
    return {"items": service().list_rangers()}


@router.post("/rangers/{ranger_code}/availability", status_code=201)
def add_availability(ranger_code: str, payload: AvailabilityCreate, actor: str = Query(..., min_length=1)):
    return service().add_availability(ranger_code, payload.model_dump(), actor)


@router.post("/risk-observations", status_code=201)
def create_risk_observation(payload: RiskObservationCreate, actor: str = Query(..., min_length=1)):
    return service().create_risk_observation(payload.model_dump(), actor)


@router.get("/risk-observations")
def list_risk_observations(zone_code: str | None = None, status: str | None = None):
    return {"items": service().list_risk_observations(zone_code, status)}


@router.post("/risk-observations/{risk_id}/close")
def close_risk_observation(risk_id: int, actor: str = Query(..., min_length=1)):
    return service().close_risk_observation(risk_id, actor)


@router.post("/schedules")
def generate_schedule(payload: ScheduleGenerate, response: Response):
    body, status_code = service().generate_schedule(payload.model_dump())
    response.status_code = status_code
    return body


@router.get("/schedules")
def list_schedules(limit: int = Query(default=100, ge=1, le=500)):
    return {"items": service().list_schedules(limit)}


@router.get("/schedules/{schedule_id}")
def get_schedule(schedule_id: int):
    return service().get_schedule(schedule_id)


@router.get("/assignments")
def list_assignments(
    status: str | None = None,
    zone_code: str | None = None,
    ranger_code: str | None = None,
    day: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
):
    return {"items": service().list_assignments(status=status, zone_code=zone_code, ranger_code=ranger_code, day=day, limit=limit)}


@router.get("/assignments/{assignment_id}")
def get_assignment(assignment_id: int):
    return service().get_assignment(assignment_id)


@router.post("/assignments/{assignment_id}/start")
def start_assignment(assignment_id: int, payload: AssignmentAction):
    return service().start_assignment(assignment_id, payload.actor, payload.expected_version)


@router.post("/assignments/{assignment_id}/complete")
def complete_assignment(assignment_id: int, payload: AssignmentAction):
    return service().complete_assignment(assignment_id, payload.actor, payload.expected_version)


@router.post("/assignments/{assignment_id}/cancel")
def cancel_assignment(assignment_id: int, payload: AssignmentCancel):
    return service().cancel_assignment(assignment_id, payload.actor, payload.reason, payload.expected_version)


@router.post("/assignments/{assignment_id}/reassign")
def reassign_assignment(assignment_id: int, payload: AssignmentReassign):
    return service().reassign_assignment(assignment_id, payload.model_dump())


@router.post("/disruptions")
def report_disruption(payload: DisruptionCreate, response: Response):
    body, status_code = service().report_disruption(payload.model_dump())
    response.status_code = status_code
    return body


@router.get("/disruptions")
def list_disruptions(limit: int = Query(default=100, ge=1, le=500)):
    return {"items": service().list_disruptions(limit)}


@router.get("/summary")
def summary():
    return service().summary()
