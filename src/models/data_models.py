from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, List, TYPE_CHECKING
from pydantic import BaseModel
import uuid

from src.config.insurance_config import config_manager

if TYPE_CHECKING:
    from src.services.azure.stt_service import AzureRealtimeSttService


class SimpleCallRequest(BaseModel):
    visit_id: str
    customer_id: str
    wait_for_initiated_ms: int | None = 2000


# Cap on CallState.conversation_history length. Growth is turn-based (not per
# audio frame), so a normal call stays far under this; only a pathologically
# long hold call trims its OLDEST turns. Readers only ever use the tail or the
# from-pivot slice, so trimming the front is behavior-neutral — add_history
# shifts denial_history_start to keep its index valid. (full_transcript is NOT
# capped: it's the transcript uploaded to the billing team and must stay whole.)
_MAX_CONVERSATION_HISTORY = 400


@dataclass
class CallState:
    """State management for active calls"""
    call_control_id: str
    status: str = "initiated"
    start_time: datetime = field(default_factory=datetime.now)
    websocket_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    azure_stt_session: Optional["AzureRealtimeSttService"] = None
    conversation_history: List[dict] = field(default_factory=list)

    # Request-provided IDs (from the frontend)
    visit_id: Optional[str] = None
    customer_id: Optional[str] = None

    # Test-mode call (started via /v1/Billing-Agent/Call/Test with inline
    # visit data — no Auth/Clinical lookups). The post-call pipeline runs
    # the outcome classification but LOGS what it would write instead of
    # touching any PracticeEHR endpoint.
    is_test: bool = False

    # Resolved from the Clinical API plan name at call creation. Used by
    # webhooks/stream to restore the insurance ContextVar when a new request
    # for this call arrives.
    insurance_name: Optional[str] = None

    # Telnyx session ID (for fetching the recording later)
    call_session_id: Optional[str] = None

    # Bearer token captured from the frontend request — reused for PracticeEHR upload
    auth_token: Optional[str] = None

    # Per-customer API key resolved from /v1/Clients/AuthClients at call start.
    # Reused by the Clinical API and the post-call Billing-Agent/Log call.
    api_key: Optional[str] = None

    # Visit data fetched from the Clinical API at call start.
    # Keys match the prompt placeholders: tax_id, npi, member_id, dob, member_name, dos
    # Required keys vary by insurance (see src/services/clinical/required_fields.py).
    visit_data: Optional[dict] = None

    # Per-customer agent assigned by the Support API (Next-Agent/IVR) at call
    # start. `agent_id` is the profile id; `agent_profile` is the full record
    # (name, voiceId, vendor, pitch, rate, emotion, number). We dial FROM its
    # `number`, and the persona name + TTS voice come from the profile — no
    # hardcoded/env defaults when a profile is present.
    agent_id: Optional[str] = None
    agent_profile: Optional[dict] = None

    # Billing-Agent/Log row id, created at call start with placeholder values
    # and PUT-updated with the final outcome at call end. Returned to the
    # frontend in /v1/Billing-Agent/Call so they can track the call. May be
    # None if the initial POST to Billing-Agent/Log failed — the call itself
    # still proceeds; post_call_upload falls back to a single POST in that case.
    ref_no: Optional[int] = None

    # Flat list of every utterance/action in the call.
    # Shape: [{"speaker": "ivr" | "agent", "text": "..."}]
    full_transcript: List[dict] = field(default_factory=list)

    claim_mode: bool = False
    debounce_seconds: float = None
    need_debounce_reset: bool = False

    # Aetna only: how many times the IVR has said it couldn't find the NPI.
    # A clipped first DTMF tone drops a digit, so we retry; after 2 retries we
    # give up (the NPI is genuinely not recognized) instead of looping.
    npi_not_found_count: int = 0

    segmentation_silence_ms: int = None
    need_segmentation_reset: bool = False

    # Reference to the currently-running debounce task in stream.py so
    # ensure_call_cleanup can cancel it when the call ends. Prevents late
    # STT finals from firing handle_user_speech for a call that's already
    # been cleaned out of active_calls (would otherwise crash with
    # KeyError on the first template placeholder).
    debounce_task: Optional[object] = None

    # Reference to the auto-hangup watchdog task started in orchestrate.py.
    # Stored so the denial pivot can cancel the claim-status timer and re-arm
    # a longer one (rep hold queues outlive the original budget).
    auto_hangup_task: Optional[object] = None

    # ── denial follow-up ────────────────────────────────────────────────────
    # Reason known UP FRONT (set by /v1/Billing-Agent/Denial-Follow-Up): the claim
    # is already known to be denied and the reason is seeded, so the call skips
    # reading/classifying the claim and goes straight to a representative the
    # moment the IVR starts the claim read-out.
    denial_upfront: bool = False
    # Claim id (ClaimStatusSeqNum) passed into the denial follow-up endpoint —
    # stored here and echoed back on the FollowupAgent/ClaimStatus write-back.
    claim_status_seq_num: Optional[int] = None
    # Which conversational phase this call is in:
    #   "claim_status" → the normal flow (default; behavior identical to today)
    #   "denial_ivr"   → post-pivot, navigating the IVR to reach a representative
    #   "denial_rep"   → talking to a live human representative
    phase: str = "claim_status"
    # Set once the call entered the denial flow (used by post-call/logging).
    denial_pivoted: bool = False
    # Denial-reason engine state (populated in later phases of the feature):
    # registry key of the classified reason, verbatim reason text, and the
    # per-question checklist {index: "OPEN"|"ASKED"|"ANSWERED"}.
    denial_reason_key: Optional[str] = None
    denial_reason_verbatim: Optional[str] = None
    # True while the reason is only a keyword GUESS that GPT hasn't confirmed
    # yet. GPT (the authoritative classifier) overrides a provisional guess;
    # once GPT confirms, this flips False and the reason sticks.
    denial_reason_provisional: bool = False
    denial_checklist: Optional[dict] = None
    # Background GPT reason-classification task — cancelled in cleanup step 0.
    denial_reason_task: Optional[object] = None
    # How many GPT reason-classification attempts have fired (hard cap 2).
    denial_gpt_attempts: int = 0
    # Index into conversation_history where the denial flow began — the rep
    # prompt's history block only shows entries from this point on (earlier
    # claim-status menu turns would be noise to the rep conversation).
    denial_history_start: int = 0

    # Hold-silence watchdog (rep phase): if the line goes quiet for a long
    # stretch (rep put us on hold and wandered off), the bot proactively says
    # "Hello, are you still there?" instead of sitting mute.
    last_activity_ts: Optional[float] = None
    hold_nudge_count: int = 0
    hold_watchdog_task: Optional[object] = None

    def __post_init__(self):
        if self.debounce_seconds is None:
            self.debounce_seconds = config_manager.get_debounce_seconds()

        if self.segmentation_silence_ms is None:
            self.segmentation_silence_ms = config_manager.get_segmentation_silence_ms()

    def add_history(self, entry: dict) -> None:
        """Append to conversation_history with a hard length cap so a very long
        call can't grow this list without bound. If we trim the oldest entries,
        shift denial_history_start by the same amount so it still points at the
        first post-pivot turn (it's an absolute index into this list)."""
        self.conversation_history.append(entry)
        overflow = len(self.conversation_history) - _MAX_CONVERSATION_HISTORY
        if overflow > 0:
            del self.conversation_history[:overflow]
            self.denial_history_start = max(0, self.denial_history_start - overflow)
