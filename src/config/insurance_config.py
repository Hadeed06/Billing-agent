# insurance_config.py
from __future__ import annotations

import contextvars
from dataclasses import dataclass, replace
from typing import Dict, Optional


@dataclass
class InsuranceConfig:
    """Configuration for each insurance provider"""
    name: str
    phone_number: str
    debounce_seconds: float
    claim_debounce_seconds: float
    claims_tail_chars: int
    prompt_template: str
    claims_prompt_template: str
    segmentation_silence_ms: int  # For normal flow
    claim_segmentation_silence_ms: int  # For claim flow
    auto_hangup_seconds: int  # Force-hangup if the call runs longer than this
    # When True, skip a GPT claims-controller call if the new chunk is near-
    # identical to the last chunk we sent to GPT (handles STT trailing-char
    # races that caused duplicate "Details"/"Next claim" responses on CIGNA).
    dedupe_chunks: bool = False

    # When True, suppress an IDENTICAL DTMF press repeated within a short window
    # (see _DTMF_DEDUPE_WINDOW_S in main.py). Handles a menu the IVR speaks in two
    # STT chunks, which makes GPT answer the SAME menu twice. Opt-in per insurer
    # so working payers are unaffected.
    dedupe_dtmf: bool = False

    # ── denial follow-up (in-call pivot) ────────────────────────────────────
    # When True AND the request opted in (denial_follow_up=true), a claim
    # that is detected as DENIED during the claim readout pivots the SAME
    # call into the denial flow (ask for a representative, gather denial
    # details) instead of hanging up. Enable per payer only AFTER the
    # "Representative" pivot has been tested against that payer's IVR.
    supports_denial_inquiry: bool = False
    # Timings for the live-representative phase. Humans talk slower and
    # pause more than IVR menus, so both are looser than the IVR baseline.
    denial_rep_debounce_seconds: float = 1.5
    denial_rep_segmentation_silence_ms: int = 1500
    # Replacement auto-hangup budget (seconds) armed at the pivot moment —
    # rep hold queues run long, so the original claim-status timer would
    # cut the call mid-hold. None → fall back to auto_hangup_seconds.
    denial_auto_hangup_seconds: Optional[int] = None
    # Phrase spoken at the pivot to request a live human. Humana reaches a rep
    # by saying "Representative"; Cigna reaches one via "customer service
    # advocate". Payer-specific — default keeps Humana/others unchanged.
    denial_ivr_request_phrase: str = "Representative"
    # Some payers reach a human via a KEYPAD press off the post-claim menu rather
    # than a spoken phrase. Oscar: press 2 for denied-line details (which reads
    # the reason, or auto-transfers to a rep if there are none); the denial-IVR
    # prompt then presses 3 for the rep. When set, the pivot sends this DTMF.
    denial_ivr_request_dtmf: Optional[str] = None

