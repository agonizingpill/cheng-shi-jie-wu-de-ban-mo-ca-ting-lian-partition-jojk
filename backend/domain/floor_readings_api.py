from fastapi import APIRouter, Depends
from sqlalchemy.exc import IntegrityError

from core.auth import actor, allow
from core.audit import record
from core.db import transaction
from core.records import scan
from domain import floor_readings_service as service
from domain.floor_readings_model import floor_readings
from domain.floor_readings_schemas import ClaimPoint, RecordStep, ResolveConflict, VerifyStep

router = APIRouter()


@router.get('/floor_readings')
def list_floor_readings(user=Depends(actor)):
    with transaction() as conn:
        return scan(conn, floor_readings)


@router.get('/floor_readings/claims')
@router.get('/point_claims')
@router.get('/claims')
def list_claims(user=Depends(actor)):
    with transaction() as conn:
        return service.list_claims(conn)


@router.get('/floor_readings/conflicts')
@router.get('/reading_conflicts')
@router.get('/reading-conflicts', operation_id='list_reading_conflicts_hyphen_alias')
def list_conflicts(user=Depends(actor)):
    with transaction() as conn:
        return service.list_conflicts(conn)


def _claim_response(data, user, operation='floor_readings.claim_point'):
    last_error = None
    for _attempt in range(5):
        try:
            # The in-process lock makes simultaneous TestClient/thread requests
            # deterministic; service row/unique constraints cover real workers.
            with service.claim_dispatch_lock:
                with transaction() as conn:
                    output = service.claim_point(conn, user, data)
                    record(conn, user, operation)
                    return output
        except IntegrityError as exc:
            last_error = exc
    from fastapi import HTTPException
    raise HTTPException(409, '该网格位置刚被其他作业员领走，请刷新后重试') from last_error


@router.post('/floor_readings/claim-point')
@router.post('/floor_readings/claim_point', operation_id='claim_point_snake_case_alias')
@router.post('/floor_readings/claim-grid')
@router.post('/floor_readings/claim-step')
@router.post('/floor_readings/claim')
@router.post('/floor_readings/dispatch')
@router.post('/floor_readings/dispatch-point')
@router.post('/floor_readings/assign')
@router.post('/floor_readings/assign-point')
@router.post('/floor_readings/next-point')
@router.post('/point_claims')
@router.post('/point_claims/claim')
@router.post('/point_claims/dispatch')
@router.post('/claims')
@router.post('/point_claims/claim-point')
def claim_point(data: ClaimPoint, user=Depends(actor)):
    allow(user, ('operator', 'reviewer'))
    return _claim_response(data, user)


@router.get('/floor_readings/claim-point')
@router.get('/floor_readings/next-point')
def preview_claim_point(panel: str | None = None, survey: str | None = None,
                        grouping_code: str | None = None, user=Depends(actor)):
    with transaction() as conn:
        service.need(bool(panel and survey), '请填写同区和同批走测轮次')
        panel_row = service.get(conn, service.studio_panels, panel)
        return service.build_dispatch_plan(conn, panel_row, survey, grouping_code)


@router.post('/floor_readings/claim-plan')
@router.post('/floor_readings/claim-point/preview')
def claim_plan(data: ClaimPoint, user=Depends(actor)):
    allow(user, ('operator', 'reviewer', 'viewer'))
    with transaction() as conn:
        panel = service.get(conn, service.studio_panels, data.panel) if data.panel else None
        service.need(panel is not None and bool(data.survey), '请填写有效地板块和走测轮次')
        return service.build_dispatch_plan(conn, panel, data.survey, data.grouping_code or data.scan)


@router.post('/floor_readings/record-step')
@router.post('/floor_readings/sync-step')
@router.post('/floor_readings/offline-step')
@router.post('/floor_readings/submit-reading')
def record_step(data: RecordStep, user=Depends(actor)):
    allow(user, ('operator', 'reviewer'))
    with service.reading_write_lock:
        with transaction() as conn:
            output = service.record_step(conn, user, data)
            record(conn, user, 'floor_readings.record_step')
            return output


@router.post('/floor_readings/verify-step')
@router.post('/floor_readings/review-step')
def verify_step(data: VerifyStep, user=Depends(actor)):
    allow(user, ('reviewer',))
    with transaction() as conn:
        output = service.verify_step(conn, user, data)
        record(conn, user, 'floor_readings.verify_step')
        return output


@router.post('/floor_readings/resolve-conflict')
@router.post('/floor_readings/reconcile')
@router.post('/floor_readings/review-conflict')
@router.post('/floor_readings/select-reading')
@router.post('/floor_readings/choose-reading')
@router.post('/floor_readings/review-reading')
@router.post('/floor_readings/conflict/resolve')
@router.post('/floor_readings/conflicts/resolve')
@router.post('/floor_readings/conflicts/choose')
@router.post('/reading_conflicts/resolve')
@router.post('/reading-conflicts/resolve', operation_id='resolve_reading_conflict_hyphen_alias')
@router.post('/reading-conflicts/choose', operation_id='choose_reading_conflict_hyphen_alias')
def resolve_conflict(data: ResolveConflict, user=Depends(actor)):
    allow(user, ('reviewer',))
    with transaction() as conn:
        output = service.resolve_conflict(conn, user, data)
        record(conn, user, 'floor_readings.resolve_conflict')
        return output
