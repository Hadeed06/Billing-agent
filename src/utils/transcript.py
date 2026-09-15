"""
Helpers for recording every utterance/action in a call into
call_state.full_transcript. Used to build the JSON transcript that
gets uploaded to PracticeEHR after the call ends.
"""


def append_ivr(call_state, text: str) -> None:
    """Append something the IVR (caller) said."""
    if not call_state or not text:
        return
    text = text.strip()
    if not text:
        return
    call_state.full_transcript.append({"speaker": "ivr", "text": text})


def append_agent(call_state, text: str) -> None:
    """Append something our agent said (TTS)."""
    if not call_state or not text:
        return
    text = text.strip()
    if not text:
        return
    call_state.full_transcript.append({"speaker": "agent", "text": text})


def append_agent_dtmf(call_state, digits: str) -> None:
    """Append a DTMF action taken by the agent."""
    if not call_state or not digits:
        return
    call_state.full_transcript.append({"speaker": "agent", "text": f"[DTMF: {digits}]"})