# All insurance configurations
INSURANCE_CONFIGS: Dict[str, InsuranceConfig] = {
    "CIGNA": InsuranceConfig(
        name="CIGNA",
        phone_number="+18009971654",
        debounce_seconds=0.8,
        claim_debounce_seconds=2,
        claims_tail_chars=300,
        prompt_template="CIGNA_PROMPT_TEMPLATE",
        claims_prompt_template="CIGNA_CLAIMS_CONTROLLER_TEMPLATE",
        segmentation_silence_ms=600,
        claim_segmentation_silence_ms=1700,
        auto_hangup_seconds=900,
        dedupe_chunks=True,
        # In-call denial pivot. Cigna's verbose IVR usually reads the denial
        # reason aloud, so the reason is often classified from the readout
        # before the advocate even picks up. Reach a human via "customer
        # service advocate". Rep-phase timings mirror Humana (humans pause
        # more than the IVR menu).
        # TEMPORARILY DISABLED — only Humana ships the denial agent to prod for
        # now. Flip back to True (and QA it) before enabling Cigna.
        supports_denial_inquiry=False,
        denial_rep_debounce_seconds=1.5,
        denial_rep_segmentation_silence_ms=1600,
        denial_auto_hangup_seconds=1800,
        denial_ivr_request_phrase="customer service advocate",
    ),

    "HUMANA": InsuranceConfig(
        name="HUMANA",
        phone_number="+18007834599",
        debounce_seconds=0.4,
        claim_debounce_seconds=1.2,
        claims_tail_chars=150,
        prompt_template="HUMANA_PROMPT_TEMPLATE",
        claims_prompt_template="HUMANA_CLAIMS_CONTROLLER_TEMPLATE",
        segmentation_silence_ms=500,
        claim_segmentation_silence_ms=1300,
        auto_hangup_seconds=1600,
        # First payer wired for the in-call denial pivot (values validated
        # on the old denial-inquiry demo calls).
        supports_denial_inquiry=True,
        # 1.2s (was 2s) — trims dead air before the bot replies. Segmentation
        # (1600ms) already ensures the rep paused before the STT final, so this
        # extra post-final wait can be shorter without cutting the rep off.
        denial_rep_debounce_seconds=1.2,
        # Longer STT segmentation so a rep who pauses mid-sentence isn't cut
        # into fragments — captures fuller sentences before the bot responds,
        # so it stops interrupting and stops double-replying to split chunks.
        denial_rep_segmentation_silence_ms=1600,
        denial_auto_hangup_seconds=1800,
    ),

    "BAYLOR_SCOTT": InsuranceConfig(
        name="BAYLOR_SCOTT",
        phone_number="+18555727238",
        debounce_seconds=0.1,
        claim_debounce_seconds=1.5,
        claims_tail_chars=200,
        prompt_template="BAYLOR_SCOTT_PROMPT_TEMPLATE",
        claims_prompt_template="BAYLOR_SCOTT_CLAIMS_CONTROLLER_TEMPLATE",
        segmentation_silence_ms=600,
        claim_segmentation_silence_ms=1000,
        auto_hangup_seconds=1600,
        # In-call denial pivot. Baylor reaches a live human by saying
        # "Representative" (like Humana); the IVR then confirms the transfer
        # ("...the representative or the customer service advocate?") which the
        # denial-IVR template answers with confirm:yes. Rep-phase timings mirror
        # Humana/Cigna (humans pause more than the IVR menu).
        # TEMPORARILY DISABLED — only Humana ships the denial agent to prod for
        # now. Flip back to True (and QA it) before enabling Baylor Scott.
        supports_denial_inquiry=False,
        # Snappier rep turns — 1.0s wait after the rep stops before we reply
        # (was 1.5s). Segmentation stays at 1600ms so we still capture a full
        # sentence; this only trims the post-speech pause.
        denial_rep_debounce_seconds=1.0,
        denial_rep_segmentation_silence_ms=1600,
        denial_auto_hangup_seconds=1800,
        denial_ivr_request_phrase="Representative",
    ),

    "OSCAR": InsuranceConfig(
        name="OSCAR",
        phone_number="+18556722755",
        debounce_seconds=0.0,
        claim_debounce_seconds=1.2,
        claims_tail_chars=250,
        prompt_template="OSCAR_PROMPT_TEMPLATE",
        claims_prompt_template="OSCAR_CLAIMS_CONTROLLER_TEMPLATE",
        segmentation_silence_ms=1400,
        claim_segmentation_silence_ms=1800,
        auto_hangup_seconds=1600,
        # In-call denial pivot. Oscar reaches a rep via the post-claim KEYPAD
        # menu (no spoken phrase): the pivot presses 2 for denied-line details
        # (reads the reason, or auto-transfers to a rep if there are none), and
        # the denial-IVR prompt then presses 3 for the representative.
        # TEMPORARILY DISABLED — only Humana ships the denial agent to prod for
        # now. Flip back to True (and QA it) before enabling Oscar.
        supports_denial_inquiry=False,
        denial_rep_debounce_seconds=1.0,
        denial_rep_segmentation_silence_ms=1600,
        denial_auto_hangup_seconds=1800,
        denial_ivr_request_dtmf="2",
    ),

    # UnitedHealthcare — reachable on the production /v1/Billing-Agent/Call
    # endpoint via payer_id 87726 (see PAYER_ID_TO_INSURANCE below). Requires
    # the Clinical API to return UHC visit data (tax_id, npi, member_id,
    # member_name, dob, dos).
    "UHC": InsuranceConfig(
        name="UHC",
        phone_number="+18778423210",
        debounce_seconds=0.5,
        claim_debounce_seconds=1.1,
        claims_tail_chars=250,
        prompt_template="UHC_PROMPT_TEMPLATE",
        claims_prompt_template="UHC_CLAIMS_CONTROLLER_TEMPLATE",
        segmentation_silence_ms=800,
        claim_segmentation_silence_ms=1100,
        auto_hangup_seconds=1200,
    ),

    # Aetna — reached via payer_id 60054. All-keypad IVR (like Oscar): NPI,
    # menu selections, Aetna member ID, DOB (mmddyyyy), DOS (mmddyyyy) and every
    # confirmation (press 1=yes / 2=no) are entered on the KEYPAD, so the prompt
    # uses dtmf: throughout. After it reads the one-line claim summary it offers
    # "Hear claim details or press 2" — the claims controller presses 2 to get
    # the full payment breakdown, then stops.
    "AETNA": InsuranceConfig(
        name="AETNA",
        phone_number="+18006240756",  # Aetna provider claim-status line (800-624-0756)
        debounce_seconds=0.3,
        claim_debounce_seconds=1.5,
        claims_tail_chars=300,
        prompt_template="AETNA_PROMPT_TEMPLATE",
        claims_prompt_template="AETNA_CLAIMS_CONTROLLER_TEMPLATE",
        segmentation_silence_ms=1100,
        claim_segmentation_silence_ms=1500,
        auto_hangup_seconds=1200,
        # Aetna is all-keypad and presses 1 for the menu + confirmations, so a menu
        # split into two STT chunks causes a duplicate press. Suppress it. Aetna
        # only; other payers keep the default.
        dedupe_dtmf=True,
    ),

    # Kept for in-progress development. No payer_id maps to it yet, so it
    # is unreachable via /v1/Billing-Agent/Call until it's wired up.
    "HEALTH_FIRST": InsuranceConfig(
        name="HEALTH_FIRST",
        phone_number="+18882502220",
        debounce_seconds=0.1,
        claim_debounce_seconds=1,
        claims_tail_chars=200,
        prompt_template="HEALTH_FIRST_PROMPT_TEMPLATE",
        claims_prompt_template="HEALTH_FIRST_CLAIMS_CONTROLLER_TEMPLATE",
        segmentation_silence_ms=1400,
        claim_segmentation_silence_ms=1800,
        auto_hangup_seconds=900,
    ),

    # Blue Cross Blue Shield — ONE shared prompt for all state plans (the
    # verification flow is the same). All BCBS payer_ids route here; the actual
    # provider IVR NUMBER is chosen per-call from the payer_id via
    # BCBS_PAYER_ID_TO_NUMBER below. phone_number here is only a default/fallback.
    "BCBS": InsuranceConfig(
        name="BCBS",
        phone_number="+18003552583",  # default/fallback (NJ - Horizon)
        debounce_seconds=0.3,
        claim_debounce_seconds=1.2,
        claims_tail_chars=250,
        prompt_template="BCBS_PROMPT_TEMPLATE",
        claims_prompt_template="BCBS_CLAIMS_CONTROLLER_TEMPLATE",
        segmentation_silence_ms=1000,
        claim_segmentation_silence_ms=1500,
        auto_hangup_seconds=900,
    ),
}


