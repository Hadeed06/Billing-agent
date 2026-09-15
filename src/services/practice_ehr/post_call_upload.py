"""
End-of-call orchestration:
  1) Wait briefly for Telnyx to finalize the recording
  2) Fetch recording from Telnyx
  3) Build JSON transcript from call_state
  4) Upload both to PracticeEHR with different UUIDs under the same folder
  5) Classify the final claim status via GPT (or "no claim" if none)
  6) POST the outcome to the Billing-Agent/Log endpoint
"""
import asyncio
import logging
import os
import uuid

from src.services.billing_log.classifier import classify_claim
from src.services.billing_log.denial_summarizer import summarize_denial
from src.services.billing_log.denial_reason_mapper import map_denial_reason
from src.services.billing_log.claim_status_client import (
    post_ivr_claim_status,
    post_followup_claim_status,
)
from src.services.billing_log.failure_classifier import (
    build_plain_transcript,
    classify_failure,
)
from src.services.billing_log.log_client import (
    post_billing_log,
    update_billing_log_row,
)
from src.services.practice_ehr.telnyx_recording import fetch_recording_wav
from src.services.practice_ehr.transcript_builder import build_transcript_json
from src.services.practice_ehr.uploader import upload_file
from src.utils.logging_config import shorten_call_id

logger = logging.getLogger(__name__)

RECORDING_AVAILABILITY_DELAY_S = 5

# Cleanup reasons that mean the call was cut short before completing —
# the captured data can't be trusted as a final claim status.
INCOMPLETE_REASONS = {"auto_hangup", "shutdown"}

# Set by main.py only when the IVR explicitly says there are no claims for the
# patient. This is the ONLY case that should be recorded as "no claim".
CLAIMS_NOT_FOUND_REASON = "claims: not found"

REQUEST_STATUS_SUCCESS = "call successful"
REQUEST_STATUS_FAILED = "call failed"

# Bound how many post-call uploads run concurrently. A hangup burst schedules
# one upload task per call (fire-and-forget); without a cap they pile up dozens
# of simultaneous outbound requests and exhaust SNAT ports. Excess tasks wait on
# the semaphore and drain as slots free — nothing is dropped.
_MAX_CONCURRENT_UPLOADS = int(os.getenv("MAX_CONCURRENT_POST_CALL_UPLOADS", "4"))
_upload_semaphore = asyncio.Semaphore(_MAX_CONCURRENT_UPLOADS)


async def upload_call_artifacts(snapshot: dict) -> None:
    """Concurrency-bounded entry point for the post-call upload.

    Callers fire this and forget it (one per hangup). The semaphore caps how
    many run at once so a burst can never flood outbound SNAT ports; queued
    tasks simply wait their turn.
    """
    async with _upload_semaphore:
        await _run_upload_call_artifacts(snapshot)


