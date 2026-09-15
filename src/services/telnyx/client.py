# services/telnyx_client.py
from __future__ import annotations
import httpx
from typing import Any, Dict

from src.services.http_client import get_http_client, request_timeout

async def create_call_raw(call_payload: Dict[str, Any], base_url: str, headers: Dict[str, str]) -> httpx.Response:
    client = get_http_client()
    return await client.post(
        f"{base_url}/calls", json=call_payload, headers=headers,
        timeout=request_timeout(read=15),
    )

async def send_dtmf(call_control_id: str, digits: str, base_url: str, headers: Dict[str, str]) -> None:
    url = f"{base_url}/calls/{call_control_id}/actions/send_dtmf"
    payload = {"digits": digits}
    client = get_http_client()
    await client.post(url, json=payload, headers=headers, timeout=request_timeout(read=10))

async def hangup(call_control_id: str, base_url: str, headers: Dict[str, str]) -> None:
    url = f"{base_url}/calls/{call_control_id}/actions/hangup"
    client = get_http_client()
    await client.post(url, json={}, headers=headers, timeout=request_timeout(read=10))