# ── payer_id → insurance mapping ────────────────────────────────────────────
# Only the insurers whose flow is production-ready. Unknown payer_ids are
# rejected by /v1/Billing-Agent/Call with a 400 error.
PAYER_ID_TO_INSURANCE: Dict[str, str] = {
    "61101": "HUMANA",
    "62308": "CIGNA",
    "94999": "BAYLOR_SCOTT",
    "OSCAR": "OSCAR",
    "87726": "UHC",
    "60054": "AETNA",
    # BCBS — every state plan routes to the SAME "BCBS" prompt; the phone number
    # to dial is chosen per payer_id in BCBS_PAYER_ID_TO_NUMBER below.
    # Enabled BCBS state plans (5). AZ (53589), TX (TXBLS), IL (00621) are NOT
    # enabled yet — left unmapped so lookup_by_payer_id() returns None and the
    # endpoint replies "Billing Agent does not support this payer yet."
    "22099": "BCBS",   # NJ - Horizon
    "FLBLS": "BCBS",   # FL - Florida Blue
    "00241": "BCBS",   # MO - Anthem/Elevance
    "00720": "BCBS",   # MN - BCBS of Minnesota
    "VABLS": "BCBS",   # VA - Anthem/Elevance
}

SUPPORTED_PAYER_IDS_HELP = ", ".join(
    f"{pid} ({name})" for pid, name in PAYER_ID_TO_INSURANCE.items()
)


