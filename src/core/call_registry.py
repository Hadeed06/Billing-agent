"""Shared in-memory registry of live calls.

Single source of truth for CallState keyed by Telnyx call_control_id. Lives here
rather than in main.py so any layer — the entrypoint, the flow handlers, the
services — imports the SAME dict without depending on the ASGI entrypoint.

In-memory only: a process restart drops every live call (they disconnect).
"""
from typing import Dict

from src.models.data_models import CallState

active_calls: Dict[str, CallState] = {}
