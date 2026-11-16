"""Authenticated routes for explicit metadata lookups and local presentation."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

from .access import require_principal
from . import metadata_features

router = APIRouter()


class MetadataCandidate(BaseModel):
    media_type: Literal["movie", "series"]
    media_id: str = Field(pattern=r"^tt\d{4,12}$")
    language: str | None = Field(default=None, pattern=r"^[a-z]{2,8}$")


@router.get("/api/metadata/videos/{video_id}")
def details(video_id: str, request: Request):
    require_principal(request)
    return {"metadata": metadata_features.local_metadata(request.app.state.db, video_id)}


@router.get("/api/metadata/videos/{video_id}/related")
def related(video_id: str, request: Request, limit: int = 12):
    require_principal(request)
    return {"items": metadata_features.related_local(request.app.state.db, video_id, limit)}


@router.get("/api/metadata/videos/{video_id}/episodes")
def episodes(video_id: str, request: Request):
    require_principal(request)
    return metadata_features.series_episodes(request.app.state.db, video_id)


@router.post("/api/metadata/videos/{video_id}/preview")
def preview(video_id: str, candidate: MetadataCandidate, request: Request):
    user = require_principal(request)
    # Check the local resource before contacting the configured provider.
    metadata_features.local_metadata(request.app.state.db, video_id)
    return {"candidate": metadata_features.preview(request.app.state.db, user.username, request, candidate.media_type, candidate.media_id, candidate.language)}


@router.post("/api/metadata/videos/{video_id}/fetch")
def fetch(video_id: str, candidate: MetadataCandidate, request: Request):
    user = require_principal(request)
    # This POST is the confirmation step. It is the only write path and does not
    # enqueue jobs/downloads or call any provider other than the configured add-on.
    return {"metadata": metadata_features.fetch_and_store(request.app.state.db, user.username, request, video_id, candidate.media_type, candidate.media_id, candidate.language)}


@router.post("/api/metadata/videos/{video_id}/poster")
async def upload_poster(video_id: str, request: Request, poster: UploadFile = File(...)):
    require_principal(request)
    return await metadata_features.save_custom_poster(request.app.state.db, request.app.state.config, video_id, poster)


@router.get("/api/metadata/videos/{video_id}/poster")
def custom_poster(video_id: str, request: Request):
    require_principal(request)
    content = metadata_features.custom_poster_bytes(request.app.state.db, request.app.state.config, video_id)
    return Response(content, media_type="image/png", headers={"Cache-Control": "private, no-store"})