async def _determine_outcome(snapshot: dict, call_tag: str, errors: list) -> tuple:
    """Decide (request_status, claim_status, description) for a finished call.

    Extracted from the upload path so TEST-MODE calls run the exact same
    classification logic (nothing diverges between test and prod decisions);
    test mode just logs the result instead of writing it.
    """
    cleanup_reason = snapshot.get("cleanup_reason", "")
    finalized_claims = snapshot.get("finalized_claims", []) or []
    is_incomplete = cleanup_reason in INCOMPLETE_REASONS
    storage_note = f" [Storage issue: {'; '.join(errors)}. call_id={call_tag}]" if errors else ""
    # Discrete fields for the Ivr/ClaimStatus write-back (DcnIcn / AiDenialReason).
    # Only the successful claim classify fills them; "" everywhere else.
    claim_reference = ""
    denial_reason = ""

    # ── Denial follow-up pivot takes precedence over the claim classifier ────
    # If the call pivoted into the denial flow, we ALREADY confirmed live (rule
    # + GPT) that the final claim has a denied line and then talked to a rep.
    # The normal claim classifier reads the raw readout and can call a PARTIAL
    # denial "paid" (most lines paid) — which would hide the denial. The pivot
    # fact is authoritative: force DENIED.
    if snapshot.get("denial_pivoted"):
        from src.core.denials.denial_reasons import get_reason, OUT_OF_SCOPE_KEY
        from src.core.denials.context import questions_for_reason
        key = snapshot.get("denial_reason_key")
        verbatim = snapshot.get("denial_reason_verbatim") or ""
        reason = get_reason(key)
        if key == OUT_OF_SCOPE_KEY:
            reason_label = f"reason OUT OF AGENT SCOPE: {verbatim or 'unspecified'}"
        elif reason is not None:
            reason_label = f"{reason.display_name} [{reason.group_code} {'/'.join(reason.carc_codes)}]"
        else:
            reason_label = "denial reason not identified during the call"

        # Claim-readout summary (status + numbers + ICN) from the IVR readout.
        claim_readout_summary = ""
        if finalized_claims:
            try:
                claim_readout_summary = (await classify_claim(finalized_claims)).get("description", "")
            except Exception as e:
                logger.warning(f"denial-pivot claim summary failed (ignored): {e}")

        # Rep-conversation summary: WHY it denied and HOW to fix it, in the rep's
        # own words (fax/portal/deadline/primary carrier, per denied line). This
        # is the actionable part the plain reason label can't carry. Falls back
        # to just the claim readout if the rep summary can't be built.
        rep_summary = ""
        try:
            rep_transcript = build_plain_transcript(snapshot.get("full_transcript") or [])
            rep_summary = await summarize_denial(
                rep_transcript=rep_transcript,
                claim_readout_summary=claim_readout_summary,
                reason_label=reason_label,
                questions=questions_for_reason(key),
                call_tag=call_tag,
            )
        except Exception as e:
            logger.warning(f"denial rep summary failed (ignored): {e}")

        detail = rep_summary or claim_readout_summary
        detail = f" {detail}" if detail else ""

        claim_status = "denied"
        incomplete_note = " Call ended before the follow-up was complete." if is_incomplete else ""
        # "Successful" for a denial call means we actually OBTAINED the denial
        # reason. If we reached the rep but no reason could be captured (e.g. the
        # payer's system was down and the rep couldn't access the claim), the goal
        # wasn't met — flag it as failed so the billing team calls back instead of
        # assuming we have the details. (A registry key OR out-of-scope both count
        # as "reason obtained"; only None means we walked away with nothing.)
        reason_obtained = key is not None
        request_status = (
            REQUEST_STATUS_FAILED if (is_incomplete or not reason_obtained)
            else REQUEST_STATUS_SUCCESS
        )
        callback_note = (
            " No denial reason could be obtained on this call — a callback is needed."
            if (not reason_obtained and not is_incomplete) else ""
        )
        description = (
            f"DENIAL FOLLOW-UP — {reason_label}.{incomplete_note}{callback_note}{detail}"
        ).strip()
        if errors:
            description = f"{description} Storage issues: {'; '.join(errors)}."
        logger.info(
            f"🔀 Denial-pivoted call → claim_status='denied' (reason={key!r}, "
            f"request_status={request_status!r})"
        )
        # Denial path writes to FollowupAgent/ClaimStatus, not Ivr/ClaimStatus, so
        # the DcnIcn/AiDenialReason discrete fields don't apply here.
        return request_status, claim_status, description, claim_reference, denial_reason

    if is_incomplete:
        # Call was cut short (auto_hangup / shutdown). Even if some claims were
        # captured before the cut, we can't trust the summary — the TRUE
        # status is usually in the LAST claim of the call, and any partial
        # capture could mislead the billing team. Always mark as failure.
        request_status = REQUEST_STATUS_FAILED
        plain_transcript = build_plain_transcript(
            snapshot.get("full_transcript") or []
        )
        failure = await classify_failure(
            transcript=plain_transcript,
            cleanup_reason=cleanup_reason,
            call_tag=call_tag,
        )
        claim_status = failure["status"]           # usually "call failed"
        description = failure["description"]
        if errors:
            description = f"{description} Storage issues: {'; '.join(errors)}."
        logger.info(
            f"⚠️ Incomplete call ({cleanup_reason}) → claim_status={claim_status!r}"
        )

    elif finalized_claims:
        # We reached the claims flow and captured claim(s) → classify.
        # (Existing successful path — unchanged.)
        request_status = REQUEST_STATUS_SUCCESS
        result = await classify_claim(finalized_claims)
        claim_status = result["status"]
        description = result["description"] + storage_note
        claim_reference = result.get("claim_reference", "")
        denial_reason = result.get("denial_reason", "")

    elif cleanup_reason == CLAIMS_NOT_FOUND_REASON:
        # The IVR explicitly told us there are no claims for this patient.
        # (Existing successful path — unchanged.)
        request_status = REQUEST_STATUS_SUCCESS
        claim_status = "no claim"
        description = "No claims found for this patient." + storage_note

    else:
        # Any other outcome (ended-before-claims-flow, IVR verification failure,
        # agent-routed, or a no-claim that the real-time detector missed) →
        # run the classifier and trust its verdict.
        plain_transcript = build_plain_transcript(
            snapshot.get("full_transcript") or []
        )
        failure = await classify_failure(
            transcript=plain_transcript,
            cleanup_reason=cleanup_reason,
            call_tag=call_tag,
        )
        claim_status = failure["status"]           # "no claim" | "patient not found" | "call failed"
        description = failure["description"]
        # "no claim" = payer verified patient and told us no claim exists for
        # the DOS. That's a SUCCESSFUL call — the classifier caught what the
        # real-time detector missed. Everything else is a failure.
        request_status = (
            REQUEST_STATUS_SUCCESS if claim_status == "no claim"
            else REQUEST_STATUS_FAILED
        )
        if errors:
            description = f"{description} Storage issues: {'; '.join(errors)}."
        logger.info(
            f"⚠️ Classified (reason: {cleanup_reason!r}) → "
            f"claim_status={claim_status!r} request_status={request_status!r}"
        )

    return request_status, claim_status, description, claim_reference, denial_reason


