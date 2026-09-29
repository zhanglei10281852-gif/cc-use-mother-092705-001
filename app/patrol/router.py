from __future__ import annotations

from fastapi import APIRouter, Query

from app.patrol.schemas import (
    AssignmentAction,
    AvailabilityIn,
    DisruptionCreate,
    ObservationCreate,
    RangerCreate,
    RunCreate,
    WindowIn,
    ZoneCreate,
)
from app.patrol.service import PatrolService

router = APIRouter(prefix="/api/patrol", tags=["湿地巡护排班"])


def service() -> PatrolService:
    return PatrolService()


@router.post("/zones", status_code=201)
def create_zone(payload: ZoneCreate):
    return service().create_zone(payload.model_dump())


@router.get("/zones")
def list_zones():
    return {"items": service().list_zones()}


@router.get("/zones/{zone_id}")
def get_zone(zone_id: int):
    return service().get_zone(zone_id)


@router.post("/zones/{zone_id}/windows", status_code=201)
def add_window(zone_id: int, payload: WindowIn):
    return service().add_window(zone_id, payload.model_dump())


@router.post("/rangers", status_code=201)
def create_ranger(payload: RangerCreate):
    return service().create_ranger(payload.model_dump())


@router.get("/rangers")
def list_rangers():
    return {"items": service().list_rangers()}


@router.post("/rangers/{ranger_id}/availability", status_code=201)
def add_availability(ranger_id: int, payload: AvailabilityIn):
    return service().add_availability(ranger_id, payload.model_dump())


@router.post("/observations", status_code=201)
def create_observation(payload: ObservationCreate):
    return service().create_observation(payload.model_dump())


@router.get("/observations")
def list_observations(zone_code: str | None = None):
    return {"items": service().list_observations(zone_code)}


@router.post("/runs", status_code=201)
def submit_run(payload: RunCreate):
    return service().submit_run(payload.model_dump())


@router.get("/runs")
def list_runs(limit: int = Query(default=100, ge=1, le=500)):
    return {"items": service().list_runs(limit)}


@router.get("/runs/{run_id}")
def get_run(run_id: int):
    return service().get_run(run_id)


@router.get("/runs/{run_id}/events")
def get_run_events(run_id: int):
    return {"items": service().run_events(run_id)}


@router.get("/assignments")
def list_assignments(
    status: str | None = None,
    zone_code: str | None = None,
    ranger_code: str | None = None,
    run_id: int | None = None,
    limit: int = Query(default=200, ge=1, le=500),
):
    return {"items": service().list_assignments(status=status, zone_code=zone_code, ranger_code=ranger_code, run_id=run_id, limit=limit)}


@router.get("/assignments/{assignment_id}")
def get_assignment(assignment_id: int):
    return service().get_assignment(assignment_id)


@router.post("/assignments/{assignment_id}/start")
def start_assignment(assignment_id: int, payload: AssignmentAction):
    return service().start_assignment(assignment_id, payload.actor)


@router.post("/assignments/{assignment_id}/complete")
def complete_assignment(assignment_id: int, payload: AssignmentAction):
    return service().complete_assignment(assignment_id, payload.actor)


@router.post("/assignments/{assignment_id}/cancel")
def cancel_assignment(assignment_id: int, payload: AssignmentAction):
    return service().cancel_assignment(assignment_id, payload.actor, payload.reason)


@router.post("/disruptions", status_code=201)
def create_disruption(payload: DisruptionCreate):
    return service().create_disruption(payload.model_dump())


@router.get("/disruptions")
def list_disruptions():
    return {"items": service().list_disruptions()}


@router.get("/disruptions/{disruption_id}")
def get_disruption(disruption_id: int):
    return service().get_disruption(disruption_id)


@router.post("/disruptions/{disruption_id}/resolve")
def resolve_disruption(disruption_id: int, payload: AssignmentAction):
    return service().resolve_disruption(disruption_id, payload.actor)
