"""Background workers for Kommo process-call jobs."""

from __future__ import annotations

import asyncio
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import kommo_jobs_db
from kommo_crm import KommoCrmClient, ProcessCallOutcome
from kommo_recording import resolve_miko_recording, MAX_PBX_RECORDING_JOB_RETRIES

log = logging.getLogger("kommo_call_worker")

WORKER_COUNT = 3
CLIENT_RECORDING_WAIT_SECONDS = 360
RECORDINGS_DIR = Path(__file__).resolve().parent / "kommo_upload_recordings"

_running = False
_tasks: list[asyncio.Task] = []


def build_dedup_key(extension: str, phone: str, call_time: str, session_id: Optional[str]) -> str:
    sid = session_id or ""
    return f"{extension}_{phone}_{call_time}_{sid}"


def _parse_call_time(value: str) -> datetime:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime.now(timezone.utc)


async def start_workers(
    *,
    get_session_for_extension: Callable[[str], Awaitable[Optional[dict[str, Any]]]],
    query_cdr: Callable[..., Awaitable[Any]],
    download_recording: Callable[[str, Path], Awaitable[bool]],
) -> None:
    global _running, _tasks
    if _running:
        return
    _running = True
    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    for i in range(WORKER_COUNT):
        _tasks.append(
            asyncio.create_task(
                _worker_loop(i, get_session_for_extension, query_cdr, download_recording),
                name=f"kommo-worker-{i}",
            )
        )
    msg = f"[kommo_call_worker] Started {WORKER_COUNT} Kommo call upload workers"
    log.info(msg)
    print(msg, flush=True)


async def stop_workers() -> None:
    global _running, _tasks
    _running = False
    for t in _tasks:
        t.cancel()
    if _tasks:
        await asyncio.gather(*_tasks, return_exceptions=True)
    _tasks = []


async def _worker_loop(
    worker_id: int,
    get_session_for_extension: Callable[[str], Awaitable[Optional[dict[str, Any]]]],
    query_cdr: Callable[..., Awaitable[Any]],
    download_recording: Callable[[str, Path], Awaitable[bool]],
) -> None:
    while _running:
        try:
            job = kommo_jobs_db.claim_next_job(["queued", "waiting_recording"])
            if not job:
                await asyncio.sleep(1.0)
                continue
            print(
                f"[kommo_call_worker] worker {worker_id} claimed job {job['id']} "
                f"ext={job.get('extension')}",
                flush=True,
            )
            await _process_job(job, get_session_for_extension, query_cdr, download_recording)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            log.exception("worker %s error: %s", worker_id, exc)
            print(f"[kommo_call_worker] worker {worker_id} error: {exc}", flush=True)
            await asyncio.sleep(2.0)


