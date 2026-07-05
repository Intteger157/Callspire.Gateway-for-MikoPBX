"""Kommo integration routes for Callspire PBX Gateway."""

from __future__ import annotations

import asyncio
import logging
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import httpx
from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

import kommo_call_worker
import kommo_jobs_db
import kommo_store

log = logging.getLogger("app_kommo")

_workers_started = False
_cfg: dict = {}
_require_jwt: Optional[Callable] = None
_require_admin: Optional[Callable] = None
_query_cdr_fn: Optional[Callable] = None
_download_recording_fn: Optional[Callable] = None
_extension_from_token_fn: Optional[Callable] = None

UPLOAD_DIR = Path(__file__).resolve().parent / "kommo_client_uploads"


class ProcessCallBody(BaseModel):
    phone: str
    call_time: str
    session_id: Optional[str] = None
    is_incoming: bool = False
    duration_seconds: int = 0
    was_answered: bool = False
    lead_id: Optional[int] = None
    client_recording_enabled: bool = False
    connection_slot: str = "main"
    call_from_label: Optional[str] = None
    call_log: Optional[str] = None
    enable_recording_upload: bool = True
    answer_time: Optional[str] = None


class RetryProcessCallBody(BaseModel):
    phone: str
    call_time: str
    session_id: Optional[str] = None
    is_incoming: bool = False
    duration_seconds: int = 0
    was_answered: bool = False
    lead_id: Optional[int] = None
    client_recording_enabled: bool = False
    connection_slot: str = "main"
    call_from_label: Optional[str] = None
    enable_recording_upload: bool = True
    answer_time: Optional[str] = None
    job_id: Optional[str] = None


def _extension_from_user(user: dict) -> str:
    if _extension_from_token_fn:
        ext = _extension_from_token_fn(user)
        if ext:
            return ext
    return (user.get("extension") or user.get("mikopbx_extension") or "").strip()


async def _get_kommo_session_for_extension(
    extension: str, *, force_refresh: bool = False
) -> Optional[dict[str, Any]]:
    if not kommo_store.is_enabled():
        return None
    oauth = kommo_store.get_oauth_tokens()
    access = (oauth.get("access_token") or "").strip()
    if not access:
        return None
    if force_refresh or kommo_store.token_expired(oauth.get("expires_at")):
        refreshed = await _refresh_kommo_token(oauth)
        if refreshed:
            oauth = refreshed
            access = oauth.get("access_token") or ""
    subdomain = kommo_store.get_subdomain()
    mapping = kommo_store.get_extension_mapping(extension)
    if mapping.get("excluded"):
        return None
    kommo_user_id = mapping.get("kommo_user_id")
    kommo_user_name = mapping.get("kommo_user_name") or ""
    source = "extension_map" if kommo_user_id else "account_default"
    return {
        "subdomain": subdomain,
        "access_token": access,
        "expires_at": oauth.get("expires_at"),
        "account_base_url": kommo_store.build_account_base_url(subdomain) + "/api/v4",
        "kommo_user_id": kommo_user_id,
        "kommo_user_name": kommo_user_name,
        "kommo_user_id_source": source,
    }


async def _refresh_kommo_token(oauth: dict) -> Optional[dict]:
    refresh = (oauth.get("refresh_token") or "").strip()
    client_id = (oauth.get("client_id") or _cfg.get("kommo_client_id") or "").strip()
    client_secret = (oauth.get("client_secret") or _cfg.get("kommo_client_secret") or "").strip()
    subdomain = kommo_store.get_subdomain()
    if not refresh or not client_id or not client_secret or not subdomain:
        return None
    base = kommo_store.build_account_base_url(subdomain)
    token_url = f"{base}/oauth2/access_token"
    try:
        async with httpx.AsyncClient(timeout=30.0) as http:
            resp = await http.post(
                token_url,
                json={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh,
                    "redirect_uri": oauth.get("redirect_uri") or _cfg.get("kommo_redirect_uri") or "",
                },
            )
        if resp.status_code != 200:
            log.warning("kommo token refresh failed: %s", resp.text[:200])
            return None
        data = resp.json()
        expires_in = int(data.get("expires_in") or 0)
        expires_at = (
            datetime.now(timezone.utc).timestamp() + expires_in
            if expires_in
            else None
        )
        expires_iso = (
            datetime.fromtimestamp(expires_at, tz=timezone.utc).isoformat()
            if expires_at
            else None
        )
        kommo_store.set_oauth_tokens(
            data.get("access_token") or "",
            data.get("refresh_token") or refresh,
            expires_iso,
            client_id=client_id,
            client_secret=client_secret,
        )
        return kommo_store.get_oauth_tokens()
    except Exception as exc:
        log.warning("kommo refresh error: %s", exc)
        return None


