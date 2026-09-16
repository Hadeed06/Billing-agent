from src.config.insurance_config import config_manager
from .loader import load_prompt

# Map your config names to the actual files under templates/<carrier>/
_MAIN_PROMPT_FILES = {
    "CIGNA_PROMPT_TEMPLATE":        "cigna/cigna_prompt_template",
    "HUMANA_PROMPT_TEMPLATE":       "humana/humana_prompt_template",
    "BAYLOR_SCOTT_PROMPT_TEMPLATE": "baylor_scott/baylor_scott_prompt_template",
    "OSCAR_PROMPT_TEMPLATE": "oscar/oscar_prompt_template",
    "HEALTH_FIRST_PROMPT_TEMPLATE": "health_first/health_first_prompt_template",
    "UHC_PROMPT_TEMPLATE": "uhc/uhc_prompt_template",
    "AETNA_PROMPT_TEMPLATE": "aetna/aetna_prompt_template",
    "MOLINA_PROMPT_TEMPLATE": "molina/molina_prompt_template",
    "BCBS_PROMPT_TEMPLATE": "bcbs/bcbs_prompt_template",   # shared default / fallback
    # BCBS per-OPERATOR navigation prompts (payer_ids under one operator share one).
    # Each operator OWNS its file so a change to one can't affect the others.
    "BCBS_HORIZON_PROMPT_TEMPLATE":      "bcbs/bcbs_horizon_prompt_template",
    "BCBS_FLORIDA_BLUE_PROMPT_TEMPLATE": "bcbs/bcbs_florida_blue_prompt_template",
    "BCBS_ELEVANCE_PROMPT_TEMPLATE":     "bcbs/bcbs_elevance_prompt_template",
    "BCBS_MN_PROMPT_TEMPLATE":           "bcbs/bcbs_mn_prompt_template",
}

def get_main_prompt_template() -> str:
    """
    Return the raw main prompt text (unformatted).
    Callers do: get_main_prompt_template().format(**vars)
    """
    prompt_name = config_manager.get_config().prompt_template
    try:
        file_stem = _MAIN_PROMPT_FILES[prompt_name]
    except KeyError:
        raise ValueError(f"Unknown main prompt: {prompt_name}")
    return load_prompt(file_stem)


# ── denial follow-up templates ──────────────────────────────────────────────
# Keyed by (insurance name, phase). Phase is "ivr" (reach a representative
# after the pivot) or "representative" (live rep conversation). A payer must
# have BOTH entries before its supports_denial_inquiry flag is turned on.
_DENIAL_PROMPT_FILES = {
    ("HUMANA", "ivr"): "humana/humana_denial_ivr_template",
    ("HUMANA", "representative"): "humana/humana_denial_rep_template",
    ("CIGNA", "ivr"): "cigna/cigna_denial_ivr_template",
    ("CIGNA", "representative"): "cigna/cigna_denial_rep_template",
    ("BAYLOR_SCOTT", "ivr"): "baylor_scott/baylor_denial_ivr_template",
    ("BAYLOR_SCOTT", "representative"): "baylor_scott/baylor_denial_rep_template",
    ("OSCAR", "ivr"): "oscar/oscar_denial_ivr_template",
    ("OSCAR", "representative"): "oscar/oscar_denial_rep_template",
}


def get_denial_prompt_template(phase: str) -> str:
    """Return the raw denial-flow prompt text for the active insurance.

    phase: "ivr" | "representative"
    Raises ValueError if the active payer has no template for that phase —
    fail loudly, same philosophy as the config_manager getters.
    """
    insurance = config_manager.get_insurance_name()
    try:
        file_stem = _DENIAL_PROMPT_FILES[(insurance, phase)]
    except KeyError:
        raise ValueError(
            f"No denial prompt registered for insurance={insurance!r} phase={phase!r}"
        )
    return load_prompt(file_stem)

# Back-compat
def get_prompt_template() -> str:
    return get_main_prompt_template()