# ── BCBS: payer_id → provider IVR number ─────────────────────────────────────
# BCBS is many state plans that share ONE prompt but have DIFFERENT provider
# IVR numbers. The payer_id (from the Clinical API) picks the number to dial.
# NOTE: verify these payer_ids match what the Clinical API actually returns, and
# the numbers against the source sheet before production.
BCBS_PAYER_ID_TO_NUMBER: Dict[str, str] = {
    "22099": "+18003552583",   # NJ - Horizon
    "FLBLS": "+18007272227",   # FL - Florida Blue
    "00241": "+18885719054",   # MO - Anthem/Elevance
    "00720": "+18002620820",   # MN - BCBS of Minnesota
    "VABLS": "+18005331120",   # VA - Anthem/Elevance
}


def bcbs_number_for_payer_id(payer_id) -> Optional[str]:
    """Pick the BCBS provider IVR number for a payer_id (case-tolerant).
    Returns None if the payer_id has no BCBS number mapped."""
    if payer_id is None:
        return None
    key = str(payer_id).strip()
    return BCBS_PAYER_ID_TO_NUMBER.get(key) or BCBS_PAYER_ID_TO_NUMBER.get(key.upper())


# ── BCBS per-OPERATOR prompt routing ────────────────────────────────────────
# BCBS is run by ~a dozen independent operators (member companies). States under
# the SAME operator share one IVR → one prompt. Each operator OWNS its own prompt
# pair so a change to one can never affect the others (isolation over DRY, by
# design). Adding a new state = add one line here pointing it at its operator.
BCBS_PAYER_ID_TO_OPERATOR: Dict[str, str] = {
    "22099": "HORIZON",       # NJ
    "FLBLS": "FLORIDA_BLUE",  # FL
    "00241": "ELEVANCE",      # MO   (Anthem/Elevance)
    "VABLS": "ELEVANCE",      # VA   (same operator as MO → same prompt)
    "00720": "BCBS_MN",       # MN
    # AZ (53589), TX (TXBLS), IL (00621) intentionally NOT enabled yet.
}