def _kommo_status_for_extension(extension: str) -> dict[str, Any]:
    enabled = kommo_store.is_enabled()
    oauth = kommo_store.get_oauth_tokens()
    configured = bool(kommo_store.get_subdomain() and oauth.get("access_token"))
    authorized = configured and not kommo_store.token_expired(oauth.get("expires_at"))
    mapping = kommo_store.get_extension_mapping(extension)
    excluded = bool(mapping.get("excluded"))
    kommo_user_id = mapping.get("kommo_user_id")
    kommo_user_name = mapping.get("kommo_user_name") or ""
    offer = enabled and configured and authorized and not excluded
    upload_enabled = offer and bool(kommo_user_id)
    return {
        "available": enabled,
        "enabled": enabled,
        "configured": configured,
        "authorized": authorized,
        "subdomain": kommo_store.get_subdomain(),
        "token_expires_at": oauth.get("expires_at"),
        "token_expired": kommo_store.token_expired(oauth.get("expires_at")),
        "excluded": excluded,
        "offer_gateway": offer,
        "kommo_user_id": kommo_user_id,
        "kommo_user_name": kommo_user_name,
        "upload_enabled": upload_enabled,
    }


def _job_to_api(job: dict[str, Any]) -> dict[str, Any]:
    upload_source = job.get("upload_source")
    amo_source = None
    if upload_source == "miko_pbx":
        amo_source = "miko_pbx"
    elif upload_source == "client":
        amo_source = "local"
    return {
        "id": job["id"],
        "status": job["status"],
        "lead_id": job.get("lead_id"),
        "upload_source": amo_source,
        "reason": job.get("reason"),
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
    }


async def _ensure_workers() -> None:
    global _workers_started
    if _workers_started:
        return
    if not _query_cdr_fn or not _download_recording_fn:
        msg = (
            "[kommo] workers NOT started — register_kommo_routes missing "
            "query_cdr/download_recording (see APP_PY_PATCH.md)"
        )
        log.warning(msg)
        print(msg, flush=True)
        return

    async def query_cdr(**kwargs):
        return await _query_cdr_fn(**kwargs)

    async def download_recording(linked_id: str, dest: Path) -> bool:
        return await _download_recording_fn(linked_id, dest)

    await kommo_call_worker.start_workers(
        get_session_for_extension=_get_kommo_session_for_extension,
        query_cdr=query_cdr,
        download_recording=download_recording,
    )
    _workers_started = True