async def _log_test_outcome(snapshot: dict, call_tag: str) -> None:
    """TEST MODE: run the real outcome classification, then LOG everything the
    prod pipeline would have written — recording/transcript upload, the
    Billing-Agent/Log PUT/POST, and the IVR/ClaimStatus PATCH — without
    touching any PracticeEHR endpoint."""
    request_status, claim_status, description, _claim_ref, _denial_reason = await _determine_outcome(
        snapshot, call_tag, errors=[]
    )
    transcript_lines = len(snapshot.get("full_transcript", []) or [])
    logger.info(
        "🧪 TEST MODE — post-call summary (NOTHING written to any endpoint):\n"
        f"    would-be requestStatus : {request_status!r}\n"
        f"    would-be claimStatus   : {claim_status!r}\n"
        f"    would-be description   : {description!r}\n"
        f"    transcript_lines={transcript_lines} claims={len(snapshot.get('finalized_claims') or [])} "
        f"cleanup_reason={snapshot.get('cleanup_reason')!r}\n"
        f"    denial_pivoted={snapshot.get('denial_pivoted')} "
        f"denial_reason_key={snapshot.get('denial_reason_key')!r}\n"
        f"    denial_reason_verbatim={snapshot.get('denial_reason_verbatim')!r}\n"
        f"    skipped: recording fetch/upload, transcript upload, "
        f"Billing-Agent/Log write, Ivr/ClaimStatus PATCH"
    )


