# Outbound Azure Telnyx IVR Agent

This project is a **Python-based outbound voice assistant** that integrates:
- **Telnyx** (for outbound calling and WebSocket audio streaming)
- **Azure Speech Services** (Speech-to-Text & Text-to-Speech)
- **LLMs (GPT / Llama)** for conversational and claim-handling logic.

---

## Folder Structure and Purpose

OUTBOUND_AZURE_TELNYX/
├── main.py
├── requirements.txt
├── .env.example
├── src/
│   ├── __init__.py
│   ├── main.py
│   ├── api/
│   │   ├── __init__.py
│   │   └── v1/
│   │       ├── __init__.py
│   │       ├── orchestrate.py
│   │       ├── stream.py
│   │       └── webhooks.py
│   ├── config/
│   │   ├── __init__.py
│   │   └── insurance_config.py
│   ├── core/
│   │   ├── __init__.py
│   │   ├── agents/
│   │   │   ├── __init__.py
│   │   │   └── claims_agent.py
│   │   └── prompts/
│   │       ├── __init__.py
│   │       ├── manager.py
│   │       ├── loader.py
│   │       ├── claims_prompts.py
│   │       └── templates/
│   │           ├── baylor_scott/
│   │           ├── cigna/
│   │           └── humana/
│   ├── models/
│   │   ├── __init__.py
│   │   └── data_models.py
│   ├── services/
│   │   ├── __init__.py
│   │   ├── azure/
│   │   │   ├── __init__.py
│   │   │   ├── stt_service.py
│   │   │   └── tts_service.py
│   │   ├── telnyx/
│   │   │   ├── __init__.py
│   │   │   └── client.py
│   │   ├── llm_service.py
│   │   ├── call_lifecycle.py
│   │   ├── call_cleanup.py
│   │   └── claims_helpers.py
│   └── utils/
│       └── __init__.py
└── Old Files/




---

## File / Module Functions

### Root Level
- **main.py** →
 - Initializes **FastAPI app**.
- Mounts all routers (webhooks, stream, orchestrate).
- Loads environment and global variables.

- **requirements.txt** → Lists dependencies.
- **.env.example** → Placeholder for environment variables.

### `/src`
Main source directory containing API routes, services, configuration, and LLM logic.


### `/src/api/v1/`
Contains all exposed HTTP & WebSocket endpoints:
- **orchestrate.py** → Starts outbound call using Telnyx.
- **stream.py** → Manages Telnyx media stream (WebSocket audio in/out).
- **webhooks.py** → Handles Telnyx event webhooks (`call.answered`, `call.hangup`, etc.).

### `/src/config/insurance_config.py`
- Defines insurer-specific parameters:
  `debounce_seconds`, `claim_debounce_seconds`, `claims_tail_chars`.
- Controls which templates to load for **CIGNA / HUMANA / BAYLOR_SCOTT**.

### `/src/core/agents/claims_agent.py`
- Uses **GPT** (via OpenAI API) to detect intents:
  `"YES"`, `"NO"`, `"NEXT"`, `"STOP"`, `"FAX-ID"`, `"CONTINUE"`.
- Handles logic for **claims controller** and decision mapping.

### `/src/core/prompts/`
Handles all prompt management:
- **manager.py** → Selects prompt templates based on insurer.
- **loader.py** → Loads text templates dynamically.
- **claims_prompts.py** → Defines specific claim-related templates.
- **templates/** → Folder for text-based LLM prompt templates (one subfolder per insurer).

### `/src/models/data_models.py`
- Defines main dataclasses:
  `CallState`, `SimpleCallRequest`, etc.
- Used to manage per-call context and message flow.

### `/src/services/`
Handles all external service integrations.

#### `/azure/`
- **stt_service.py** → Azure Speech-to-Text streaming client.
- **tts_service.py** → Azure Text-to-Speech, sends generated audio to Telnyx stream.

#### `/telnyx/`
- **client.py** → Telnyx REST client; sends `dial`, `dtmf`, `hangup` requests.

#### Other Services
- **llm_service.py** → Communicates with local **Llama API**, parses LLM responses (`say`, `dtmf`, `endcall`).
- **call_lifecycle.py** → Handles start/hangup flow and state management.
- **call_cleanup.py** → Performs safe cleanup of active calls (idempotent).
- **claims_helpers.py** → Helpers for detecting claim start/end or “not found” phrases.

### `/src/utils/`
- Utility placeholder for shared helper functions (currently empty).


---

## Run Instructions

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 5000 --reload
