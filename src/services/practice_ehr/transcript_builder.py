"""
Build the JSON transcript payload for PracticeEHR from CallState.
"""
import json


def build_transcript_json(call_state) -> bytes:
    """
    Return the transcript as UTF-8 JSON bytes, ready to upload.
    Shape: {"conversation": [{"speaker": "ivr"|"agent", "text": "..."}, ...]}
    """
    payload = {"conversation": list(getattr(call_state, "full_transcript", []) or [])}
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