async def _process_job(
    job: dict[str, Any],
    get_session_for_extension: Callable[[str], Awaitable[Optional[dict[str, Any]]]],
    query_cdr: Callable[..., Awaitable[Any]],
    download_recording: Callable[[str, Path], Awaitable[bool]],
) -> None:
    job_id = job["id"]
    payload = dict(job["payload"])
    extension = job["extension"]

    session = await get_session_for_extension(extension)
    if not session or not session.get("access_token"):
        kommo_jobs_db.update_job(job_id, status="failed", reason="Kommo session unavailable")
        print(f"[kommo_call_worker] job {job_id} failed: Kommo session unavailable", flush=True)
        return

    async def refresh_token() -> tuple[str, Optional[str]]:
        refreshed = await get_session_for_extension(extension, force_refresh=True)
        if refreshed and refreshed.get("access_token"):
            return refreshed["access_token"], refreshed.get("expires_at")
        return session["access_token"], session.get("expires_at")

    client = KommoCrmClient(
        session.get("subdomain") or "",
        session["access_token"],
        account_base_url=session.get("account_base_url"),
        acting_user_id=session.get("kommo_user_id"),
        token_refresher=refresh_token,
    )

    try:
        audio_path: Optional[str] = job.get("recording_path")
        upload_source: Optional[str] = None

        client_recording = bool(payload.get("client_recording_enabled"))
        connection_slot = (payload.get("connection_slot") or "main").lower()
        prefer_miko = connection_slot != "secondary" and not client_recording

        if client_recording and not audio_path:
            kommo_jobs_db.update_job(job_id, status="waiting_recording")
            waited = 0
            while waited < CLIENT_RECORDING_WAIT_SECONDS:
                await asyncio.sleep(2.0)
                waited += 2
                refreshed = kommo_jobs_db.get_job(job_id)
                if refreshed and refreshed.get("recording_path"):
                    audio_path = refreshed["recording_path"]
                    break
                if refreshed and refreshed.get("status") == "processing" and refreshed.get("recording_path"):
                    audio_path = refreshed["recording_path"]
                    break
            if not audio_path:
                kommo_jobs_db.update_job(
                    job_id,
                    status="failed",
                    reason="Client recording not received within timeout",
                )
                return

        if audio_path and Path(audio_path).is_file():
            upload_source = "client"
        elif prefer_miko:
            call_time = _parse_call_time(payload.get("call_time") or "")
            answer_time_raw = payload.get("answer_time")
            answer_time = _parse_call_time(answer_time_raw) if answer_time_raw else None
            print(
                f"[kommo_call_worker] job {job_id}: resolving Miko recording "
                f"phone={payload.get('phone')} ext={extension}",
                flush=True,
            )
            miko_path, _ = await resolve_miko_recording(
                query_cdr=query_cdr,
                download_recording=download_recording,
                extension=extension,
                phone=payload.get("phone") or "",
                call_time=call_time,
                was_answered=bool(payload.get("was_answered")),
                call_duration_sec=int(payload.get("duration_seconds") or 0),
                answer_time=answer_time,
                work_dir=RECORDINGS_DIR / job_id,
            )
            if miko_path:
                audio_path = miko_path
                upload_source = "miko_pbx"
                kommo_jobs_db.update_job(job_id, recording_path=miko_path, upload_source=upload_source)
            elif (
                bool(payload.get("enable_recording_upload", True))
                and bool(payload.get("was_answered"))
            ):
                retry = int(payload.get("pbx_recording_retry") or 0)
                if retry < MAX_PBX_RECORDING_JOB_RETRIES:
                    payload["pbx_recording_retry"] = retry + 1
                    wait_sec = min(120, 20 * (retry + 1))
                    kommo_jobs_db.update_job(
                        job_id,
                        status="waiting_recording",
                        payload_json=payload,
                        reason=f"Waiting for Miko recording (retry {retry + 1}/{MAX_PBX_RECORDING_JOB_RETRIES}, next in {wait_sec}s)",
                    )
                    msg = (
                        f"[kommo_call_worker] job {job_id}: PBX recording not ready, "
                        f"retry {retry + 1}/{MAX_PBX_RECORDING_JOB_RETRIES} in {wait_sec}s"
                    )
                    log.info(msg)
                    print(msg, flush=True)
                    await asyncio.sleep(wait_sec)
                    kommo_jobs_db.update_job(job_id, status="queued")
                    return
                log.warning("job %s: PBX recording not found after %s attempts", job_id, retry)
                print(
                    f"[kommo_call_worker] job {job_id}: PBX recording not found after {retry} attempts",
                    flush=True,
                )

        if (
            bool(payload.get("enable_recording_upload", True))
            and bool(payload.get("was_answered"))
            and not (audio_path and Path(audio_path).is_file())
        ):
            kommo_jobs_db.update_job(
                job_id,
                status="failed",
                reason="PBX recording not found or not ready",
            )
            print(f"[kommo_call_worker] job {job_id} failed: no PBX recording", flush=True)
            return

        if not bool(payload.get("enable_recording_upload", True)):
            audio_path = None

        outcome: ProcessCallOutcome = await client.process_call(
            payload.get("phone") or "",
            is_incoming=bool(payload.get("is_incoming")),
            duration_seconds=int(payload.get("duration_seconds") or 0),
            was_answered=bool(payload.get("was_answered")),
            audio_path=audio_path,
            call_time=_parse_call_time(payload.get("call_time") or ""),
            lead_id=payload.get("lead_id"),
            call_from_label=payload.get("call_from_label"),
            upload_source=upload_source,
        )

        final_source = upload_source or outcome.upload_source
        if outcome.success and outcome.upload_status == "uploaded":
            kommo_jobs_db.update_job(
                job_id,
                status="uploaded",
                lead_id=outcome.lead_id,
                upload_source=final_source,
                reason=outcome.reason,
            )
            print(
                f"[kommo_call_worker] job {job_id} uploaded lead_id={outcome.lead_id} "
                f"source={final_source}",
                flush=True,
            )
        elif outcome.upload_status == "not_uploaded":
            kommo_jobs_db.update_job(
                job_id,
                status="failed",
                lead_id=outcome.lead_id,
                upload_source=final_source,
                reason=outcome.reason or "Recording upload failed",
            )
            print(
                f"[kommo_call_worker] job {job_id} failed: {outcome.reason or 'Recording upload failed'}",
                flush=True,
            )
        else:
            kommo_jobs_db.update_job(
                job_id,
                status="failed",
                lead_id=outcome.lead_id,
                reason=outcome.reason or outcome.upload_status,
            )
            print(
                f"[kommo_call_worker] job {job_id} failed: {outcome.reason or outcome.upload_status}",
                flush=True,
            )
    except Exception as exc:
        log.exception("job %s failed: %s", job_id, exc)
        print(f"[kommo_call_worker] job {job_id} exception: {exc}", flush=True)
        kommo_jobs_db.update_job(job_id, status="failed", reason=str(exc))
    finally:
        await client.close()
        _cleanup_job_files(job_id)


def _cleanup_job_files(job_id: str) -> None:
    folder = RECORDINGS_DIR / job_id
    if folder.is_dir():
        shutil.rmtree(folder, ignore_errors=True)
