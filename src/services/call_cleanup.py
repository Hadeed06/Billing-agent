# services/call_cleanup.py
import logging
from typing import Dict, Any, Callable
import asyncio

from src.services.agent_profile.client import release_agent
from src.services.practice_ehr.post_call_upload import (
    snapshot_call_state,
    upload_call_artifacts,
)

logger = logging.getLogger(__name__)

# Strong references to in-flight post-call upload tasks. asyncio only holds weak
# references to tasks, so a bare create_task() result can be garbage-collected
# mid-flight ("Task was destroyed but it is pending"). We drop the ref when the
# task finishes. Concurrency itself is bounded inside upload_call_artifacts.
_pending_upload_tasks: set = set()


async def ensure_call_cleanup(
    call_control_id: str,
    *,
    reason: str,
    send_hangup: bool,
    active_calls: Dict[str, Any],
    claims_agent,
    stt_manager,
    hangup_call: Callable[[str], Any],
):
    """
    Unified, idempotent cleanup for a call.
    Safe to call from webhook, WebSocket finally, auto-hangup, or shutdown.
    - Ends claims session (if active)
    - Cleans Azure STT session
    - Optionally issues hangup to Telnyx
    - Removes call from active_calls
    """
    cs = active_calls.get(call_control_id)
    if not cs:
        return

    # lazily add guard fields to CallState
    if not hasattr(cs, "cleanup_lock"):
        cs.cleanup_lock = asyncio.Lock()
    if not hasattr(cs, "cleanup_done"):
        cs.cleanup_done = False

    async with cs.cleanup_lock:
        if cs.cleanup_done:
            return

        logger.info(f"🧹 Cleanup [{call_control_id}] due to: {reason}")

        # 0️⃣ cancel any pending stream.py debounce task so a late STT final
        # can't fire handle_user_speech AFTER we pop this call from
        # active_calls (was causing KeyError: 'tax_id' from empty visit_data
        # — see Task exception was never retrieved logs in App Insights).
        # Same for the denial-flow background tasks: the reason-classifier
        # task and the auto-hangup watchdog. Never cancel the task we are
        # currently running inside (auto_hangup itself calls this cleanup —
        # cancelling it here would kill the cleanup mid-flight).
        for task_attr in ("debounce_task", "denial_reason_task", "auto_hangup_task", "hold_watchdog_task"):
            try:
                task = getattr(cs, task_attr, None)
                if (
                    task is not None
                    and not task.done()
                    and task is not asyncio.current_task()
                ):
                    task.cancel()
                    logger.info(f"[{call_control_id}] cancelled pending {task_attr}")
            except Exception as e:
                logger.warning(f"[{call_control_id}] {task_attr} cancel error (ignored): {e}")

        # 1️⃣ stop claims flow
        try:
            await claims_agent.end_session(call_control_id)
        except Exception as e:
            logger.warning(f"[{call_control_id}] end_session error (ignored): {e}")

        # 2️⃣ stop/cleanup STT
        try:
            if getattr(cs, "azure_stt_session", None):
                stt_manager.remove_session(cs.websocket_id)
        except Exception as e:
            logger.warning(f"[{call_control_id}] STT cleanup error (ignored): {e}")

        # 3️⃣ release the per-customer agent back to available (before hangup).
        #     Best-effort; never blocks teardown on a failure.
        try:
            agent_id = getattr(cs, "agent_id", None)
            if agent_id:
                await release_agent(agent_id, getattr(cs, "auth_token", None))
        except Exception as e:
            logger.warning(f"[{call_control_id}] release_agent error (ignored): {e}")

        # 4️⃣ send hangup if needed
        if send_hangup and getattr(cs, "status", "") not in ("hangup", "ended"):
            try:
                await hangup_call(call_control_id)
            except Exception as e:
                logger.warning(f"[{call_control_id}] hangup_call error (ignored): {e}")

        # 5️⃣ snapshot state for the post-call upload BEFORE we drop it.
        #     Pass the cleanup reason so the upload task can tell whether the
        #     call completed normally or was cut short (auto_hangup/shutdown).
        upload_snapshot = snapshot_call_state(cs, reason=reason)

        # 6️⃣ clear flags and forget this call
        cs.claim_mode = False
        cs.cleanup_done = True

        active_calls.pop(call_control_id, None)

        logger.info(f"✅ Cleanup complete [{call_control_id}]")

    # 7️⃣ fire-and-forget upload of recording + transcript to PracticeEHR.
    # Runs outside the cleanup lock so it doesn't block call teardown. Keep a
    # strong reference until the task finishes so it isn't GC'd mid-flight.
    try:
        task = asyncio.create_task(upload_call_artifacts(upload_snapshot))
        _pending_upload_tasks.add(task)
        task.add_done_callback(_pending_upload_tasks.discard)
    except Exception as e:
        logger.warning(f"[{call_control_id}] failed to schedule post-call upload: {e}")


async def drain_pending_uploads(timeout: float = 8.0) -> None:
    """Wait (bounded) for in-flight post-call upload tasks to finish.

    Called on shutdown BEFORE the shared HTTP client is closed, so uploads
    scheduled during teardown aren't cut off — and don't wake from their initial
    sleep to find the shared client gone (which would make them build a fresh,
    never-closed one). Bounded by `timeout` so shutdown can't hang.
    """
    pending = [t for t in _pending_upload_tasks if not t.done()]
    if not pending:
        return
    logger.info(f"⏳ Draining {len(pending)} in-flight post-call upload(s) (timeout={timeout}s)")
    try:
        _, still_pending = await asyncio.wait(pending, timeout=timeout)
        if still_pending:
            logger.warning(
                f"⚠️ {len(still_pending)} post-call upload(s) did not finish before shutdown drain timeout"
            )
    except Exception as e:
        logger.warning(f"drain_pending_uploads error (ignored): {e}")
