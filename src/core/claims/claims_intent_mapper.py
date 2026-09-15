def map_keyword(upper: str) -> str:
    """Map raw GPT output to a canonical intent string."""

    if "DTMF:" in upper:
        return upper  # Return as-is: "DTMF:1", "DTMF:2", etc.

    # Check for single digits (in case LLaMA returns just the number)
    if upper in ["1", "2", "3", "4", "5", "6", "7", "8", "9", "0"]:
        return f"DTMF:{upper}"

    if "CHARGE" in upper:
        return "CHARGE"
    if "DETAIL" in upper:
        return "DETAILS"
    if "NEXT" in upper:
        return "NEXT"
    if "STOP" in upper or "END" in upper or "HANG" in upper or "MAIN MENU" in upper or "NO MORE CLAIM" in upper:
        return "STOP"
    if "CONFIRM" in upper or "YES" in upper or "HEAR CLAIM" in upper:
        return "CONFIRM"
    if "FAX" in upper or "FAX ID" in upper or "FAX-ID" in upper or "FAXID" in upper:
        return "FAX-ID"
    if upper == "NO" or " NO" in upper or upper.startswith("NO") or upper.endswith(" NO"):
        return "NO"
    return "CONTINUE"
