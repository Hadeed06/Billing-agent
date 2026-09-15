"""
Upload files (recording WAV + transcript JSON) to the PracticeEHR
`/v1/Document/Scribe/Recording` endpoint.
"""
import logging
import os
from typing import Optional

import httpx

from src.services.http_client import get_http_client, request_timeout

logger = logging.getLogger(__name__)


async def upload_file(
    *,
    file_bytes: bytes,
    file_path: str,
    filename: str,
    content_type: str,
    auth_token: str,
) -> bool:
    """
    POST a multipart/form-data upload to PracticeEHR.

    Returns True on HTTP 200, False otherwise.
    """
    base_url = os.getenv("PRACTICE_EHR_BASE_URL", "").rstrip("/")
    storage_account = os.getenv("PRACTICE_EHR_STORAGE_ACCOUNT", "")
    container = os.getenv("PRACTICE_EHR_CONTAINER", "documents")

    if not base_url or base_url == "REPLACE_ME":
        logger.error("PRACTICE_EHR_BASE_URL is not configured — skipping upload")
        return False
    if not auth_token:
        logger.error("No auth token available for PracticeEHR upload")
        return False

    url = f"{base_url}/v1/Document/Scribe/Recording"
    headers = {"Authorization": f"Bearer {auth_token}"}

    form_fields = {
        "Container": container,
        "StorageAccount": storage_account,
        "FilePath": file_path,
    }
    files = {"file": (filename, file_bytes, content_type)}

    try:
        client = get_http_client()
        resp = await client.post(
            url, headers=headers, data=form_fields, files=files,
            timeout=request_timeout(read=120),
        )

        if resp.status_code == 200:
            logger.info(f"✅ PracticeEHR upload OK: {file_path}")
            return True

        logger.error(
            f"❌ PracticeEHR upload failed ({resp.status_code}) for {file_path}: {resp.text[:500]}"
        )
        return False

    except Exception as e:
        logger.exception(f"❌ PracticeEHR upload exception for {file_path}: {e}")
        return False
