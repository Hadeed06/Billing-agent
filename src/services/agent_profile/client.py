"""
Per-customer agent profile (Support API).

Before an outbound call we ask the Support API which agent to use for this
customer, and we release that agent back to the pool when the call ends.

  GET /v1/Billing-Agent/{customerId}/Next-Agent/IVR   -> assign & return an agent
  GET /v1/Billing-Agent/Agent/{agentId}/Available      -> release the agent

The response `data` carries the agent config:
  { id, name, customerId, voiceId, status, emotion, pitch, rate,
    vendor, number, type, assignedCount }

For the IVR claim-status agent we use ONLY `number` (the per-customer FROM
number) and `id` (to release the agent at call end). The voice fields
(name/voiceId/vendor/pitch/rate) are for the future denial-follow-up agent
(Google TTS) — the IVR agent stays on Azure TTS and ignores them.

Auth: Bearer token only (same Support API + token as Billing-Agent/Log).
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from src.services.http_client import get_http_client, request_timeout

logger = logging.getLogger(__name__)


def _base_url() -> str:
    """Support API base (same host as Billing-Agent/Log, e.g. …/support)."""
    return os.getenv("BILLING_AGENT_LOG_BASE_URL", "").rstrip("/")


def to_e164(number) -> Optional[str]:
    """Normalize a bare US number (e.g. '2038848539') to E.164 ('+12038848539').
    Returns None if it can't be made into a plausible E.164 number."""
    if number is None:
        return None
    s = str(number).strip()
    if s.startswith("+"):
        return s
    digits = "".join(ch for ch in s if ch.isdigit())
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return None


def persona_name_parts(agent_profile: Optional[dict]) -> tuple[str, str]:
    """Split the assigned agent's full `name` into (first, last) for the prompt
    persona (the bot introduces + spells this name to a rep). Falls back to the
    DENIAL_AGENT_PERSONA_* env defaults only when there's no profile name
    (legacy / test-endpoint paths that don't fetch an agent)."""
    name = ((agent_profile or {}).get("name") or "").strip()
    if name:
        parts = name.split()
        return parts[0], (parts[-1] if len(parts) > 1 else "")
    return (
        os.getenv("DENIAL_AGENT_PERSONA_NAME", "Marcus"),
        os.getenv("DENIAL_AGENT_PERSONA_LAST_NAME", "Bell"),
    )


async def fetch_next_agent_ivr(customer_id, auth_token: str) -> Optional[dict]:
    """GET /v1/Billing-Agent/{customerId}/Next-Agent/IVR.

    Returns the agent profile dict (id, number, name, …) on success, or None if
    no agent is configured / the call fails. Never raises — callers treat None
    as "no agents configured for this practice".
    """
    base = _base_url()
    if not base or base == "REPLACE_ME":
        logger.error("BILLING_AGENT_LOG_BASE_URL not configured — cannot fetch agent profile")
        return None
    if not customer_id:
        logger.error("fetch_next_agent_ivr: no customer_id")
        return None
    if not auth_token:
        logger.error("fetch_next_agent_ivr: no auth token")
        return None

    url = f"{base}/v1/Billing-Agent/{customer_id}/Next-Agent/IVR"
    headers = {"Authorization": f"Bearer {auth_token}"}
    try:
        client = get_http_client()
        resp = await client.get(url, headers=headers, timeout=request_timeout(read=15))
    except Exception as e:
        logger.error(f"Next-Agent request failed: {e}")
        return None

    if not (200 <= resp.status_code < 300):
        logger.warning(f"Next-Agent returned {resp.status_code}: {resp.text[:200]!r}")
        return None
    try:
        body = resp.json()
    except Exception:
        logger.error("Next-Agent: invalid JSON response")
        return None

    if not body.get("succeeded"):
        logger.info(f"Next-Agent not succeeded: {body.get('message')!r}")
        return None
    data = body.get("data")
    if not data:
        logger.info("Next-Agent succeeded but no agent data returned")
        return None

    logger.info(
        f"🧑‍💼 Agent assigned for customer {customer_id}: "
        f"id={data.get('id')!r} number={data.get('number')!r} name={data.get('name')!r}"
    )
    return data


async def release_agent(agent_id, auth_token: str) -> bool:
    """GET /v1/Billing-Agent/Agent/{agentId}/Available — mark the agent available
    again at call end. Never raises; returns True on a 2xx."""
    if not agent_id:
        return False
    base = _base_url()
    if not base or base == "REPLACE_ME" or not auth_token:
        logger.warning(f"Cannot release agent {agent_id}: missing base URL or auth token")
        return False

    url = f"{base}/v1/Billing-Agent/Agent/{agent_id}/Available"
    headers = {"Authorization": f"Bearer {auth_token}"}
    try:
        client = get_http_client()
        resp = await client.get(url, headers=headers, timeout=request_timeout(read=15))
    except Exception as e:
        logger.warning(f"release_agent({agent_id}) request error (ignored): {e}")
        return False

    if 200 <= resp.status_code < 300:
        logger.info(f"✅ Released agent {agent_id} back to available")
        return True
    logger.warning(f"release_agent({agent_id}) returned {resp.status_code}: {resp.text[:200]!r}")
    return False
