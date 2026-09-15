"""
Fetch the WAV recording for a completed call from the Telnyx API.
Ports the C# reference implementation.
"""
import logging
from typing import Optional, Tuple

import httpx

from src.services.http_client import get_http_client, request_timeout

logger = logging.getLogger(__name__)

TELNYX_API_BASE = "https://api.telnyx.com/v2"


def _select_recording(recordings: list) -> Optional[dict]:
    """Pick the recording with the highest duration when multiple exist."""
    if not recordings:
        return None
    if len(recordings) == 1:
        return recordings[0]
    return max(
        recordings,
        key=lambda r: r.get("duration_millis") or 0,
    )


async def fetch_recording_wav(
    call_session_id: str,
    telnyx_api_key: str,
) -> Optional[Tuple[bytes, int]]:
    """
    Look up the recording for a call_session_id and download the .wav bytes.

    Returns (wav_bytes, duration_seconds) on success, or None if the recording
    couldn't be found / downloaded.
    """
    if not call_session_id or not telnyx_api_key:
        logger.error("fetch_recording_wav: missing call_session_id or API key")
        return None

    headers = {"Authorization": f"Bearer {telnyx_api_key}"}
    params = {"filter[call_session_id]": call_session_id}

    try:
        client = get_http_client()
        meta_resp = await client.get(
            f"{TELNYX_API_BASE}/recordings",
            headers=headers,
            params=params,
            timeout=request_timeout(read=60),
        )
        if meta_resp.status_code != 200:
            logger.error(
                f"Telnyx recordings lookup failed: {meta_resp.status_code} {meta_resp.text}"
            )
            return None

        data = meta_resp.json().get("data") or []
        logger.info(f"Telnyx returned {len(data)} recording(s) for session {call_session_id}")
        recording = _select_recording(data)
        if not recording:
            logger.warning(f"No Telnyx recording for session {call_session_id}")
            return None

        download_url = (recording.get("download_urls") or {}).get("wav")
        if not download_url:
            logger.warning(f"No WAV download URL in recording for session {call_session_id}")
            return None

        duration_ms = recording.get("duration_millis") or 0
        duration_seconds = duration_ms // 1000
        logger.info(f"Downloading WAV ({duration_seconds}s) from Telnyx")

        wav_resp = await client.get(download_url, timeout=request_timeout(read=120))
        if wav_resp.status_code != 200:
            logger.error(
                f"Telnyx WAV download failed: {wav_resp.status_code}"
            )
            return None

        return wav_resp.content, duration_seconds

    except Exception as e:
        logger.exception(f"Unexpected error fetching Telnyx recording: {e}")
        return None