async def _run_upload_call_artifacts(snapshot: dict) -> None:
    """
    Uploads the recording and transcript JSON for a finished call, then
    records the outcome in the Billing-Agent/Log DB.

    `snapshot` is a dict captured BEFORE active_calls cleanup so we don't
    depend on the CallState still being around.
    """
    call_session_id = snapshot.get("call_session_id")
    customer_id = snapshot.get("customer_id")
    visit_id = snapshot.get("visit_id")
    auth_token = snapshot.get("auth_token")
    api_key = snapshot.get("api_key")
    # RefNo reserved at call start by orchestrate.py. May be None if the
    # initial INSERT failed — in that case we fall back to a single POST.
    ref_no = snapshot.get("ref_no")
    finalized_claims = snapshot.get("finalized_claims", []) or []
    transcript_lines = len(snapshot.get("full_transcript", []) or [])
    cleanup_reason = snapshot.get("cleanup_reason", "")
    # Short call tag derived from the call_control_id — identical to the tag
    # in our log lines, so descriptions can be grepped straight to the logs.
    call_tag = shorten_call_id(snapshot.get("call_control_id") or "")

    # The call was cut short (timeout / shutdown) → data is incomplete and the
    # claim status can't be trusted. Mark the whole call as failed.
    is_incomplete = cleanup_reason in INCOMPLETE_REASONS

    # A call that captured no real interaction (no transcript lines, no claims)
    # has no useful recording — this is exactly the profile of the hangup-burst
    # calls whose recording fetches piled up outbound connections. Skip the
    # Telnyx fetch/upload for them; the outcome is still classified and logged
    # below, so the billing row is unaffected. (Never skip the explicit
    # "no claims found" success path.)
    no_interaction = transcript_lines <= 1 and not finalized_claims
    skip_recording = no_interaction and cleanup_reason != CLAIMS_NOT_FOUND_REASON

    logger.info(
        f"📤 post_call_upload START: customer_id={customer_id} visit_id={visit_id} "
        f"call_session_id={call_session_id} transcript_lines={transcript_lines} "
        f"claims={len(finalized_claims)} reason={cleanup_reason!r} incomplete={is_incomplete} "
        f"auth_token_present={bool(auth_token)} api_key_present={bool(api_key)}"
    )

    # TEST-MODE calls: classify + log the would-be writes, touch nothing.
    if snapshot.get("is_test"):
        await _log_test_outcome(snapshot, call_tag)
        return

    if not customer_id:
        logger.warning("post_call_upload: no customer_id, skipping")
        return
    if not visit_id:
        logger.warning("post_call_upload: no visit_id, skipping")
        return
    if not auth_token:
        logger.warning("post_call_upload: no auth_token, skipping")
        return

    folder = f"billing/agent/claim/{customer_id}/{visit_id}"
    errors: list[str] = []

    # Give Telnyx a few seconds to save the recording on its side.
    logger.info(f"⏳ Sleeping {RECORDING_AVAILABILITY_DELAY_S}s for Telnyx to finalize recording")
    await asyncio.sleep(RECORDING_AVAILABILITY_DELAY_S)

    # 1) Recording ───────────────────────────────────────────────────────────
    recording_path = ""
    telnyx_api_key = os.getenv("TELNYX_API_KEY", "")
    if skip_recording:
        logger.info(
            "⏭️ Skipping Telnyx recording fetch — call captured no interaction "
            f"(transcript_lines={transcript_lines}, claims={len(finalized_claims)})"
        )
    elif call_session_id:
        logger.info(f"🎙 Fetching Telnyx recording for session {call_session_id}")
        result = await fetch_recording_wav(call_session_id, telnyx_api_key)
        if result:
            wav_bytes, duration = result
            recording_uuid = str(uuid.uuid4())
            candidate_path = f"{folder}/{recording_uuid}.wav"
            logger.info(
                f"📥 Got recording: {len(wav_bytes)} bytes, {duration}s — uploading to {candidate_path}"
            )
            ok = await upload_file(
                file_bytes=wav_bytes,
                file_path=candidate_path,
                filename=f"{recording_uuid}.wav",
                content_type="audio/wav",
                auth_token=auth_token,
            )
            if ok:
                recording_path = candidate_path
            else:
                errors.append("recording upload failed")
        else:
            errors.append("could not fetch Telnyx recording")
    else:
        errors.append("missing call_session_id")
        logger.warning("post_call_upload: no call_session_id, skipping recording")

    # 2) Transcript ──────────────────────────────────────────────────────────
    transcript_path = ""
    transcript_bytes = build_transcript_json_from_snapshot(snapshot)
    transcript_uuid = str(uuid.uuid4())
    candidate_path = f"{folder}/{transcript_uuid}.json"
    logger.info(
        f"📝 Uploading transcript ({len(transcript_bytes)} bytes, {transcript_lines} lines) to {candidate_path}"
    )
    ok = await upload_file(
        file_bytes=transcript_bytes,
        file_path=candidate_path,
        filename=f"{transcript_uuid}.json",
        content_type="application/json",
        auth_token=auth_token,
    )
    if ok:
        transcript_path = candidate_path
    else:
        errors.append("transcript upload failed")

    # 3) Determine outcome → requestStatus / claimStatus / description ─────────
    # _determine_outcome also returns the discrete claim_reference + denial_reason
    # (from the successful claim classify) for the Ivr/ClaimStatus write-back.
    request_status, claim_status, description, claim_reference, denial_reason = await _determine_outcome(
        snapshot, call_tag, errors
    )
    # Canonical denial category (one of the 13 enum keys) mapped below; "" when it
    # doesn't clearly match — then only the raw reason is sent.
    denial_category = ""

    # Map the raw denial reason to one of the 13 canonical categories (no forcing;
    # "" when it doesn't clearly match — then only the raw reason is sent).
    if denial_reason:
        denial_category = await map_denial_reason(denial_reason) or ""

    # Discrete fields extracted from the IVR claim readout (for denial follow-up).
    # Sent to IVR/ClaimStatus below as: DcnIcn (claim_reference), AiDenialReason
    # (raw denial_reason), DenialCategory (the enum key, or "" when unmatched).
    # Each is "" when absent — we send whatever we have.
    logger.info(
        f"🧾 Extracted fields → claim_reference={claim_reference!r} "
        f"denial_reason={denial_reason!r} denial_category={denial_category!r}"
    )

    # 4) Update Billing-Agent/Log ────────────────────────────────────────────
    # Preferred path: PUT the row created at call start (RefNo from snapshot).
    # Fallback path: if the initial INSERT failed (ref_no is None), do a single
    # POST so the call is still logged — the frontend won't have a RefNo to
    # display, but the DB record + downstream IVR/ClaimStatus still happen.
    data_id = None
    if ref_no:
        updated = await update_billing_log_row(
            ref_no=ref_no,
            request_status=request_status,
            claim_status=claim_status,
            description=description,
            transcript_path=transcript_path,
            recording_path=recording_path,
            auth_token=auth_token,
        )
        if updated:
            data_id = ref_no
        else:
            logger.error(
                f"❌ PUT Billing-Agent/Log/{ref_no} failed — row stays at 'in_progress'. "
                f"Manual cleanup may be needed."
            )
    else:
        logger.warning(
            "No RefNo from call start — falling back to single POST to Billing-Agent/Log"
        )
        # Match the tags the initial INSERT would have used (see orchestrate.py):
        # denial follow-up rows are DENIAL_FOLLOW_UP / AI-Denial-Follow-Up-Agent,
        # everything else stays CLAIM_STATUS / AI-Billing-Agent.
        is_denial = bool(snapshot.get("denial_pivoted"))
        data_id = await post_billing_log(
            request_type="DENIAL_FOLLOW_UP" if is_denial else "CLAIM_STATUS",
            transcript_path=transcript_path,
            recording_path=recording_path,
            request_status=request_status,
            claim_status=claim_status,
            description=description,
            visit_seq_num=visit_id,
            customer_id=customer_id,
            entered_by="AI-Denial-Follow-Up-Agent" if is_denial else "AI-Billing-Agent",
            payer_name=snapshot.get("insurance_name") or "",
            auth_token=auth_token,
        )

    if data_id is not None:
        logger.info(f"📊 Billing-Agent/Log data id: {data_id}")

        # 5) Final status update ──────────────────────────────────────────────
        # RefNo is the Billing-Agent/Log row id (data_id). Denial follow-up calls
        # write to FollowupAgent/ClaimStatus (with the claim id as ClaimStatusSeqNum);
        # every other call keeps using Ivr/ClaimStatus. Sent whenever we have a
        # claim_status (success and failure cases both).
        if claim_status and snapshot.get("denial_pivoted"):
            await post_followup_claim_status(
                visit_seq_num=visit_id,
                ref_no=data_id,
                summary=description,
                status=claim_status,
                customer_id=customer_id,
                claim_status_seq_num=snapshot.get("claim_status_seq_num"),
                bearer_token=auth_token,
            )
        elif claim_status:
            await post_ivr_claim_status(
                visit_seq_num=visit_id,
                ref_no=data_id,
                summary=description,
                status=claim_status,
                customer_id=customer_id,
                bearer_token=auth_token,
                claim_status_seq_num=snapshot.get("claim_status_seq_num"),
                dcn_icn=claim_reference,          # "" when the IVR didn't state a claim number
                # AiDenialReason: the mapped category (one of the 13 enum keys) when
                # matched, else the RAW reason. "" when not denied / no reason given.
                ai_denial_reason=(denial_category or denial_reason),
            )
        else:
            logger.info(
                f"ℹ️ Skipping ClaimStatus write-back — no claim_status "
                f"(request_status={request_status!r})"
            )
    else:
        logger.warning("No RefNo from Billing-Agent/Log — skipping ClaimStatus write-back")

    logger.info("✅ post_call_upload DONE")


