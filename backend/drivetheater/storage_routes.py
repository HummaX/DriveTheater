"""Storage and diagnostics endpoints using the unified request principal."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from .access import Principal, require_principal
from .cache_safety import CacheConflict
from .diagnostics import DiagnosticsService
from .storage_features import StorageService


class PolicyBody(BaseModel):
    keep_days: Literal[7, 30, 90] | None = None
    delete_after_watch: StrictBool = False

    model_config = ConfigDict(extra='forbid')


class CleanupBody(BaseModel):
    video_ids: list[str] = Field(min_length=1, max_length=100)

    model_config = ConfigDict(extra='forbid')


class ConfirmationBody(BaseModel):
    confirm: StrictBool

    model_config = ConfigDict(extra='forbid')


def _call(operation, *args, **kwargs):
    try:
        return operation(*args, **kwargs)
    except CacheConflict as exc:
        raise HTTPException(409, str(exc)) from None
    except OSError:
        raise HTTPException(409, 'Local artifacts are unavailable; refresh before trying again') from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


def create_storage_router(storage: StorageService, diagnostics: DiagnosticsService) -> APIRouter:
    router = APIRouter(prefix='/api')

    @router.get('/storage/details')
    def details(principal: Principal = Depends(require_principal)):
        return _call(storage.inventory)

    @router.get('/storage/policies')
    def policies(principal: Principal = Depends(require_principal)):
        return {'items': storage.policies(), 'defaults': {'keep_days': None, 'delete_after_watch': False}}

    @router.put('/storage/policies/{scope_kind}/{scope_id}')
    def policy(scope_kind: str, scope_id: str, body: PolicyBody, principal: Principal = Depends(require_principal)):
        return _call(storage.set_policy, scope_kind, scope_id, body.keep_days, body.delete_after_watch)

    @router.post('/storage/cleanup/preview')
    def preview(body: CleanupBody, principal: Principal = Depends(require_principal)):
        return _call(storage.create_cleanup_plan, principal.username, body.video_ids)

    @router.post('/storage/cleanup/{plan_id}/confirm')
    def cleanup(plan_id: str, body: ConfirmationBody, principal: Principal = Depends(require_principal)):
        if not body.confirm:
            raise HTTPException(400, 'Explicit cleanup confirmation is required')
        return _call(storage.execute_cleanup_plan, principal.username, plan_id)

    @router.get('/videos/{video_id}/integrity')
    def integrity(video_id: str, principal: Principal = Depends(require_principal)):
        return _call(storage.integrity_status, video_id)

    @router.post('/videos/{video_id}/integrity')
    def verify(video_id: str, principal: Principal = Depends(require_principal)):
        return _call(storage.verify_integrity, video_id)

    @router.get('/videos/{video_id}/diagnostics')
    def probe(video_id: str, principal: Principal = Depends(require_principal)):
        return _call(diagnostics.probe, video_id)

    @router.get('/jobs/{job_id}/stages')
    def stages(job_id: str, principal: Principal = Depends(require_principal)):
        return {'items': _call(diagnostics.job_stages, job_id)}

    @router.get('/diagnostics/monitor')
    def monitor(principal: Principal = Depends(require_principal)):
        return _call(diagnostics.monitor)

    @router.get('/diagnostics/samples')
    def samples(principal: Principal = Depends(require_principal)):
        return {'items': _call(diagnostics.samples)}

    @router.get('/diagnostics/support-export')
    def support_export(principal: Principal = Depends(require_principal)):
        return _call(diagnostics.support_export)

    return router