def register_kommo_routes(
    app,
    *,
    cfg: dict,
    require_admin,
    require_jwt,
    templates,
    html_context,
    kommo_default_redirect_uri,
    query_cdr=None,
    download_recording=None,
    extension_from_token=None,
    **kwargs,
) -> None:
    if kwargs:
        log.info(
            "register_kommo_routes: ignoring legacy kwargs %s",
            sorted(kwargs.keys()),
        )
    global _cfg, _require_jwt, _require_admin, _query_cdr_fn, _download_recording_fn, _extension_from_token_fn
    _cfg = cfg
    _require_jwt = require_jwt
    _require_admin = require_admin
    _query_cdr_fn = query_cdr
    _download_recording_fn = download_recording
    _extension_from_token_fn = extension_from_token

    db_path = cfg.get("permissions_db_path") or cfg.get("kommo_jobs_db_path")
    kommo_store.init_db(db_path)
    kommo_jobs_db.init_db(db_path)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

    import kommo_recording
    kommo_recording.configure_pbx_timezone(cfg.get("pbx_utc_offset_hours", 0))

    router = APIRouter()

    @router.get("/api/kommo/status")
    async def kommo_status(user: dict = Depends(require_jwt)):
        ext = _extension_from_user(user)
        return _kommo_status_for_extension(ext)

    @router.get("/api/kommo/session")
    async def kommo_session(
        user: dict = Depends(require_jwt),
        force_refresh: bool = Query(False),
    ):
        ext = _extension_from_user(user)
        status = _kommo_status_for_extension(ext)
        if status.get("excluded"):
            raise HTTPException(403, "Extension excluded from gateway Kommo")
        if not status.get("offer_gateway"):
            raise HTTPException(503, "Kommo gateway module is not available")
        session = await _get_kommo_session_for_extension(ext, force_refresh=force_refresh)
        if not session:
            raise HTTPException(503, "Kommo session unavailable")
        return session

    @router.post("/api/kommo/process-call")
    async def kommo_process_call(body: ProcessCallBody, user: dict = Depends(require_jwt)):
        ext = _extension_from_user(user)
        if not ext:
            raise HTTPException(403, "No PBX extension on this account")
        status = _kommo_status_for_extension(ext)
        if not status.get("offer_gateway"):
            raise HTTPException(503, "Kommo gateway module is not available")
        if not status.get("upload_enabled"):
            raise HTTPException(
                403,
                "Kommo upload is not enabled for this extension (map a Kommo user in admin)",
            )

        dedup = kommo_call_worker.build_dedup_key(
            ext, body.phone, body.call_time, body.session_id
        )
        existing = kommo_jobs_db.get_job_by_dedup(dedup)
        if existing and existing["status"] == "uploaded":
            return _job_to_api(existing)

        payload = body.model_dump()
        initial_status = "waiting_recording" if body.client_recording_enabled else "queued"
        job = kommo_jobs_db.create_job(ext, dedup, payload, status=initial_status)
        await _ensure_workers()
        return _job_to_api(job)

    @router.get("/api/kommo/process-call/{job_id}")
    async def kommo_process_call_status(job_id: str, user: dict = Depends(require_jwt)):
        ext = _extension_from_user(user)
        job = kommo_jobs_db.get_job(job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        if job["extension"] != ext and user.get("role") != "admin":
            raise HTTPException(403, "Forbidden")
        return _job_to_api(job)

    @router.put("/api/kommo/process-call/{job_id}/recording")
    async def kommo_process_call_recording(
        job_id: str,
        user: dict = Depends(require_jwt),
        file: UploadFile = File(...),
    ):
        ext = _extension_from_user(user)
        job = kommo_jobs_db.get_job(job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        if job["extension"] != ext:
            raise HTTPException(403, "Forbidden")

        dest_dir = UPLOAD_DIR / job_id
        dest_dir.mkdir(parents=True, exist_ok=True)
        suffix = Path(file.filename or "recording.wav").suffix or ".wav"
        dest = dest_dir / f"client{suffix}"
        with dest.open("wb") as out:
            shutil.copyfileobj(file.file, out)

        kommo_jobs_db.update_job(
            job_id,
            recording_path=str(dest),
            upload_source="client",
            status="queued",
        )
        await _ensure_workers()
        return {"ok": True, "path": str(dest)}

    @router.post("/api/kommo/process-call/retry")
    async def kommo_process_call_retry(body: RetryProcessCallBody, user: dict = Depends(require_jwt)):
        ext = _extension_from_user(user)
        if not ext:
            raise HTTPException(403, "No PBX extension")
        if body.job_id:
            job = kommo_jobs_db.get_job(body.job_id)
            if job and job["extension"] == ext:
                payload = dict(job["payload"])
                if body.lead_id:
                    payload["lead_id"] = body.lead_id
                kommo_jobs_db.update_job(
                    body.job_id,
                    status="waiting_recording" if body.client_recording_enabled else "queued",
                    payload_json=payload,
                    reason=None,
                )
                await _ensure_workers()
                return _job_to_api(kommo_jobs_db.get_job(body.job_id))

        req = ProcessCallBody(**body.model_dump(exclude={"job_id"}))
        return await kommo_process_call(req, user)

    @router.get("/oauth/kommo/callback")
    async def kommo_oauth_callback(request: Request, code: str = Query(""), state: str = Query("")):
        if not code:
            raise HTTPException(400, "Missing code")
        oauth = kommo_store.get_oauth_tokens()
        client_id = (oauth.get("client_id") or _cfg.get("kommo_client_id") or "").strip()
        client_secret = (oauth.get("client_secret") or _cfg.get("kommo_client_secret") or "").strip()
        redirect_uri = kommo_default_redirect_uri(request)
        subdomain = kommo_store.get_subdomain()
        if not client_id or not client_secret or not subdomain:
            raise HTTPException(500, "Kommo OAuth not configured")
        token_url = f"{kommo_store.build_account_base_url(subdomain)}/oauth2/access_token"
        async with httpx.AsyncClient(timeout=30.0) as http:
            resp = await http.post(
                token_url,
                json={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                },
            )
        if resp.status_code != 200:
            raise HTTPException(502, f"Token exchange failed: {resp.text[:200]}")
        data = resp.json()
        expires_in = int(data.get("expires_in") or 0)
        expires_iso = (
            datetime.now(timezone.utc).timestamp() + expires_in
            if expires_in
            else None
        )
        expires_str = (
            datetime.fromtimestamp(expires_iso, tz=timezone.utc).isoformat()
            if expires_iso
            else None
        )
        kommo_store.set_oauth_tokens(
            data.get("access_token") or "",
            data.get("refresh_token") or "",
            expires_str,
            client_id=client_id,
            client_secret=client_secret,
        )
        return RedirectResponse(url="/admin/kommo?authorized=1", status_code=302)

    @router.get("/admin/kommo", response_class=HTMLResponse)
    async def admin_kommo_page(request: Request, _admin: dict = Depends(require_admin)):
        status = _kommo_status_for_extension("")
        ctx = html_context(request)
        ctx.update({"kommo": status, "subdomain": kommo_store.get_subdomain()})
        return templates.TemplateResponse(request, "admin_kommo.html", context=ctx)

    app.include_router(router)

    @app.on_event("startup")
    async def _kommo_startup():
        asyncio.create_task(_ensure_workers())