# Operator → (navigation prompt name, claims controller name). Operators WITHOUT
# an entry here (e.g. BCBS_AZ, HCSC) fall back to the shared default templates
# until a real transcript proves they need their own copy — then add the files,
# register them, and add the entry here (one place).
BCBS_OPERATOR_PROMPTS: Dict[str, tuple] = {
    "HORIZON":      ("BCBS_HORIZON_PROMPT_TEMPLATE",      "BCBS_HORIZON_CLAIMS_CONTROLLER_TEMPLATE"),
    "FLORIDA_BLUE": ("BCBS_FLORIDA_BLUE_PROMPT_TEMPLATE", "BCBS_FLORIDA_BLUE_CLAIMS_CONTROLLER_TEMPLATE"),
    "ELEVANCE":     ("BCBS_ELEVANCE_PROMPT_TEMPLATE",     "BCBS_ELEVANCE_CLAIMS_CONTROLLER_TEMPLATE"),
    "BCBS_MN":      ("BCBS_MN_PROMPT_TEMPLATE",           "BCBS_MN_CLAIMS_CONTROLLER_TEMPLATE"),
}


def bcbs_config_for_payer_id(base: InsuranceConfig, payer_id) -> InsuranceConfig:
    """Return a per-call BCBS config whose prompt names + dial number are chosen
    by the payer_id's operator. Falls back to the shared default prompts (and the
    base number) for payer_ids whose operator has no own prompt yet. Non-BCBS or
    unknown payer_ids get `base` unchanged. Timing/tail_chars are inherited."""
    if payer_id is None:
        return base
    key = str(payer_id).strip()
    operator = BCBS_PAYER_ID_TO_OPERATOR.get(key) or BCBS_PAYER_ID_TO_OPERATOR.get(key.upper())
    prompts = BCBS_OPERATOR_PROMPTS.get(operator) if operator else None
    number = bcbs_number_for_payer_id(payer_id)
    if not prompts and not number:
        return base  # nothing to override
    nav, claims = prompts if prompts else (base.prompt_template, base.claims_prompt_template)
    return replace(
        base,
        prompt_template=nav,
        claims_prompt_template=claims,
        phone_number=number or base.phone_number,
    )


def lookup_by_payer_id(payer_id) -> Optional[InsuranceConfig]:
    """Return the InsuranceConfig for a given payer_id, or None if unsupported."""
    if payer_id is None:
        return None
    key = str(payer_id).strip()
    # Be case-tolerant only for string-keyed payers like "OSCAR"
    name = PAYER_ID_TO_INSURANCE.get(key) or PAYER_ID_TO_INSURANCE.get(key.upper())
    if not name:
        return None
    return INSURANCE_CONFIGS.get(name)


# ── plan name → insurance mapping ───────────────────────────────────────────
# Insurance is chosen from the Clinical API's `planShortName` value.
# Matching is case-insensitive.
PAYER_NAME_TO_INSURANCE: Dict[str, str] = {
    "CIGNA-TEST": "CIGNA",
    "HUMANA-TEST": "HUMANA",
    "BAYLOR-TEST": "BAYLOR_SCOTT",
    "OSCAR-TEST": "OSCAR",
}


def lookup_by_payer_name(plan_short_name) -> Optional[InsuranceConfig]:
    """Return the InsuranceConfig for a given Clinical API planShortName,
    or None if unknown. Case-insensitive."""
    if not plan_short_name:
        return None
    key = str(plan_short_name).strip().upper()
    for name, insurance in PAYER_NAME_TO_INSURANCE.items():
        if name.upper() == key:
            return INSURANCE_CONFIGS.get(insurance)
    return None


# ── per-call active insurance (ContextVar) ──────────────────────────────────
# Each asyncio task has its own isolated value, so concurrent calls with
# different insurers never stomp on each other. asyncio.create_task copies
# the current context, so background work (post_call_upload etc.) inherits
# the correct insurance automatically.
_active_insurance_var: contextvars.ContextVar[Optional[InsuranceConfig]] = (
    contextvars.ContextVar("active_insurance", default=None)
)


