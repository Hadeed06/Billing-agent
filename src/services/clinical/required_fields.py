"""
Per-insurance list of visit_data fields that MUST be present (non-null,
non-empty) before a call can start. If any are missing, orchestrate
refuses to place the call.
"""
from typing import Dict, List

# Keys match the prompt placeholders in src/core/prompts/templates/*.
REQUIRED_FIELDS_BY_INSURANCE: Dict[str, List[str]] = {
    "CIGNA":        ["tax_id", "npi", "member_id", "dob", "member_name", "dos"],
    "HUMANA":       ["tax_id", "npi", "member_id", "dob", "member_name", "dos"],
    "BAYLOR_SCOTT": ["tax_id", "npi", "member_id", "dob", "member_name", "dos"],
    "OSCAR":        ["tax_id", "npi", "member_id", "dos"],
    # UHC's IVR can ask for the provider Tax ID (TIN) in addition to NPI, and
    # verifies the member by name + Member ID + DOB + DOS.
    "UHC":          ["tax_id", "npi", "member_id", "member_name", "dob", "dos"],
    # Aetna verifies by NPI + Tax ID + Aetna member ID + DOB + DOS, and confirms
    # the patient NAME back to us ("the patient is <name>, yes/no"). After the NPI
    # its IVR asks for the tax ID the claim was filed under. All on keypad.
    "AETNA":        ["tax_id", "npi", "member_id", "member_name", "dob", "dos"],
    # Molina (NV Medicaid) verifies by provider NPI + member ID + DOB + DOS, all
    # entered on the keypad. It does NOT ask for the tax ID. Member name isn't
    # asked by the IVR but is required as useful context in the prompt.
    "MOLINA":       ["npi", "member_id", "member_name", "dob", "dos"],
    # BCBS verifies by NPI ("provider ID number") + subscriber (member) ID + DOB
    # + DOS. Several BCBS operators (e.g. Florida Blue, Elevance) also ask for the
    # claim's TOTAL CHARGE to disambiguate — so charge_amount is required up front:
    # without it the IVR question can't be answered and the call fails mid-flow.
    "BCBS":         ["npi", "member_id", "member_name", "dob", "dos", "charge_amount"],
}


def missing_fields_for(insurance_name: str, visit_data: dict) -> List[str]:
    """Return the list of required fields that are missing/empty for this insurance."""
    required = REQUIRED_FIELDS_BY_INSURANCE.get(insurance_name, [])
    missing = []
    for key in required:
        value = visit_data.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            missing.append(key)
    return missing
