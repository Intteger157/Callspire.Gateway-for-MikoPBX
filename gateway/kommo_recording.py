"""Resolve Miko PBX recordings for Kommo upload jobs."""

from __future__ import annotations

import logging
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger("kommo_recording")

PBX_CDR_RETRY_DELAYS = [0, 5, 15, 30, 60, 90, 120, 180]
MAX_PBX_RECORDING_JOB_RETRIES = 8
MAX_START_DIFF_SECONDS = 180
_COMMON_UTC_OFFSETS = (0, 3, 4, 2, 5, -5, -4, -6, 1, -3)

_pbx_utc_offset_hours: float = 0.0


def configure_pbx_timezone(offset_hours: float) -> None:
    global _pbx_utc_offset_hours
    _pbx_utc_offset_hours = float(offset_hours or 0)


def is_recording_usable(path: Optional[str]) -> bool:
    if not path:
        return False
    p = Path(path)
    try:
        return p.is_file() and p.stat().st_size > 0
    except OSError:
        return False


def is_pbx_recording_acceptable(
    *,
    was_answered: bool,
    call_duration_sec: int,
    answer_time: Optional[datetime],
    call_time: datetime,
    pbx_path: Optional[str],
    cdr_duration_sec: Optional[int],
) -> tuple[bool, Optional[str]]:
    if not is_recording_usable(pbx_path):
        return False, "PBX file missing or empty"

    compare_duration = call_duration_sec
    if was_answered and answer_time and call_duration_sec > 0:
        pre_answer = (answer_time - call_time).total_seconds()
        if 0 < pre_answer < call_duration_sec:
            compare_duration = max(1, call_duration_sec - int(round(pre_answer)))

    if cdr_duration_sec and cdr_duration_sec > 0 and compare_duration >= 3:
        diff = abs(cdr_duration_sec - compare_duration)
        tolerance = max(30, compare_duration // 2)
        if diff > tolerance:
            return False, (
                f"CDR duration {cdr_duration_sec}s vs talk ~{compare_duration}s "
                f"(diff {diff}s > tolerance {tolerance}s)"
            )

    if not was_answered or compare_duration < 5:
        return True, None

    try:
        pbx_bytes = Path(pbx_path).stat().st_size
        duration_for_size = cdr_duration_sec if cdr_duration_sec and cdr_duration_sec > 0 else compare_duration
        compressed = str(pbx_path).lower().endswith((".webm", ".mp3", ".ogg"))
        if compare_duration < 30:
            min_bps = 200 if compressed else 1500
            floor_bytes = 1500 if compressed else 8000
        else:
            min_bps = 400 if compressed else 4000
            floor_bytes = 4000 if compressed else 50_000
        min_bytes = max(floor_bytes, duration_for_size * min_bps)
        if pbx_bytes < min_bytes:
            return False, (
                f"PBX file too small ({pbx_bytes} bytes < min {min_bytes} "
                f"for ~{duration_for_size}s)"
            )
    except OSError:
        pass
    return True, None


def _parse_cdr_start(value: Any) -> Optional[datetime]:
    """Parse CDR start as naive local PBX wall clock (no timezone)."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    s = str(value).strip()
    if s.endswith("Z"):
        s = s[:-1]
    if "T" in s:
        s = s.replace("T", " ", 1)
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:26], fmt)
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.replace(tzinfo=None) if dt.tzinfo else dt
    except ValueError:
        return None


def _call_vs_cdr_diff_seconds(rec_time: datetime, call_time: datetime) -> float:
    """Seconds between UTC call_time (browser) and naive PBX-local CDR start."""
    ct = call_time.astimezone(timezone.utc) if call_time.tzinfo else call_time.replace(tzinfo=timezone.utc)
    if rec_time.tzinfo is not None:
        rt = rec_time.astimezone(timezone.utc)
        return abs((rt - ct).total_seconds())
    naive = rec_time
    if _pbx_utc_offset_hours:
        rt = (naive - timedelta(hours=_pbx_utc_offset_hours)).replace(tzinfo=timezone.utc)
        return abs((rt - ct).total_seconds())
    return min(
        abs((naive - timedelta(hours=oh)).replace(tzinfo=timezone.utc) - ct).total_seconds()
        for oh in _COMMON_UTC_OFFSETS
    )


def _digits_match(a: str, b: str) -> bool:
    """True if two digit strings refer to the same phone (handles +1 / 10 vs 11 digits)."""
    da = "".join(c for c in a if c.isdigit())
    db = "".join(c for c in b if c.isdigit())
    if not da or not db:
        return False
    ra = da[-10:] if len(da) >= 10 else da
    rb = db[-10:] if len(db) >= 10 else db
    return ra == rb or da.endswith(rb) or db.endswith(ra)


def pick_best_cdr_record(
    records: list[dict],
    *,
    phone: str,
    call_time: datetime,
    was_answered: bool,
    call_duration_sec: Optional[int],
) -> Optional[dict]:
    if call_time.tzinfo is None:
        call_time = call_time.replace(tzinfo=timezone.utc)

    best: Optional[dict] = None
    best_diff = float("inf")
    phone_digits = "".join(c for c in phone if c.isdigit())

    for r in records:
        if not (r.get("recording") or r.get("linkedid")):
            continue
        rec_time = _parse_cdr_start(r.get("start") or r.get("calldate"))
        if not rec_time:
            continue
        diff = _call_vs_cdr_diff_seconds(rec_time, call_time)
        if diff > MAX_START_DIFF_SECONDS:
            continue
        if was_answered:
            disp = (r.get("disposition") or "").upper()
            if disp and disp != "ANSWERED":
                continue
        if was_answered and call_duration_sec:
            try:
                cdr_dur = int(r.get("billsec") or r.get("duration") or 0)
            except (TypeError, ValueError):
                cdr_dur = 0
            if cdr_dur > 0:
                dur_diff = abs(cdr_dur - call_duration_sec)
                tolerance = max(30, call_duration_sec // 2)
                if dur_diff > tolerance:
                    continue
        dst = str(r.get("dst") or r.get("dst_num") or "")
        src = str(r.get("src") or r.get("src_num") or "")
        if phone_digits:
            if not _digits_match(dst, phone) and not _digits_match(src, phone):
                continue
        if diff < best_diff:
            best_diff = diff
            best = r
    return best


async def resolve_miko_recording(
    *,
    query_cdr: Callable[..., Any],
    download_recording: Callable[[str, Path], Any],
    extension: str,
    phone: str,
    call_time: datetime,
    was_answered: bool,
    call_duration_sec: int,
    answer_time: Optional[datetime] = None,
    work_dir: Optional[Path] = None,
) -> tuple[Optional[str], Optional[int]]:
    """Query CDR with retries and download best matching recording to a temp file."""
    work_dir = work_dir or Path(tempfile.gettempdir()) / "callspire_kommo_recordings"
    work_dir.mkdir(parents=True, exist_ok=True)

    for attempt, delay in enumerate(PBX_CDR_RETRY_DELAYS):
        if delay > 0:
            import asyncio
            await asyncio.sleep(delay)
            msg = f"[kommo_recording] CDR retry {attempt} for {phone} ext={extension}"
            log.info(msg)
            print(msg, flush=True)

        try:
            records = await query_cdr(
                ext=extension,
                limit=100,
            )
        except Exception as exc:
            log.warning("CDR query failed: %s", exc)
            print(f"[kommo_recording] CDR query failed: {exc}", flush=True)
            continue

        if isinstance(records, dict):
            records = records.get("data") or records.get("result") or []
        if not isinstance(records, list):
            records = []

        if attempt == 0:
            print(
                f"[kommo_recording] CDR query ext={extension} phone={phone} "
                f"rows={len(records)} pbx_utc_offset={_pbx_utc_offset_hours}",
                flush=True,
            )

        best = pick_best_cdr_record(
            records,
            phone=phone,
            call_time=call_time,
            was_answered=was_answered,
            call_duration_sec=call_duration_sec or None,
        )
        if not best:
            continue

        linked_id = best.get("linkedid") or best.get("linked_id")
        if not linked_id:
            continue
        try:
            cdr_duration = int(best.get("billsec") or best.get("duration") or 0) or None
        except (TypeError, ValueError):
            cdr_duration = None

        dest = work_dir / f"mikopbx_{linked_id}.mp3"
        try:
            ok = await download_recording(str(linked_id), dest)
        except Exception as exc:
            log.warning("recording download failed: %s", exc)
            print(f"[kommo_recording] download failed linkedid={linked_id}: {exc}", flush=True)
            continue
        if not ok or not dest.is_file():
            print(f"[kommo_recording] download empty linkedid={linked_id}", flush=True)
            continue

        acceptable, reason = is_pbx_recording_acceptable(
            was_answered=was_answered,
            call_duration_sec=call_duration_sec,
            answer_time=answer_time,
            call_time=call_time,
            pbx_path=str(dest),
            cdr_duration_sec=cdr_duration,
        )
        if acceptable:
            print(
                f"[kommo_recording] matched linkedid={linked_id} bytes={dest.stat().st_size}",
                flush=True,
            )
            return str(dest), cdr_duration
        log.info("PBX recording rejected: %s", reason)
        print(f"[kommo_recording] rejected linkedid={linked_id}: {reason}", flush=True)
        try:
            dest.unlink(missing_ok=True)
        except OSError:
            pass

    print(f"[kommo_recording] no recording for {phone} ext={extension}", flush=True)
    return None, None
