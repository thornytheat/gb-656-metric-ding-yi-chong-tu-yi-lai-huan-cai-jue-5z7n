"""M8 入口：合并页、会话接口、裁决与发布。"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app import config, errors, store, web
from app.modules.m8_merge import service

router = APIRouter()

OPEN_KEYS = {"base_file", "left_file", "right_file"}
SELECT_KEYS = {"selections", "expected_revision"}
PUBLISH_KEYS = {"expected_revision", "effective_from", "effective_to",
                "base_version_seq"}


def _reject_unknown(body: dict, allowed: set[str]) -> None:
    unknown = set(body) - allowed
    if unknown:
        raise errors.BadInput("出现未知参数", detail={"keys": sorted(unknown)})


@router.get("/merge", response_class=HTMLResponse)
def merge_page(request: Request):
    sessions = store.list_merge_sessions(20)
    return web.page_or_404(
        request,
        "merge.html",
        "merge",
        store.latest_run(),
        sessions=sessions,
        defaults={"base_file": config.MERGE_BASE,
                  "left_file": config.MERGE_LEFT,
                  "right_file": config.MERGE_RIGHT},
        current_seq=store.current_version_seq(),
    )


@router.post("/api/merges")
def open_merge(payload: dict | None = None):
    body = payload or {}
    _reject_unknown(body, OPEN_KEYS)
    base_file = str(body.get("base_file") or config.MERGE_BASE).strip()
    left_file = str(body.get("left_file") or config.MERGE_LEFT).strip()
    right_file = str(body.get("right_file") or config.MERGE_RIGHT).strip()
    if not base_file or not left_file or not right_file:
        raise errors.BadInput("需要共同基线与两组改动三份文件",
                              detail={"field": "base_file/left_file/right_file"})
    session = service.open_session(base_file, left_file, right_file)
    return _summary(session)


@router.get("/api/merges")
def list_merges():
    return {"sessions": store.list_merge_sessions(30),
            "current_version_seq": store.current_version_seq()}


@router.get("/api/merges/{merge_id}")
def get_merge(merge_id: str):
    session = service.get_session(merge_id)
    return {**_summary(session),
            "conflicts": session.get("conflicts") or [],
            "pending": session.get("pending"),
            "last_error": session.get("last_error")}


@router.get("/api/merges/{merge_id}/preview")
def preview_merge(merge_id: str):
    return service.preview(merge_id)


@router.post("/api/merges/{merge_id}/selections")
def save_selections(merge_id: str, payload: dict | None = None):
    body = payload or {}
    _reject_unknown(body, SELECT_KEYS)
    selections = body.get("selections")
    if not isinstance(selections, dict) or not selections:
        raise errors.BadInput("selections 必须是非空对象",
                              detail={"field": "selections"})
    session = service.save_selections(merge_id, selections,
                                      body.get("expected_revision"))
    return {**_summary(session),
            "revision": session["selection_revision"],
            "selections": session.get("selections") or {},
            "conflicts": session.get("conflicts") or []}


@router.post("/api/merges/{merge_id}/publish")
def publish_merge(merge_id: str, payload: dict | None = None):
    body = payload or {}
    _reject_unknown(body, PUBLISH_KEYS)
    return service.publish(
        merge_id,
        expected_revision=body.get("expected_revision"),
        effective_from=body.get("effective_from"),
        effective_to=body.get("effective_to"),
        base_version_seq=body.get("base_version_seq"),
    )


@router.post("/api/merges/{merge_id}/recover")
def recover_merge(merge_id: str, payload: dict | None = None):
    body = payload or {}
    _reject_unknown(body, PUBLISH_KEYS)
    return service.recover(
        merge_id,
        effective_from=body.get("effective_from"),
        effective_to=body.get("effective_to"),
        base_version_seq=body.get("base_version_seq"),
    )


def _summary(session: dict) -> dict:
    return {
        "merge_id": session["merge_id"],
        "merge_key": session["merge_key"],
        "status": session["status"],
        "base_ref": session["base_ref"],
        "left_ref": session["left_ref"],
        "right_ref": session["right_ref"],
        "labels": session.get("labels") or {},
        "snapshots": session.get("snapshots") or {},
        "content_digest": session["content_digest"],
        "selection_revision": session["selection_revision"],
        "selections": session.get("selections") or {},
        "conflicts": session.get("conflicts") or [],
        "stats": (session.get("options") or {}).get("stats")
        or (session.get("merge") or {}).get("stats") or {},
        "published_run_id": session.get("published_run_id"),
        "published_version_seq": session.get("published_version_seq"),
    }
