"""Routes for local, authenticated library organization and search."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from .access import Principal, require_principal
from . import library

router = APIRouter(prefix="/api/library", tags=["library"])


class PreferencesPatch(BaseModel):
    watched_threshold: int | None = Field(default=None, ge=50, le=100)
    autoplay_next: bool | None = None
    metadata_language: str | None = Field(default=None, min_length=2, max_length=8)


class OverridePatch(BaseModel):
    title: str | None = Field(default=None, max_length=500)
    year: int | None = Field(default=None)
    genre: list[str] | None = None
    cast: list[str] | None = None
    director: str | None = Field(default=None, max_length=500)
    type: str | None = Field(default=None, max_length=500)
    series: str | None = Field(default=None, max_length=500)
    season: int | None = Field(default=None)
    episode: int | None = Field(default=None)


class WatchedPatch(BaseModel): watched: bool
class CollectionCreate(BaseModel): name: str = Field(min_length=1, max_length=120)
class CollectionItem(BaseModel): video_id: str = Field(min_length=1, max_length=128); position: int = Field(default=0, ge=0)
class CollectionOrder(BaseModel): video_ids: list[str] = Field(default_factory=list, max_length=500)
class SavedViewCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    filters: dict = Field(default_factory=dict)
    sort: str = "title"
class DownloadPlanCreate(BaseModel): video_ids: list[str] = Field(min_length=1, max_length=500)
class PlaybackObservation(BaseModel): position: float; duration: float
class CaptionIndex(BaseModel): track_id: str = Field(min_length=1, max_length=128)


@router.get("")
def list_items(request: Request, filters: str = "", sort: str = "title", query: str = "", limit: int = Query(500, ge=1, le=500), principal: Principal = Depends(require_principal)):
    try: parsed = json.loads(filters) if filters else {}
    except ValueError: raise HTTPException(422, "filters must be JSON") from None
    result = library.list_library(request.app.state.db, principal.username, parsed, sort, query, limit)
    if query.strip(): library.record_search(request.app.state.db, principal.username, query)
    return {"items": result, "returned": len(result), "limit": limit}


@router.get("/preferences")
def get_preferences(request: Request, principal: Principal = Depends(require_principal)):
    return library.preferences(request.app.state.db, principal.username)


@router.put("/preferences")
def patch_preferences(payload: PreferencesPatch, request: Request, principal: Principal = Depends(require_principal)):
    return library.update_preferences(request.app.state.db, principal.username, payload.model_dump(exclude_none=True))


@router.get("/collections")
def collections(request: Request, principal: Principal = Depends(require_principal)):
    return {"items": library.list_collections(request.app.state.db, principal.username)}


@router.post("/collections")
def create_collection(payload: CollectionCreate, request: Request, principal: Principal = Depends(require_principal)):
    return library.create_collection(request.app.state.db, principal.username, payload.name)


@router.patch("/collections/{collection_id}")
def patch_collection(collection_id: str, payload: CollectionCreate, request: Request, principal: Principal = Depends(require_principal)):
    return library.update_collection(request.app.state.db, principal.username, collection_id, payload.name)


@router.delete("/collections/{collection_id}")
def remove_collection(collection_id: str, request: Request, principal: Principal = Depends(require_principal)):
    library.delete_collection(request.app.state.db, principal.username, collection_id)
    return {"ok": True}


@router.get("/collections/{collection_id}/items")
def list_collection_items(collection_id: str, request: Request, principal: Principal = Depends(require_principal)):
    return {"items": library.list_collection_items(request.app.state.db, principal.username, collection_id)}


@router.post("/collections/{collection_id}/items")
def collection_item(collection_id: str, payload: CollectionItem, request: Request, principal: Principal = Depends(require_principal)):
    library.add_collection_item(request.app.state.db, principal.username, collection_id, payload.video_id, payload.position)
    return {"ok": True}


@router.delete("/collections/{collection_id}/items/{video_id}")
def remove_collection_item(collection_id: str, video_id: str, request: Request, principal: Principal = Depends(require_principal)):
    library.remove_collection_item(request.app.state.db, principal.username, collection_id, video_id)
    return {"ok": True}


@router.put("/collections/{collection_id}/items/order")
def reorder_collection(collection_id: str, payload: CollectionOrder, request: Request, principal: Principal = Depends(require_principal)):
    library.reorder_collection_items(request.app.state.db, principal.username, collection_id, payload.video_ids)
    return {"ok": True}


@router.get("/saved-views")
def saved_views(request: Request, principal: Principal = Depends(require_principal)):
    return {"items": library.list_saved_views(request.app.state.db, principal.username)}


@router.post("/saved-views")
def create_view(payload: SavedViewCreate, request: Request, principal: Principal = Depends(require_principal)):
    return library.create_saved_view(request.app.state.db, principal.username, payload.name, payload.filters, payload.sort)


@router.patch("/saved-views/{view_id}")
def patch_view(view_id: str, payload: SavedViewCreate, request: Request, principal: Principal = Depends(require_principal)):
    return library.update_saved_view(request.app.state.db, principal.username, view_id, payload.name, payload.filters, payload.sort)


@router.delete("/saved-views/{view_id}")
def remove_view(view_id: str, request: Request, principal: Principal = Depends(require_principal)):
    library.delete_saved_view(request.app.state.db, principal.username, view_id)
    return {"ok": True}


@router.get("/recent-searches")
def recent_searches(request: Request, principal: Principal = Depends(require_principal)):
    return {"items": library.list_recent_searches(request.app.state.db, principal.username)}


@router.delete("/recent-searches")
def clear_searches(request: Request, principal: Principal = Depends(require_principal)):
    library.clear_recent_searches(request.app.state.db, principal.username)
    return {"ok": True}


@router.delete("/recent-searches/{search_id}")
def remove_search(search_id: str, request: Request, principal: Principal = Depends(require_principal)):
    library.delete_recent_search(request.app.state.db, principal.username, search_id)
    return {"ok": True}


@router.post("/videos/{video_id}/override")
def override(video_id: str, payload: OverridePatch, request: Request, principal: Principal = Depends(require_principal)):
    # exclude_unset preserves an explicit JSON null, which resets that override.
    return library.update_override(request.app.state.db, video_id, payload.model_dump(exclude_unset=True))


@router.post("/videos/{video_id}/watched")
def watched(video_id: str, payload: WatchedPatch, request: Request, principal: Principal = Depends(require_principal)):
    return library.set_watched(request.app.state.db, principal.username, video_id, payload.watched)


@router.post("/videos/{video_id}/playback")
def playback(video_id: str, payload: PlaybackObservation, request: Request, principal: Principal = Depends(require_principal)):
    return library.observe_playback(request.app.state.db, principal.username, video_id, payload.position, payload.duration)


@router.get("/videos/{video_id}/next")
def next_episode(video_id: str, request: Request, principal: Principal = Depends(require_principal)):
    return {"item": library.next_episode(request.app.state.db, principal.username, video_id)}


@router.get("/series/{series}/progress")
def get_series_progress(series: str, request: Request, principal: Principal = Depends(require_principal)):
    return library.series_progress(request.app.state.db, principal.username, series)


@router.post("/videos/{video_id}/captions")
def captions(video_id: str, payload: CaptionIndex, request: Request, principal: Principal = Depends(require_principal)):
    from .caption_index import index_existing_captions
    return {"indexed": index_existing_captions(request.app.state.db, request.app.state.config.media_dir, video_id, payload.track_id)}


@router.delete("/videos/{video_id}/captions")
def remove_captions(video_id: str, request: Request, track_id: str | None = None, principal: Principal = Depends(require_principal)):
    return {"removed": library.remove_caption_index(request.app.state.db, video_id, track_id)}


@router.post("/download-plans")
def download_plan(payload: DownloadPlanCreate, request: Request, principal: Principal = Depends(require_principal)):
    return library.create_download_plan(request.app.state.db, principal.username, payload.video_ids)


@router.post("/download-plans/{plan_id}/confirm")
def confirm_download_plan(plan_id: str, request: Request, principal: Principal = Depends(require_principal)):
    return library.confirm_download_plan(request.app.state.db, principal.username, plan_id)