def set_active_insurance(config: InsuranceConfig) -> None:
    """Set the active insurance for the current async task.
    Must be called at each call-entry point:
      - /v1/Billing-Agent/Call (once the insurance is resolved from the plan name)
      - /webhooks/calls (restored from CallState.insurance_name)
      - /stream (restored from CallState.insurance_name on 'start')
    """
    _active_insurance_var.set(config)


def get_active_insurance() -> Optional[InsuranceConfig]:
    return _active_insurance_var.get()


def set_active_insurance_by_name(name: str) -> bool:
    """Convenience for webhooks/stream: set context by insurer name.
    Returns True on success, False if the name is unknown."""
    cfg = INSURANCE_CONFIGS.get(name) if name else None
    if cfg is None:
        return False
    _active_insurance_var.set(cfg)
    return True


def set_active_insurance_for_call(call_state) -> bool:
    """Set the active insurance for a call's task, OPERATOR-aware for BCBS.

    Like set_active_insurance_by_name, but for BCBS it reads the call's payer_id
    (from CallState.visit_data) and selects that operator's prompt pair + dial
    number via bcbs_config_for_payer_id. Non-BCBS insurers are unaffected.
    Use this at the stream/webhook entry points so handle_user_speech and the
    claims controller pick up the right per-operator prompt. Returns False if
    the insurer name is unknown."""
    name = getattr(call_state, "insurance_name", None)
    cfg = INSURANCE_CONFIGS.get(name) if name else None
    if cfg is None:
        return False
    if cfg.name == "BCBS":
        payer_id = (getattr(call_state, "visit_data", None) or {}).get("payer_id")
        cfg = bcbs_config_for_payer_id(cfg, payer_id)
    _active_insurance_var.set(cfg)
    return True


class ConfigManager:
    """Reads the active insurance config from the current async task's context.

    All getters raise if set_active_insurance() hasn't been called in the
    current task. This is intentional: it surfaces misconfigured call flows
    immediately rather than silently falling back to the wrong insurer.
    """

    def get_config(self) -> InsuranceConfig:
        cfg = _active_insurance_var.get()
        if cfg is None:
            raise RuntimeError(
                "No active insurance config. set_active_insurance() must be "
                "called at each call-entry point (orchestrate / webhooks / stream)."
            )
        return cfg

    def get_phone_number(self) -> str:
        return self.get_config().phone_number

    def get_debounce_seconds(self) -> float:
        return self.get_config().debounce_seconds

    def get_claim_debounce_seconds(self) -> float:
        return self.get_config().claim_debounce_seconds

    def get_claims_tail_chars(self) -> int:
        return self.get_config().claims_tail_chars

    def get_insurance_name(self) -> str:
        return self.get_config().name

    def get_segmentation_silence_ms(self) -> int:
        return self.get_config().segmentation_silence_ms

    def get_claim_segmentation_silence_ms(self) -> int:
        return self.get_config().claim_segmentation_silence_ms

    def get_auto_hangup_seconds(self) -> int:
        return self.get_config().auto_hangup_seconds

    def get_dedupe_chunks(self) -> bool:
        return self.get_config().dedupe_chunks

    # ── denial follow-up getters ─────────────────────────────────────────────
    def get_supports_denial_inquiry(self) -> bool:
        return self.get_config().supports_denial_inquiry

    def get_denial_rep_debounce_seconds(self) -> float:
        return self.get_config().denial_rep_debounce_seconds

    def get_denial_rep_segmentation_silence_ms(self) -> int:
        return self.get_config().denial_rep_segmentation_silence_ms

    def get_denial_auto_hangup_seconds(self) -> int:
        cfg = self.get_config()
        return cfg.denial_auto_hangup_seconds or cfg.auto_hangup_seconds

    def get_denial_ivr_request_dtmf(self) -> Optional[str]:
        return self.get_config().denial_ivr_request_dtmf


# Global instance — stateless, reads from ContextVar on every call.
config_manager = ConfigManager()
