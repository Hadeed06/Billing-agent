# services/call_lifecycle.py
import asyncio
import logging
from typing import Dict

logger = logging.getLogger(__name__)

async def hangup_call(call_control_id: str, *, telnyx_client, TELNYX_BASE_URL: str, HEADERS: dict):
    try:
        resp = await telnyx_client.hangup(call_control_id, TELNYX_BASE_URL, HEADERS)
        status = getattr(resp, "status_code", 200)

        if 200 <= status < 300:
            logger.info(f"✅ Call hung up: {call_control_id}")
        elif status == 422:
            logger.info(f"ℹ️ Hangup ignored (already ended): {call_control_id}")
        else:
            logger.warning(f"⚠️ Hangup returned {status} for {call_control_id}")
    except Exception as e:
        logger.warning(f"⚠️ hangup exception for {call_control_id}: {e}")


async def auto_hangup(call_control_id: str, active_calls: Dict, ensure_call_cleanup, delay_seconds: int):
    """
    Wait `delay_seconds`, and if the call is still active, hang it up.
    `delay_seconds` is supplied by the caller from the insurance config.
    """
    await asyncio.sleep(delay_seconds)
    if call_control_id in active_calls:
        logger.info(f"⌛ Auto-hanging up call {call_control_id} after {delay_seconds} seconds")
        await ensure_call_cleanup(call_control_id, reason="auto_hangup", send_hangup=True)
