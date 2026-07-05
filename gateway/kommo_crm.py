"""Kommo / AmoCRM API v4 client for PBX Gateway process-call pipeline."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import mimetypes
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Awaitable, Optional

import httpx

log = logging.getLogger("kommo_crm")

AMO_MISSED_CALL_RESULT = "No Answer"
AMO_MISSED_CALL_STATUS = 6
_RATE_INTERVAL = 0.5
_last_request_at = 0.0


@dataclass
class ProcessCallOutcome:
    success: bool
    upload_status: str  # uploaded | not_uploaded | failed
    reason: Optional[str] = None
    lead_id: Optional[int] = None
    contact_id: Optional[int] = None
    upload_source: Optional[str] = None


@dataclass
class LeadScore:
    lead_id: int
    is_open: bool
    is_responsible: bool
    is_our_contact_with_phone: bool
    is_main: bool
    is_single_contact: bool
    is_primary: bool
    updated_at: datetime

    def sort_key(self) -> tuple:
        return (
            self.is_open,
            self.is_responsible,
            self.is_our_contact_with_phone,
            self.is_main,
            self.is_single_contact,
            self.is_primary,
            self.updated_at,
            self.lead_id,
        )


class KommoCrmClient:
    def __init__(
        self,
        subdomain: str,
        access_token: str,
        *,
        account_base_url: Optional[str] = None,
        acting_user_id: Optional[int] = None,
        token_refresher: Optional[Callable[[], Awaitable[tuple[str, Optional[str]]]]] = None,
    ) -> None:
        self.subdomain = subdomain.strip()
        self.access_token = access_token
        self.account_base_url = self._resolve_api_base(account_base_url, self.subdomain)
        self.acting_user_id = acting_user_id
        self._token_refresher = token_refresher
        self._drive_url: Optional[str] = None
        self._current_user_id: Optional[int] = None
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=30.0))
        # Drive multipart uploads can be slow; desktop uses 300s timeout + retries.
        self._upload_http = httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=30.0))

    @staticmethod
    def _resolve_api_base(account_base_url: Optional[str], subdomain: str) -> str:
        """Match desktop ``GetApiBaseUrl()`` — always ``…/api/v4``."""
        raw = (account_base_url or "").strip().rstrip("/")
        if raw:
            return raw if raw.endswith("/api/v4") else f"{raw}/api/v4"
        sub = (subdomain or "").strip().lower()
        if not sub:
            return ""
        if sub.startswith("http://") or sub.startswith("https://"):
            host = sub.rstrip("/")
        elif ".kommo.com" in sub or ".amocrm." in sub:
            host = f"https://{sub}"
        else:
            host = f"https://{sub}.kommo.com"
        return f"{host.rstrip('/')}/api/v4"

    def _default_api_base(self) -> str:
        return self._resolve_api_base(None, self.subdomain)

    async def close(self) -> None:
        await self._http.aclose()
        await self._upload_http.aclose()

    async def _rate_limit(self) -> None:
        global _last_request_at
        now = time.monotonic()
        wait = _RATE_INTERVAL - (now - _last_request_at)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_request_at = time.monotonic()

    async def _ensure_token(self, force_refresh: bool = False) -> None:
        if force_refresh and self._token_refresher:
            token, _ = await self._token_refresher()
            if token:
                self.access_token = token

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: Optional[dict] = None,
        content: Optional[bytes] = None,
        headers: Optional[dict] = None,
    ) -> httpx.Response:
        await self._ensure_token()
        await self._rate_limit()
        url = path if path.startswith("http") else f"{self.account_base_url.rstrip('/')}/{path.lstrip('/')}"
        hdrs = {"Authorization": f"Bearer {self.access_token}"}
        if headers:
            hdrs.update(headers)
        resp = await self._http.request(
            method, url, json=json_body, params=params, content=content, headers=hdrs
        )
        if resp.status_code == 401 and self._token_refresher:
            await self._ensure_token(force_refresh=True)
            hdrs["Authorization"] = f"Bearer {self.access_token}"
            resp = await self._http.request(
                method, url, json=json_body, params=params, content=content, headers=hdrs
            )
        return resp

    async def load_account_info(self) -> None:
        resp = await self._request("GET", "/account", params={"with": "drive_url"})
        if resp.status_code != 200:
            log.warning("GET /account failed: %s %s", resp.status_code, resp.text[:200])
            return
        data = resp.json()
        self._drive_url = (data.get("drive_url") or "").rstrip("/")
        self._current_user_id = data.get("current_user_id")
        if self.acting_user_id is None:
            self.acting_user_id = self._current_user_id

    def get_acting_user_id(self) -> Optional[int]:
        return self.acting_user_id or self._current_user_id

    @staticmethod
    def normalize_phone(phone: str) -> str:
        digits = re.sub(r"\D", "", phone or "")
        if len(digits) == 11 and digits.startswith("8"):
            digits = "7" + digits[1:]
        return digits

    @staticmethod
    def phone_search_variants(normalized: str) -> list[str]:
        if not normalized:
            return []
        variants = [normalized]
        if len(normalized) >= 10:
            variants.append(normalized[-10:])
        if len(normalized) == 11 and normalized.startswith("7"):
            variants.append("+" + normalized)
            variants.append("8" + normalized[1:])
        if len(normalized) == 11 and normalized.startswith("1"):
            variants.append("+" + normalized)
        if len(normalized) == 10 and not normalized.startswith("7"):
            variants.append("1" + normalized)
            variants.append("+1" + normalized)
        return list(dict.fromkeys(variants))

    async def find_contact_by_phone(self, phone: str) -> Optional[int]:
        normalized = self.normalize_phone(phone)
        if not normalized:
            return None
        for variant in self.phone_search_variants(normalized):
            resp = await self._request("GET", "/contacts", params={"query": variant})
            if resp.status_code == 204:
                continue
            if resp.status_code != 200:
                log.warning("contact search failed: %s", resp.status_code)
                return None
            contacts = (resp.json().get("_embedded") or {}).get("contacts") or []
            for c in contacts:
                cid = c.get("id")
                if not cid:
                    continue
                full = await self._get_contact(cid)
                if full and self._contact_has_phone(full, normalized):
                    return int(cid)
        return None

    async def lookup_contact_display(self, phone: str) -> dict[str, Any]:
        """Resolve CRM contact name (and ids) for a phone number — used by softphone caller ID."""
        contact_id = await self.find_contact_by_phone(phone)
        if not contact_id:
            return {"name": None, "contact_id": None, "lead_id": None}
        contact = await self._get_contact(contact_id)
        name = (contact.get("name") or "").strip() if contact else ""
        lead_id = await self.find_open_lead_for_contact(contact_id)
        return {
            "name": name or None,
            "contact_id": contact_id,
            "lead_id": lead_id,
        }

    async def _get_contact(self, contact_id: int) -> Optional[dict]:
        resp = await self._request("GET", f"/contacts/{contact_id}")
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 204:
            resp = await self._request(
                "GET",
                "/contacts",
                params={"filter[id][]": contact_id, "limit": 1},
            )
            if resp.status_code == 200:
                contacts = (resp.json().get("_embedded") or {}).get("contacts") or []
                return contacts[0] if contacts else None
        return None

    def _contact_has_phone(self, contact: dict, normalized: str) -> bool:
        for cf in (contact.get("custom_fields_values") or []):
            if (cf.get("field_code") or "").upper() != "PHONE":
                continue
            for v in cf.get("values") or []:
                val = self.normalize_phone(str(v.get("value") or ""))
                if val and (val == normalized or val.endswith(normalized[-10:])):
                    return True
        return False

    @staticmethod
    def _parse_ts(token: Any) -> datetime:
        if token is None:
            return datetime.min.replace(tzinfo=timezone.utc)
        if isinstance(token, int):
            return datetime.fromtimestamp(token, tz=timezone.utc)
        if isinstance(token, str):
            try:
                return datetime.fromisoformat(token.replace("Z", "+00:00"))
            except ValueError:
                pass
        return datetime.min.replace(tzinfo=timezone.utc)

    @staticmethod
    def _is_copy_lead(name: str) -> bool:
        n = (name or "").lower()
        return "копия" in n or "copy" in n

    async def find_open_lead_for_contact(self, contact_id: int) -> Optional[int]:
        resp = await self._request(
            "GET",
            f"/contacts/{contact_id}",
            params={"with": "leads", "limit": 50, "order[created_at]": "desc"},
        )
        if resp.status_code != 200:
            return None
        leads = (resp.json().get("_embedded") or {}).get("leads") or []
        open_leads: list[tuple[int, datetime]] = []
        for short in leads[:30]:
            lid = short.get("id")
            if not lid:
                continue
            name = short.get("name") or ""
            if self._is_copy_lead(name):
                continue
            closed = short.get("closed_at")
            is_open = closed is None
            if not is_open:
                continue
            created = self._parse_ts(short.get("created_at"))
            open_leads.append((int(lid), created))
        if not open_leads:
            return None
        open_leads.sort(key=lambda x: x[1], reverse=True)
        return open_leads[0][0]

    async def get_lead(self, lead_id: int) -> Optional[dict]:
        resp = await self._request("GET", f"/leads/{lead_id}")
        if resp.status_code != 200:
            return None
        return resp.json()

    @staticmethod
    def _call_note_params(
        is_missed: bool, call_from_label: Optional[str]
    ) -> tuple[Optional[str], Optional[int]]:
        caller_line = f"Call from {call_from_label.strip()}" if call_from_label and call_from_label.strip() else None
        if is_missed:
            if caller_line:
                return f"{AMO_MISSED_CALL_RESULT} · {caller_line}", AMO_MISSED_CALL_STATUS
            return AMO_MISSED_CALL_RESULT, AMO_MISSED_CALL_STATUS
        return caller_line, None

    async def process_call(
        self,
        phone: str,
        *,
        is_incoming: bool,
        duration_seconds: int,
        was_answered: bool,
        audio_path: Optional[str],
        call_time: Optional[datetime] = None,
        lead_id: Optional[int] = None,
        call_from_label: Optional[str] = None,
        upload_source: Optional[str] = None,
    ) -> ProcessCallOutcome:
        try:
            return await self._process_call_impl(
                phone,
                is_incoming=is_incoming,
                duration_seconds=duration_seconds,
                was_answered=was_answered,
                audio_path=audio_path,
                call_time=call_time,
                lead_id=lead_id,
                call_from_label=call_from_label,
                upload_source=upload_source,
            )
        except httpx.HTTPError as exc:
            log.exception("Kommo HTTP error during process_call")
            print(f"[kommo_crm] process_call HTTP error: {exc}", flush=True)
            return ProcessCallOutcome(
                success=False,
                upload_status="failed",
                reason=f"Kommo API error: {exc}",
            )
        except Exception as exc:
            log.exception("Unexpected error during process_call")
            print(f"[kommo_crm] process_call error: {exc}", flush=True)
            return ProcessCallOutcome(
                success=False,
                upload_status="failed",
                reason=str(exc),
            )

    async def _process_call_impl(
        self,
        phone: str,
        *,
        is_incoming: bool,
        duration_seconds: int,
        was_answered: bool,
        audio_path: Optional[str],
        call_time: Optional[datetime] = None,
        lead_id: Optional[int] = None,
        call_from_label: Optional[str] = None,
        upload_source: Optional[str] = None,
    ) -> ProcessCallOutcome:
        await self.load_account_info()
        call_time = call_time or datetime.now(timezone.utc)

        if lead_id:
            return await self._process_for_lead(
                lead_id,
                phone,
                is_incoming=is_incoming,
                duration_seconds=duration_seconds,
                was_answered=was_answered,
                audio_path=audio_path,
                call_time=call_time,
                call_from_label=call_from_label,
                upload_source=upload_source,
            )

        contact_id = await self.find_contact_by_phone(phone)
        if not contact_id:
            return ProcessCallOutcome(
                success=False,
                upload_status="not_uploaded",
                reason="Contact not found in Kommo",
            )

        open_lead = await self.find_open_lead_for_contact(contact_id)
        if open_lead:
            outcome = await self._process_for_lead(
                open_lead,
                phone,
                is_incoming=is_incoming,
                duration_seconds=duration_seconds,
                was_answered=was_answered,
                audio_path=audio_path,
                call_time=call_time,
                call_from_label=call_from_label,
                upload_source=upload_source,
            )
            outcome.contact_id = contact_id
            return outcome

        return await self._process_for_contact(
            contact_id,
            phone,
            is_incoming=is_incoming,
            duration_seconds=duration_seconds,
            was_answered=was_answered,
            audio_path=audio_path,
            call_time=call_time,
            call_from_label=call_from_label,
            upload_source=upload_source,
        )

    async def _process_for_lead(
        self,
        lead_id: int,
        phone: str,
        *,
        is_incoming: bool,
        duration_seconds: int,
        was_answered: bool,
        audio_path: Optional[str],
        call_time: datetime,
        call_from_label: Optional[str],
        upload_source: Optional[str],
    ) -> ProcessCallOutcome:
        has_recording = bool(audio_path and Path(audio_path).is_file() and Path(audio_path).stat().st_size > 0)
        is_missed = not was_answered and not has_recording

        if is_missed:
            cr, cs = self._call_note_params(True, call_from_label)
            ok = await self._add_call_note("leads", lead_id, phone, is_incoming, 0, False, None, call_time, cr, cs)
            return ProcessCallOutcome(
                success=ok,
                upload_status="uploaded" if ok else "failed",
                lead_id=lead_id,
                reason=None if ok else "Missed call note failed",
            )

        download_link = await self._attach_audio("leads", lead_id, audio_path)
        cr, cs = self._call_note_params(False, call_from_label)
        ok = await self._add_call_note(
            "leads",
            lead_id,
            phone,
            is_incoming,
            duration_seconds,
            was_answered,
            download_link,
            call_time,
            cr,
            cs,
        )
        status = "uploaded" if ok else "failed"
        if ok and not download_link and has_recording:
            status = "not_uploaded"
        return ProcessCallOutcome(
            success=ok,
            upload_status=status,
            lead_id=lead_id,
            reason=None if ok else "Failed to add call note",
            upload_source=upload_source if download_link else None,
        )

    async def _process_for_contact(
        self,
        contact_id: int,
        phone: str,
        *,
        is_incoming: bool,
        duration_seconds: int,
        was_answered: bool,
        audio_path: Optional[str],
        call_time: datetime,
        call_from_label: Optional[str],
        upload_source: Optional[str],
    ) -> ProcessCallOutcome:
        has_recording = bool(audio_path and Path(audio_path).is_file() and Path(audio_path).stat().st_size > 0)
        is_missed = not was_answered and not has_recording

        if is_missed:
            cr, cs = self._call_note_params(True, call_from_label)
            ok = await self._add_call_note("contacts", contact_id, phone, is_incoming, 0, False, None, call_time, cr, cs)
            return ProcessCallOutcome(
                success=ok,
                upload_status="uploaded" if ok else "failed",
                contact_id=contact_id,
                reason=None if ok else "Missed call note failed",
            )

        download_link = await self._attach_audio("contacts", contact_id, audio_path)
        cr, cs = self._call_note_params(False, call_from_label)
        ok = await self._add_call_note(
            "contacts",
            contact_id,
            phone,
            is_incoming,
            duration_seconds,
            was_answered,
            download_link,
            call_time,
            cr,
            cs,
        )
        status = "uploaded" if ok else "failed"
        if ok and not download_link and has_recording:
            status = "not_uploaded"
        return ProcessCallOutcome(
            success=ok,
            upload_status=status,
            contact_id=contact_id,
            reason=None if ok and download_link else ("Recording upload failed" if has_recording else None),
            upload_source=upload_source if download_link else None,
        )

    async def _attach_audio(self, entity: str, entity_id: int, audio_path: Optional[str]) -> Optional[str]:
        if not audio_path or not Path(audio_path).is_file():
            print("[kommo_crm] attach_audio: file missing", flush=True)
            return None
        file_uuid = await self._upload_file(audio_path)
        if not file_uuid:
            print("[kommo_crm] attach_audio: drive upload returned no uuid", flush=True)
            return None
        # Desktop order: upload → download link → attach (attach is best-effort).
        download_link = await self._get_download_link(file_uuid)
        if not download_link:
            print(f"[kommo_crm] attach_audio: no download link for uuid={file_uuid}", flush=True)
            return None
        resp = await self._request(
            "PUT",
            f"/{entity}/{entity_id}/files",
            json_body=[{"file_uuid": file_uuid}],
        )
        if resp.status_code not in (200, 201, 202, 204):
            log.warning("attach files failed: %s %s", resp.status_code, resp.text[:200])
            print(
                f"[kommo_crm] attach files HTTP {resp.status_code} (call note link still used)",
                flush=True,
            )
        else:
            print(f"[kommo_crm] attach files ok entity={entity}/{entity_id}", flush=True)
        return download_link

    async def _upload_drive_part(self, url: str, chunk: bytes) -> Optional[dict]:
        """Upload one Drive chunk with retries (matches desktop AmoCrmService)."""
        headers: dict[str, str] = {
            "Content-Type": "application/octet-stream",
            "Content-Length": str(len(chunk)),
        }
        # Presigned storage URLs break if we add Authorization (Kommo direct URLs need it).
        if "amocrm" in url or "kommo" in url:
            headers["Authorization"] = f"Bearer {self.access_token}"
        max_retries = 3
        for attempt in range(max_retries):
            try:
                resp = await self._upload_http.post(url, content=chunk, headers=headers)
                if resp.status_code in (200, 201):
                    return resp.json()
                log.warning(
                    "drive part upload HTTP %s (attempt %s/%s): %s",
                    resp.status_code,
                    attempt + 1,
                    max_retries,
                    resp.text[:200],
                )
                print(
                    f"[kommo_crm] drive part HTTP {resp.status_code} attempt {attempt + 1}",
                    flush=True,
                )
            except (
                httpx.RemoteProtocolError,
                httpx.ConnectError,
                httpx.ReadTimeout,
                httpx.WriteTimeout,
                httpx.NetworkError,
            ) as exc:
                log.warning(
                    "drive part upload error (attempt %s/%s): %s",
                    attempt + 1,
                    max_retries,
                    exc,
                )
                print(
                    f"[kommo_crm] drive part error attempt {attempt + 1}: {exc}",
                    flush=True,
                )
            if attempt < max_retries - 1:
                await asyncio.sleep(1.0 * (attempt + 1))
        return None

    async def _upload_file(self, file_path: str) -> Optional[str]:
        if not self._drive_url:
            await self.load_account_info()
        if not self._drive_url:
            return None
        path = Path(file_path)
        file_size = path.stat().st_size
        if file_size == 0:
            return None
        ext = path.suffix.lower()
        content_type = {
            ".mp3": "audio/mpeg",
            ".wav": "audio/wav",
            ".webm": "audio/webm",
        }.get(ext, mimetypes.guess_type(path.name)[0] or "audio/mpeg")

        session_resp = await self._request(
            "POST",
            f"{self._drive_url}/v1.0/sessions",
            json_body={
                "file_name": path.name,
                "file_size": file_size,
                "content_type": content_type,
            },
        )
        if session_resp.status_code != 200:
            log.warning("drive session failed: %s", session_resp.text[:200])
            print(
                f"[kommo_crm] drive session HTTP {session_resp.status_code}: {session_resp.text[:120]}",
                flush=True,
            )
            return None
        session = session_resp.json()
        upload_url = session.get("upload_url")
        max_part = int(session.get("max_part_size") or 524288)
        if not upload_url:
            return None

        print(
            f"[kommo_crm] drive upload start {path.name} size={file_size} parts~{(file_size + max_part - 1) // max_part}",
            flush=True,
        )

        uploaded = 0
        current_url = upload_url
        with path.open("rb") as fh:
            while uploaded < file_size:
                chunk = fh.read(min(max_part, file_size - uploaded))
                if not chunk:
                    break
                data = await self._upload_drive_part(current_url, chunk)
                if not data:
                    log.warning("drive part upload failed after retries at offset %s", uploaded)
                    return None
                uploaded += len(chunk)
                if uploaded < file_size:
                    current_url = data.get("next_url")
                    if not current_url:
                        log.warning("drive next_url missing after part upload")
                        return None
                else:
                    file_uuid = data.get("uuid") or data.get("file_uuid")
                    if file_uuid:
                        print(f"[kommo_crm] drive upload ok uuid={file_uuid}", flush=True)
                        return str(file_uuid)
                    log.warning("drive upload finished without uuid: %s", data)
                    return None
        return None

    async def _get_download_link(self, file_uuid: str) -> Optional[str]:
        if not self._drive_url:
            await self.load_account_info()
        if not self._drive_url:
            return None
        resp = await self._request("GET", f"{self._drive_url}/v1.0/files/{file_uuid}")
        if resp.status_code == 401 and self._token_refresher:
            await self._ensure_token(force_refresh=True)
            resp = await self._request("GET", f"{self._drive_url}/v1.0/files/{file_uuid}")
        if resp.status_code != 200:
            log.warning("drive file info HTTP %s: %s", resp.status_code, resp.text[:200])
            print(
                f"[kommo_crm] drive file info HTTP {resp.status_code}: {resp.text[:120]}",
                flush=True,
            )
            return None
        data = resp.json()
        link = ((data.get("_links") or {}).get("download") or {}).get("href")
        if not link:
            link = (data.get("download") or {}).get("href")
        if not link:
            link = data.get("href")
        if link:
            print(f"[kommo_crm] drive download link ok uuid={file_uuid}", flush=True)
        else:
            log.warning("drive download link missing in response: %s", str(data)[:300])
            print(f"[kommo_crm] drive download link missing for uuid={file_uuid}", flush=True)
        return link

    async def _add_call_note(
        self,
        entity: str,
        entity_id: int,
        phone: str,
        is_incoming: bool,
        duration_seconds: int,
        was_answered: bool,
        audio_link: Optional[str],
        call_time: datetime,
        call_result: Optional[str],
        call_status: Optional[int],
    ) -> bool:
        user_id = self.get_acting_user_id()
        if not user_id:
            log.warning("no acting kommo user id for call note")
            return False

        direction = "in" if is_incoming else "out"
        normalized = self.normalize_phone(phone)
        uniq_src = f"{entity}_{entity_id}_{direction}_{normalized}_{call_time.strftime('%Y%m%d%H%M%S')}"
        uniq = hashlib.sha256(uniq_src.encode()).hexdigest()[:16]
        note_type = "call_in" if is_incoming else "call_out"

        for try_result in (bool(call_result), False):
            params: dict[str, Any] = {
                "uniq": uniq,
                "duration": duration_seconds,
                "source": "Callspire",
                "phone": phone,
            }
            if audio_link:
                params["link"] = audio_link
            if try_result and call_result:
                params["call_result"] = call_result
                if call_status is not None:
                    params["call_status"] = call_status

            body = [
                {
                    "note_type": note_type,
                    "created_by": user_id,
                    "responsible_user_id": user_id,
                    "params": params,
                }
            ]
            resp = await self._request("POST", f"/{entity}/{entity_id}/notes", json_body=body)
            if resp.status_code in (200, 201):
                return True
            if try_result and resp.status_code == 400:
                continue
            log.warning("call note failed: %s %s", resp.status_code, resp.text[:300])
            return False
        return False
