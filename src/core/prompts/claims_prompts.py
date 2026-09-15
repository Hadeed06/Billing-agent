from .loader import load_prompt

_CLAIMS_PROMPT_FILES = {
    "CIGNA_CLAIMS_CONTROLLER_TEMPLATE":        "cigna/cigna_claims_controller_template",
    "HUMANA_CLAIMS_CONTROLLER_TEMPLATE":       "humana/humana_claims_controller_template",
    "BAYLOR_SCOTT_CLAIMS_CONTROLLER_TEMPLATE": "baylor_scott/baylor_scott_claims_controller_template",
    "OSCAR_CLAIMS_CONTROLLER_TEMPLATE": "oscar/oscar_claims_controller_template",
    "HEALTH_FIRST_CLAIMS_CONTROLLER_TEMPLATE": "health_first/health_first_claims_controller_template",
    "UHC_CLAIMS_CONTROLLER_TEMPLATE": "uhc/uhc_claims_controller_template",
    "AETNA_CLAIMS_CONTROLLER_TEMPLATE": "aetna/aetna_claims_controller_template",
    "BCBS_CLAIMS_CONTROLLER_TEMPLATE": "bcbs/bcbs_claims_controller_template",   # shared default / fallback
    # BCBS per-OPERATOR claims controllers (mirror the navigation prompts above).
    "BCBS_HORIZON_CLAIMS_CONTROLLER_TEMPLATE":      "bcbs/bcbs_horizon_claims_controller_template",
    "BCBS_FLORIDA_BLUE_CLAIMS_CONTROLLER_TEMPLATE": "bcbs/bcbs_florida_blue_claims_controller_template",
    "BCBS_ELEVANCE_CLAIMS_CONTROLLER_TEMPLATE":     "bcbs/bcbs_elevance_claims_controller_template",
    "BCBS_MN_CLAIMS_CONTROLLER_TEMPLATE":           "bcbs/bcbs_mn_claims_controller_template",
}

def get_claims_prompt(prompt_name: str) -> str:
    """Return raw claims prompt text (unformatted)."""
    try:
        file_stem = _CLAIMS_PROMPT_FILES[prompt_name]
    except KeyError:
        raise ValueError(f"Unknown claims prompt: {prompt_name}")
    return load_prompt(file_stem)