def build_transcript_json_from_snapshot(snapshot: dict) -> bytes:
    """Helper to reuse build_transcript_json against a snapshot dict."""
    class _Shim:
        pass
    shim = _Shim()
    shim.full_transcript = snapshot.get("full_transcript", [])
    return build_transcript_json(shim)


def snapshot_call_state(call_state, reason: str = "") -> dict:
    """Capture the minimum data needed for a background upload.
    Taken AFTER claims_agent.end_session(), so finalized_claims is populated.
    `reason` is the cleanup reason (auto_hangup / webhook: call.hangup / ...)."""
    visit_data = getattr(call_state, "visit_data", None) or {}
    return {
        "call_control_id": getattr(call_state, "call_control_id", None),
        "call_session_id": getattr(call_state, "call_session_id", None),
        "customer_id": getattr(call_state, "customer_id", None),
        "visit_id": getattr(call_state, "visit_id", None),
        "auth_token": getattr(call_state, "auth_token", None),
        "api_key": getattr(call_state, "api_key", None),
        # RefNo reserved at call start. None if initial INSERT failed →
        # post_call_upload will fall back to a single POST.
        "ref_no": getattr(call_state, "ref_no", None),
        "full_transcript": list(getattr(call_state, "full_transcript", []) or []),
        "finalized_claims": list(getattr(call_state, "finalized_claims", []) or []),
        # Insurance name (CIGNA/HUMANA/...) — sent as payerName in the Log payload.
        "insurance_name": getattr(call_state, "insurance_name", None),
        # Plan name from Clinical API — held for future use.
        "plan_short_name": visit_data.get("plan_short_name"),
        "plan_description": visit_data.get("plan_description"),
        # How the call ended — drives the success/failed outcome.
        "cleanup_reason": reason,
        # Test-mode flag: classification runs, but nothing is written to any
        # PracticeEHR endpoint — the would-be writes are logged instead.
        "is_test": bool(getattr(call_state, "is_test", False)),
        # Denial follow-up outcome (for logging now; persistence in a later phase).
        "denial_pivoted": bool(getattr(call_state, "denial_pivoted", False)),
        "denial_reason_key": getattr(call_state, "denial_reason_key", None),
        "denial_reason_verbatim": getattr(call_state, "denial_reason_verbatim", None),
        # Claim id echoed back on the FollowupAgent/ClaimStatus write-back.
        "claim_status_seq_num": getattr(call_state, "claim_status_seq_num", None),
    }
