# CLAUDE.md — Outbound IVR Claim-Status Agent

Onboarding guide for the next Claude session working on this repo. Reads what the code IS,
how the CLAIM STATUS FLOW works end-to-end, WHAT we've fixed and WHY, what's in each branch,
and how to safely add new features / insurers.

Keep this file up to date when you change something meaningful.

---

## 1. What this project is

An automated outbound IVR agent for medical claim status inquiries. Given a `visit_id` +
`customer_id`, it:

1. Looks up the visit's insurance from the PracticeEHR Clinical API (routed by `payerId`).
2. Places an outbound phone call to that payer's IVR line via Telnyx.
3. Streams the audio through Azure Speech-to-Text (STT).
4. Uses Azure OpenAI (GPT-4.1 PTU) to decide what to say/press at each IVR prompt.
5. Speaks responses back via Azure TTS.
6. Captures the claim(s) the payer reads out.
7. Classifies the final claim status → PAID / DENIED / IN PROCESS / UNKNOWN / NOT ON FILE /
   PATIENT NOT FOUND / CALL FAILED.
8. Uploads recording + transcript to PracticeEHR + writes the outcome to the
   Billing-Agent/Log DB, then PATCHes the Ivr/ClaimStatus endpoint so the frontend sees it.

Deployed to Azure App Service (Linux container): `as-ai-billing-agent-prod-cus` (Central US).

---

## 2. Tech stack

- **Runtime**: Python 3.10, FastAPI, uvicorn, port 5000
- **Telephony**: Telnyx Call Control v2 (webhooks + bidirectional WebSocket audio streaming)
- **STT**: Azure Cognitive Services Speech (real-time, μ-law input, sends `on_partial` /
  `on_final` callbacks)
- **TTS**: Azure Speech TTS, voice `en-US-JennyNeural`, SSML-driven (dates spoken naturally,
  alphanumeric IDs spelled character-by-character)
- **LLM**: Azure OpenAI (Azure deployment name `gpt-4.1`, temperature=0, PTU)
- **HTTP**: `httpx.AsyncClient` singleton with connection pooling (fixed SNAT exhaustion)
- **Observability**: Application Insights via OpenTelemetry
- **CI/CD**: Azure Pipelines (`azure-pipelines.yml`)

---

## 3. End-to-end claim-status flow

```
Frontend --POST /v1/Billing-Agent/Call {visit_id, customer_id, Bearer token}
     |
     v
orchestrate.py
  1. Resolve api_key   (PracticeEHR /v1/Clients/AuthClients)
  2. Fetch visit_data  (Clinical API /v1/Clinical/Billing-Agent/Visit/{id})
                       → returns payer_id + tax_id/npi/member_id/dob/dos/member_name
  3. Route insurance   (lookup_by_payer_id → CIGNA / HUMANA / BAYLOR_SCOTT / OSCAR / ...)
  4. Validate required visit fields for this insurer (required_fields.py)
  5. Reserve RefNo     (POST Billing-Agent/Log — reserves the row for two-phase update)
  6. Telnyx.dial()     (outbound call to payer's IVR phone number)
  7. Return {succeeded, message, refNo} to frontend

Telnyx --webhook 'call.answered'-> webhooks.py       (restore log context + insurance ContextVar)
Telnyx --WebSocket 'start'-------> stream.py         (start Azure STT session, restore context)
Telnyx --WebSocket 'media' (μ-law inbound audio, 20ms frames)
     |
     v
stt_service.py  → convert_mulaw_to_pcm → push_stream.write → Azure STT
                → STT callbacks (on_partial/on_final) run inside CAPTURED ContextVar snapshot
                  (loop.call_soon_threadsafe with context=ctx  — see fix note in section 12)
     |
     v
stream.py debounce (per-utterance debounce; final chunks accumulate until N seconds of silence)
     |
     v
main.py::handle_user_speech(text, call_control_id)
  a) is_claim_not_found(text)  → cleanup with reason "claims: not found"  (SUCCESS: NOT ON FILE)
  b) is_claim_start(text)      → flip claim_mode + bump debounce + start claims session
  c) In claim_mode → forward chunk to claims_controller (per-insurer prompt: DETAILS/NEXT/STOP/CONTINUE)
  d) Otherwise → format the insurer's main prompt template with visit_data + transcript
                 → GPT → parse response (say / value / dtmf / endcall / fallback / claim_mode)
     |
     v
Response parser (llm_service.py + main.py post-GPT):
  - dtmf:N            → Telnyx send_dtmf
  - say/value/confirm → TTS (value: substitutes authoritative member_id if hallucinated)
  - endcall           → ensure_call_cleanup (send_hangup=True)
  - fallback / unknown → silent
  - claim_mode (NEW)  → GPT fallback signal → same helper as is_claim_start (see section 12)

Call ends (webhook 'call.hangup' | WebSocket close | endcall | is_claim_not_found | auto_hangup):
     |
     v
call_cleanup.py::ensure_call_cleanup (idempotent, guarded by cleanup_lock)
  1. Cancel pending debounce task (prevents late STT KeyError — see section 12)
  2. End claims_agent session
  3. Stop Azure STT session
  4. Optional Telnyx hangup
  5. Snapshot call state (visit_id, transcript, finalized_claims, cleanup_reason, ...)
  6. Fire-and-forget upload_call_artifacts(snapshot)  (background task, strong-refd)
     |
     v
post_call_upload.py
  1. Wait 5s for Telnyx recording to finalize
  2. Fetch recording (.wav) from Telnyx, upload to PracticeEHR under billing/agent/claim/{cid}/{vid}
  3. Build transcript JSON, upload alongside
  4. Determine outcome:
       is_incomplete (auto_hangup/shutdown)           → FAILED + classify_failure()
       finalized_claims present                       → SUCCESS + classify_claim() (paid/denied/inprocess/unknown)
       cleanup_reason == "claims: not found"          → SUCCESS + "no claim" (→ NOT ON FILE)
       else                                           → classify_failure()  (safety net!)
                                                         - matches "no claim" patterns → SUCCESS
                                                         - matches "patient not found" → FAILED
                                                         - else GPT semantic fallback  → FAILED
  5. update_billing_log_row(RefNo, requestStatus, claimStatus, description, paths)
  6. post_ivr_claim_status (PATCH IVR/ClaimStatus with mapped Status enum)
```

Status vocabulary end-to-end:

| Internal            | Billing-Agent/Log `claimStatus` | Ivr/ClaimStatus `Status`     |
|---------------------|---------------------------------|------------------------------|
| paid                | paid                            | PAID                         |
| inprocess           | inprocess                       | IN PROCESS                   |
| denied              | denied                          | DENIED                       |
| unknown             | unknown                         | UNKNOWN                      |
| no claim            | no claim                        | NOT ON FILE                  |
| patient not found   | patient not found               | PATIENT NOT FOUND            |
| call failed         | call failed                     | CALL FAILED                  |

---

## 4. Project structure

```
main.py                                  — FastAPI app, handle_user_speech, wiring
src/
  api/v1/
    orchestrate.py                       — POST /v1/Billing-Agent/Call (start call)
    webhooks.py                          — Telnyx call.answered/initiated/hangup handlers
    stream.py                            — WebSocket /stream: audio streaming, debounce loop
  auth/jwt_auth.py                       — Bearer token verify (frontend JWT)
  config/insurance_config.py             — InsuranceConfig, INSURANCE_CONFIGS dict, ContextVar
  core/
    claims/
      claims_agent.py                    — claims session lifecycle
      claims_controller.py               — pumps chunks through claims_controller GPT prompt
      claims_helpers.py                  — is_claim_not_found / is_claim_start triggers
      claims_intent_mapper.py            — one-word intent → action
    prompts/
      manager.py                         — get_main_prompt_template() (per active insurer)
      claims_prompts.py                  — get_claims_controller_template()
      loader.py                          — reads .txt templates from disk
      templates/
        cigna/                           — main + claims_controller templates
        humana/
        baylor_scott/
        oscar/
        health_first/
  models/data_models.py                  — CallState dataclass, SimpleCallRequest
  services/
    azure/
      stt_service.py                     — AzureRealtimeSttService (see STT ContextVar fix)
      tts_service.py                     — Azure TTS + SSML routing (dates vs codes)
    billing_log/
      classifier.py                      — classify_claim (paid/denied/inprocess/unknown)
      failure_classifier.py              — classify_failure (no claim/patient not found/call failed)
      claim_status_client.py             — PATCH Ivr/ClaimStatus
      log_client.py                      — Billing-Agent/Log CRUD
    clinical/
      client.py                          — Clinical /v1/Clinical/Billing-Agent/Visit
      required_fields.py                 — per-insurer required visit_data fields
    llm/llm_service.py                   — _call_gpt_api + response parser + hallucination guard
    practice_ehr/
      post_call_upload.py                — end-of-call orchestration (see section 3)
      telnyx_recording.py                — Telnyx recording fetch
      transcript_builder.py              — build JSON transcript
      uploader.py                        — upload artifact to PracticeEHR
    practice_ehr_auth/client.py          — resolve per-customer api_key
    telnyx/client.py                     — Telnyx REST helpers (dial, send_dtmf, hangup)
    call_cleanup.py                      — ensure_call_cleanup (idempotent)
    call_lifecycle.py                    — hangup_call, auto_hangup
    http_client.py                       — singleton httpx.AsyncClient (Adan's SNAT fix)
  utils/
    logging_config.py                    — call_id/visit_id/customer_id ContextVars + filter
    transcript.py                        — append_ivr/append_agent helpers
test_cigna_prompt.py                     — GITIGNORED — prompt regression harness
```

---

## 5. Multi-insurance routing

**Key idea**: routing is by `payer_id` from the Clinical API, NOT by plan name. Plan names
in the billing DB change; payer IDs are stable.

- `INSURANCE_CONFIGS` in `src/config/insurance_config.py` — per-insurer timings + prompt names.
- `PAYER_ID_TO_INSURANCE` — maps payer_ids like `"62308"` → `"CIGNA"`.
- `set_active_insurance()` / `set_active_insurance_by_name()` — set the per-request
  `active_insurance` ContextVar. Every downstream `config_manager.get_*()` reads from it.
- The insurance context is set at ALL entry points: orchestrate (call creation), webhooks
  (each webhook), stream (WebSocket 'start' event). Also inside STT callbacks — see
  section 12 for the ContextVar propagation fix.

**Per-insurer knobs** (`InsuranceConfig`):
- `debounce_seconds` / `claim_debounce_seconds` — how long to wait after last STT final
  before processing the accumulated text. Higher = fewer chunks, more context per GPT call.
- `segmentation_silence_ms` / `claim_segmentation_silence_ms` — Azure STT boundary detection.
  Different for normal vs claim-listing phase (the claim phase needs longer silence
  detection because payers pause more between line items).
- `claims_tail_chars` — how much of the accumulated claim text to send in each
  claims_controller GPT call.
- `auto_hangup_seconds` — force-terminate after this many seconds (safety net for stuck IVRs).
- `dedupe_chunks` — Cigna-only: skip the GPT call if this chunk is near-identical to the
  last one sent. Was needed because Cigna's STT had trailing-char races producing
  duplicate DETAILS / NEXT responses.

---

## 6. Prompt architecture

Each insurer has TWO templates:

1. **`{insurer}_prompt_template.txt`** — used for the general IVR conversation.
   Response formats GPT can return: `say:<phrase>`, `value:<value>`, `dtmf:<digit>`,
   `confirm:<yes/no>`, `endcall`, `fallback`, **`claim_mode`** (new safety-net signal).
   Templates get filled in with `{tax_id}`, `{npi}`, `{member_id}`, `{dob}`, `{dos}`,
   `{member_name}`, `{transcript}`.

2. **`{insurer}_claims_controller_template.txt`** — used ONCE claim_mode is active.
   Returns one of `DETAILS`, `NEXT`, `STOP`, `CONTINUE`. Has duplicate-prevention rule
   (don't send NEXT twice in a row — send CONTINUE between). Gets filled in with
   `{transcript_chunk}` and `{last_response}`.

**All three "spoken value" prompt templates** (cigna, humana, baylor_scott) have a
"CRITICAL: Alphanumeric ID Fidelity" section warning GPT NOT to substitute look-alike
characters (1↔I, 0↔O, 5↔S, etc.). See section 12.

Oscar and HealthFirst are **DTMF-only** — GPT types the ID digit-by-digit via keypad, no
letter/digit confusion possible, so no `value:` branch and no ID fidelity section.

**Adding a new insurer's prompt**: see section 16.

---

## 7. Configuration / env vars

Required at runtime (see `.env.example`):

| Var                              | Purpose                                                  |
|----------------------------------|----------------------------------------------------------|
| `WEBHOOK_BASE_URL`               | Public URL Telnyx sends webhooks to                      |
| `STREAM_BASE_URL`                | Public WebSocket URL for `/stream`                       |
| `TELNYX_API_KEY`                 | Telnyx REST API key                                      |
| `TEL_FROM`                       | Outbound "from" number                                   |
| `CALL_CONTROL_APP_ID`            | Telnyx Call Control application ID                       |
| `AZURE_SPEECH_KEY` / `REGION`    | Azure Cognitive Services Speech                          |
| `PTU_API_KEY` / `ENDPOINT` / `VERSION` | Azure OpenAI (PTU)                                 |
| `OPENAI_MODEL`                   | Azure deployment name (default `gpt-4.1`)                |
| `JWT_SECRET_KEY`                 | HS256 secret used to verify frontend Bearer tokens       |
| `PRACTICE_EHR_BASE_URL`          | PracticeEHR file upload                                  |
| `PRACTICE_EHR_AUTH_BASE_URL`     | Auth API (resolve per-customer api_key)                  |
| `CLINICAL_API_BASE_URL`          | Clinical /v1/Clinical/Billing-Agent/Visit                |
| `BILLING_AGENT_LOG_BASE_URL`     | Billing-Agent/Log CRUD                                   |
| `IVR_CLAIM_STATUS_BASE_URL`      | PATCH Ivr/ClaimStatus                                    |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | App Insights                                      |

Prod values live in `.env.prod` (gitignored). QA in `.env.qa` (gitignored).
`.env.example` is committed as a schema reference.

---

## 8. Branches (as of 2026-07)

| Branch                       | Purpose |
|------------------------------|---------|
| `main` (azure-new)           | Deployment branch — what runs in prod. |
| `develop` (azure-new)        | Active integration branch. **You work here.** |
| `refactor` (origin/azure)    | Legacy — where the big code split from monolith to `src/` happened. Superseded by develop. |
| `feature/uhc-claim-status`   | WIP: UHC (United Healthcare) claim-status endpoint + prompts. Not merged. |
| `denial-inquiry-wip`         | See below. |
| `oscar`                      | Old Oscar-specific branch — superseded. |
| `refactor-backup` / `refactor-test` / `refactor-template` | Old checkpoints. |

Remotes:
- `azure-new` → `https://dev.azure.com/PracticeEHR/PracticeEHR-AI/_git/PracticeEHR-AI-Billing-Agents` ← current
- `azure` → older Azure DevOps location (legacy)
- `origin` → github.com fork (legacy)

### The `denial-inquiry-wip` branch

Adds a **separate endpoint** `/v1/Denial-Inquiry/Call` for a DIFFERENT flow:
- **What it does**: automated calls to inquire about DENIED claims — talks to the payer
  representative to understand denial reasons and next steps, gathering PCP/authorization
  numbers/codes as needed.
- **Two-phase prompt architecture**: (a) IVR phase to reach a rep, (b) rep-phase for the
  actual conversation. Longer conversation history (12 turns vs 8) so the bot stays
  oriented through longer rep conversations.
- **Files added**: `src/api/v1/denial_inquiry.py` (207 lines), plus Humana-specific denial
  templates: `humana_denial_ivr_template`, `humana_denial_rep_template`.
- **Distinct required fields**: `visit_id`, `patient_name`, `dob`, `dos`, `billed_amount`,
  `member_id`, `plan`, `provider_npi`, `provider_name` (more than the claim-status flow —
  reps ask for provider info).
- **TTS voice on that branch**: `en-US-AndrewMultilingualNeural` (not Jenny). Also uses
  a manual PCM→μ-law conversion path (different from the SSML-driven claim-status flow).
- **Key commits**: `fb9ac38` (initial), `4e0893f` (voice + audio conversion),
  `0fbca57` (smarter prompt rules + 12-turn history).
- **Status**: not merged into develop. If we want to merge, we'll need to:
  1. Reconcile the TTS voice/audio-conversion divergence.
  2. Rebase on top of the new httpx singleton + STT ContextVar fixes.
  3. Bring the two-phase prompt architecture cleanly into the current file layout.

---

## 9. Deployment

- **Prod**: Azure App Service `as-ai-billing-agent-prod-cus` (Central US, Linux container).
- **Build**: `azure-pipelines.yml` triggers on `main`. Builds Docker image, pushes to
  ACR, deploys to App Service.
- **`Dockerfile`**: bullseye base with STT system deps, uvicorn entrypoint on port 5000.
- **Startup**: uvicorn runs `main:app`. `main.py` at import time calls `setup_logging()`,
  wires all routers with shared `active_calls` / `initiated_events` dicts, registers
  claims_agent callbacks, and mounts Application Insights.
- **App Service quirks**:
  - Anything on stderr is classified as ERROR by App Service. `BelowErrorFilter` in
    `logging_config.py` splits INFO/WARN → stdout, ERROR/CRIT → stderr.
  - SNAT ports are limited per instance — that's why we MUST use the singleton
    `httpx.AsyncClient` from `src/services/http_client.py`. Never create per-request
    clients.

---

## 10. Two-phase Billing-Agent/Log write

We reserve the DB row at CALL START so the frontend gets a `RefNo` immediately:

1. **POST** `Billing-Agent/Log` at call creation → row inserted with placeholders,
   `requestStatus="in_progress"`. Returns the row id (this becomes `RefNo`).
2. **PUT** `Billing-Agent/Log/{RefNo}` at call end (from `post_call_upload.py`) with
   the real `requestStatus` / `claimStatus` / description / uploaded file paths.

If the initial POST fails (`ref_no is None`), we fall back at call end to a single POST
so the call is still logged — the frontend won't have a RefNo to display, but the DB
record + downstream `Ivr/ClaimStatus` PATCH still happen.

---

## 11. Logging

Every log line is tagged with `c=<call_id> v=<visit_id> cid=<customer_id>` via ContextVars
in `logging_config.py`. Cost is a couple of dict lookups per record — not measurable.

Format:
```
[2026-07-21 14:32:11 - INFO - c=DthoBVG0 v=88231 cid=5567 - main.py - handle_user_speech] ...
```

KQL to filter by visit or customer in App Insights:
```kql
traces | where message contains "v=88231"
traces | where message contains "cid=5567"
```

Entry points that set the context:
- `orchestrate.py` — `set_call_id` + `set_visit_context` at call creation
- `webhooks.py` — restore from `active_calls` on each webhook
- `stream.py` — restore on WebSocket 'start'

The context propagates automatically into `asyncio.create_task` children (including the
post-call upload background task) so post-call logs are also tagged.

---

## 12. History — bugs we hit and how we handled them

Understanding these makes it easier to keep the invariants intact when adding features.

### 12.1 SNAT port exhaustion in prod (Adan's fix)
- **Symptom**: `ConnectTimeout` across every outbound API (Auth, Clinical, Billing-Agent/Log,
  Telnyx, GPT, PracticeEHR upload) — random, correlated with call volume.
- **Cause**: per-request `httpx.AsyncClient()` opened fresh TCP connections, exhausting the
  App Service instance's limited SNAT port pool.
- **Fix**: `src/services/http_client.py` — a process-wide singleton `AsyncClient` with a
  connection pool (max_connections=100, max_keepalive=20, short connect timeout so failed
  ports free fast). All service clients (Clinical, Auth, Billing-Log, IVR, Telnyx recording,
  TTS, uploader) use `get_http_client()` now. Never make a per-request client.
- **Bonus**: `stt_service.py` used to run `asyncio.run(asyncio.sleep(0.1))` 10×/sec per call
  (spinning up and tearing down a fresh event loop each time). Replaced with `time.sleep(0.1)`
  in the worker thread. Massive CPU improvement under load.

### 12.2 STT ContextVar propagation (Adan's fix + our diagnosis)
- **Symptom**: `RuntimeError: No active insurance config` firing dozens of times per
  concurrent call — but ONLY during active calls, not at cleanup.
- **Cause**: Old STT code created a pump task via `loop.create_task(self._process_events())`
  which then invoked `on_final` in ITS context — a context that never had `set_active_insurance`
  called in it. Everything downstream (`asyncio.create_task` for debounce, then
  `handle_user_speech`) inherited that empty context. Under a single call this sometimes
  "worked by accident" (stale ContextVar leftover). Under 6 concurrent calls the accident
  broke immediately.
- **Fix**: `stt_service.py::start_async_event_handler` now captures the WebSocket task's
  context (`contextvars.copy_context()`) at wire-up time. All SDK-thread callbacks dispatch
  through `_dispatch()` which schedules the coroutine on the loop **inside the captured
  context** (`loop.call_soon_threadsafe(_schedule, context=ctx)`). The debounce task and
  everything below it now inherit the correct `active_insurance`.

### 12.3 Late-STT-after-cleanup race (`KeyError: 'tax_id'`)
- **Symptom**: `KeyError: 'tax_id'` in `main.py` right after call cleanup — visible as
  "Task exception was never retrieved" in App Insights.
- **Cause**: STT can emit `on_final` AFTER `ensure_call_cleanup` popped the call from
  `active_calls`. The debounce task then fires `handle_user_speech`, which finds no
  call_state, defaults `visit_data` to `{}`, and `prompt.format(...)` crashes on the first
  `{placeholder}`.
- **Fix**: two layers.
  1. **Guard** at the top of `handle_user_speech` — if `call_state is None`, log a warning
     and return.
  2. **Cancel the debounce task** at the start of `ensure_call_cleanup`. We added a
     `debounce_task: Optional[object]` field to `CallState` and `stream.py::_reschedule_debounce`
     writes the task into it whenever it creates one. Cleanup cancels it before removing the
     call from `active_calls`.

### 12.4 GPT hallucinates member_id characters (U1 → UI)
- **Symptom**: Cigna call. `member_id` was `U16684575`, GPT spoke `UI6684575`. TTS spelled
  it correctly char-by-char — GPT was the substitution point.
- **Cause**: LLM tokenizer confuses look-alike alphanumeric characters (1↔I, 0↔O, 5↔S, 8↔B).
  Happens on alphanumeric IDs, doesn't happen on all-digit IDs (tax_id, npi) or structured
  formats (dates, names).
- **Fix**: two layers.
  1. **Prompt hardening** — added a "CRITICAL: Alphanumeric ID Fidelity" section to the
     top of each spoken-value template (cigna, humana, baylor_scott) telling GPT never to
     substitute look-alikes and to copy character-by-character from the Call Information block.
  2. **Post-process guard** in `llm_service.py::_correct_hallucinated_member_id` — when GPT
     returns `value:<val>`, if `val` is same-length + edit distance ≤ 2 from the authoritative
     `visit_data["member_id"]`, and only for alphanumeric IDs, we substitute the real value.
     Fires a WARN log. Exact matches pass through with no correction, no log.
- **Not touched**: TTS SSML routing, which already correctly spells alphanumeric IDs
  character-by-character.

### 12.5 "No claim" phrasings the real-time detector missed → call marked failed
- **Symptom**: IVR said "I couldn't find any claims on that date of service..." — a genuine
  successful outcome (payer confirmed no-claim). Real-time `is_claim_not_found` missed the
  phrasing. `finalized_claims` empty → fell into failure classifier → classified as
  "call failed" ❌.
- **Fix**: three layers of defense.
  1. **Broaden the real-time triggers** in `claims_helpers.py` — shorter substrings that
     match more variants; added `_normalize_for_match()` (folds curly apostrophes `’` → `'`,
     collapses whitespace).
  2. **Add "no claim" as a status in the failure classifier** — `_ALLOWED = {"no claim",
     "patient not found", "call failed"}`; new `_NO_CLAIM_FOUND_PATTERNS` checked at
     PRIORITY 0 (before patient-not-found); GPT prompt updated with "no claim" definition
     + priority rules.
  3. **Route "no claim" to SUCCESS** in `post_call_upload.py`'s else-branch — no longer
     hardcodes `REQUEST_STATUS_FAILED`; trusts the classifier's verdict.

### 12.6 "Claims found" phrasings the real-time detector missed → call marked failed
- **Symptom**: mirror of 12.5. IVR started reading claims but our `is_claim_start` didn't
  match. Call ended with no `finalized_claims` and no matched `NO_CLAIM_FOUND` pattern.
- **Fix**: `claim_mode` GPT-driven fallback. Added a new response format in the prompt:
  ```
  - claim_mode → the IVR just started reading claim details (e.g. "I found your claim",
    "I found 2 claims", "here's the first claim"). Return ONLY the exact word claim_mode.
  ```
  In `main.py::handle_user_speech`, after the GPT call, `_is_gpt_claim_mode_signal()` checks
  for it (strict-equality on compact form — no collision with any other format). If matched,
  calls the shared helper `_enter_claim_mode_and_forward()` — same wiring as the real-time
  `is_claim_start` path.
- **Defense in depth**: `llm_service.py::_process_llama_response` also silently swallows
  `claim_mode` (log-only, no action) so it can never fall through as "unrecognized".

### 12.7 Cigna NEXT trigger too narrow
- **Symptom**: Cigna IVR listing options said `"you can say repeat that fax the full list
  next item previous item or..."`. Our claims_controller prompt required `"press 3 for next
  item"` or `" 4 next claim"` — bare `"next item"` didn't match → GPT returned STOP → we
  stopped listing before all claims read.
- **Fix**: broadened PRIORITY 2 in `cigna_claims_controller_template.txt` to also accept
  bare `"next item"` and `"next claim"`. Added a matching example. Priority 4 STOP rule
  still says "NEVER RETURN STOP IF THERE ARE WORDS LIKE NEXT CLAIM, DETAILS" — reinforced.

### 12.8 IVR/ClaimStatus 400 (data seeding)
- **Symptom**: PATCH IVR/ClaimStatus returns `Success=false, Message='Update Ivr claim status
  operation failed.' ErrorCode=400` — even though our payload is correct.
- **Cause (working hypothesis)**: test visit_id is in the Clinical API but NOT in the parent
  Visits table that Ivr/ClaimStatus writes to. Oracle FK violation manifests as this generic
  400. Same call flow with a real production visit_id succeeds.
- **Action items**: (a) test with real visits; (b) enhance logging to include the full
  response body so future failures self-diagnose.

### 12.9 Port 5000 already in use
- **Cause**: previous `python main.py` still holding port on the dev machine.
- **Fix**: `netstat -ano | findstr :5000` → `taskkill /PID <pid> /F`.

### 12.10 Character-substitution history
- Old versions of `is_claim_start` had `"the first claim"` as a trigger. Cigna IVR sometimes
  says "if this is the first claim you filed with this tax ID..." while REJECTING the tax
  ID — false-positive fired claim_mode → call was logged as a "successful unknown" claim.
  Removed. See comment in `claims_helpers.py`.

---

## 13. Testing

- **`test_cigna_prompt.py`** (gitignored) — regression harness for the Cigna claims_controller
  prompt. Feeds handcrafted transcripts + `last_response` seeds through GPT and checks the
  one-word verdict (DETAILS/NEXT/STOP/CONTINUE). Extend by appending tuples to `CASES`.
  Run: `python test_cigna_prompt.py`. Requires `.env` with `PTU_API_KEY`/endpoint.
- **`test.py`** (gitignored) — legacy sandbox.
- No formal pytest suite yet. Manual testing is via real Telnyx calls to sandbox IVR lines,
  observing behaviour in App Insights.

---

## 14. Non-obvious quirks / gotchas

- **`is_tts_active` gates media forwarding**. While TTS is playing, we STOP feeding inbound
  audio into STT so the payer doesn't hear our own voice bounced back. Managed in
  `stream.py`. If you touch TTS or STT lifecycle, keep this invariant.
- **`ensure_call_cleanup` is idempotent + guarded by `cleanup_lock`**. Called from webhook,
  WebSocket finally, LLM `endcall`, auto_hangup, shutdown. All paths must remain safe to
  call multiple times.
- **Post-call upload is fire-and-forget** (`asyncio.create_task`). We hold a strong
  reference in `_pending_upload_tasks` so it isn't GC'd mid-flight. On shutdown, we
  `drain_pending_uploads(timeout=8s)` BEFORE closing the shared http client.
- **The claims_controller has a duplicate-prevention rule**: if last response was NEXT and
  the transcript says next again, return CONTINUE. Prevents infinite loops. Don't remove.
- **Cigna has `dedupe_chunks=True`**. If a new claim chunk is near-identical to the last
  one we sent to the claims_controller GPT, we skip the call. Fixed a race where trailing
  STT chars produced duplicate DETAILS/NEXT responses.
- **`raw_full_transcript` vs `finalized_claims`**: `finalized_claims` is populated by
  `claims_agent.end_session()` ONLY if claim_mode was ever entered. If we never entered
  claim_mode, `finalized_claims` is empty even if claim text is in `full_transcript`.
- **Never log PHI**: Clinical client is careful to log only visit_id, payer_id, plan_name,
  plan_description. Not member_id, member_name, DOB, DOS. Follow the same discipline for
  any new logging.
- **Log truncation at 500 chars**: HTTP-level failures log `resp.text[:500]`. If you're
  debugging a mystery error, increase temporarily.

---

## 15. How to add a new insurer

1. Add an `InsuranceConfig` entry to `INSURANCE_CONFIGS` in `src/config/insurance_config.py`
   with the phone number, timing knobs, and template names.
2. Add the payer_id → insurer mapping to `PAYER_ID_TO_INSURANCE` in the same file.
3. Add required fields to `src/services/clinical/required_fields.py` (which visit_data
   keys must be present for this insurer before we make the call).
4. Create `src/core/prompts/templates/<insurer>/`:
   - `<insurer>_prompt_template.txt` — main IVR conversation. Include the "CRITICAL:
     Alphanumeric ID Fidelity" section if the payer uses spoken member_ids. Add
     `claim_mode` to the response format list.
   - `<insurer>_claims_controller_template.txt` — the DETAILS/NEXT/STOP/CONTINUE prompt.
     Keep the duplicate-prevention rule.
5. Register the template names in `src/core/prompts/manager.py` and
   `src/core/prompts/claims_prompts.py`.
6. Test with `test_cigna_prompt.py` as a model — copy it to `test_<insurer>_prompt.py`,
   add to `.gitignore`, seed a few transcripts.

If the payer's flow is DTMF-only (no spoken IDs), skip the "Alphanumeric ID Fidelity"
section and the `value:` handling in the prompt (see Oscar / HealthFirst).

---

## 16. How to add a new feature

1. **Understand where in the flow it belongs** (section 3). Most features are one of:
   (a) new IVR prompt patterns → prompt template changes,
   (b) new status vocabulary → `classifier.py` / `failure_classifier.py` + downstream
   mapping in `claim_status_client.py`,
   (c) new post-call action → `post_call_upload.py`,
   (d) new endpoint → new file under `src/api/v1/` + router mount in `main.py`.
2. **Preserve invariants**:
   - Any new outbound HTTP call MUST use `get_http_client()` (never `httpx.AsyncClient()`).
   - Any code that touches `active_calls` after cleanup MUST guard against `None`.
   - Any new ContextVar-dependent logic MUST be reachable from the STT callback context
     (Adan's `contextvars.copy_context()` snapshot handles it; don't fight the pattern).
   - Never log PHI.
3. **Layer defense** if the feature depends on GPT/STT:
   - Real-time cheap detector first.
   - Post-call semantic classifier as backup.
   - Log both when they fire so you can KQL-measure the safety-net effectiveness.
4. **Test with the prompt harness** BEFORE running full calls. GPT calls are cheap, real
   Telnyx calls are not.
5. **Add App Insights KQL queries** for the new signals to `# Verification path` sections
   in the commit message so ops can monitor.

---

## 17. Useful KQL queries

```kql
// All logs for one call
traces | where message contains "c=DthoBVG0" | order by timestamp asc

// All logs for one visit
traces | where message contains "v=88231" | order by timestamp asc

// GPT member_id hallucination corrections
traces | where message contains "GPT hallucinated member_id: sent"

// No-claim caught by post-call safety net (real-time detector missed)
traces | where message contains "'no claim' (matched" and message contains "post-call safety net"

// GPT-driven claim_mode fallback (real-time is_claim_start missed)
traces | where message contains "GPT signaled claim_mode (safety net"

// IVR/ClaimStatus PATCH failures
traces | where message contains "IVR/ClaimStatus Success=false"

// Auto-hangups (calls exceeded max duration)
traces | where message contains "auto_hangup" | summarize count() by cloud_RoleInstance, bin(timestamp, 1h)
```

---

## 18. Contact / handoff

- **Repo**: https://dev.azure.com/PracticeEHR/PracticeEHR-AI/_git/PracticeEHR-AI-Billing-Agents
- **Prod App Service**: `as-ai-billing-agent-prod-cus` (Central US)
- **Other dev on repo**: Adan Abbas (Azure DevOps)
- **Backend team** (PracticeEHR APIs, visit seeding): Waseem
- **Frontend integration**: consumes `/v1/Billing-Agent/Call`, displays `RefNo` returned in
  the response, then polls Billing-Agent/Log for the final outcome.

---

# 19. Denial follow-up (in-call pivot) — full context & session handoff (updated 2026-07-27)

> This is the running record of the denial-follow-up feature. Read this first if you're
> picking up the work. It captures **what we built, every problem we hit, the wrong turns,
> and the solution we landed on** — so you don't repeat the circles we already went around.
> Active branch: **`feature/denial-follow-up`** (off `main`, clean fast-forward — no conflicts).
> Commits so far: `65e4877` (classification + memory leaks), `822c6a3` (CIGNA + voice + auth).

## 19.0 The one-paragraph summary
When a normal claim-status call discovers the claim is **DENIED**, the SAME call **pivots**:
it asks the IVR for a live human, and once a rep/advocate answers, it works through a mapped
set of questions to learn WHY the claim was denied (mapped to a **13-reason registry**) and
what's needed to fix it (docs, fax, deadline, primary carrier, ICN, …). No second call, no
separate endpoint — the provider was already authenticated to the IVR during the status flow.

## 19.1 Product decisions (locked, don't relitigate)
- **In-call pivot, NOT a separate endpoint** (manager decision). The old `denial-inquiry-wip`
  branch (see §8) is **IGNORED** — we only read its rep-template *text* as reference; no code
  reuse. Everything was written fresh on `feature/denial-follow-up`.
- **Reason is discovered during the call** (from the IVR readout if it names it, else ask the rep).
- **Priority order we built in:** (1) the pivot itself, (2) the 13-reason engine, (3) persistence LAST.
- **Gated OFF by default.** Pivot fires ONLY when `supports_denial_inquiry` (per payer config)
  **AND** `denial_follow_up: true` (per request, default false). Feature-off ⇒ behaviour is
  byte-identical to today's claim-status flow. This is why it's safe to deploy dormant.
- **All 13 reasons supported**, not just one.
- **Test with the dev endpoint + inline data** (`/v1/Billing-Agent/Call/Test`), which bypasses
  the Clinical API. This is how every test call in this feature was run.

## 19.2 Architecture
- **Phases** on `CallState.phase`: `claim_status` → `denial_ivr` (goal: reach a human) →
  `denial_rep` (free conversation). Set/advanced in `main.py`.
- **Live denial detection** during claim_mode: `src/core/denials/detection.py`
  - `denial_signal()` → `strong` (pivot without GPT) / `weak` / `none`. STRONG cues = "was denied",
    "denied because", etc. WEAK cues include the **Cigna** phrasings **"no payment was made"**,
    **"not covered"**, "not payable" — Cigna never says "denied", so its signal is *weak* and the
    pivot runs a GPT confirm (`confirm_denial_via_gpt`) before pivoting. Negation + conditional guards.
- **13-reason registry**: `src/core/denials/denial_reasons.py` — each reason has
  `trigger_keywords` (cheap rules) + `questions` (what the biller needs; Q1 usually the ICN).
- **Reason classifier**: `src/core/denials/reason_classifier.py` — see §19.3 (GPT is authoritative).
- **Per-turn goal block**: `src/core/denials/context.py` `render_denial_context()` renders the
  `{denial_context_block}` (the reason + its question list + evaluation rules) each turn.
- **Templates** keyed by `(insurance, phase)` in `src/core/prompts/manager.py`:
  `("HUMANA","ivr"/"representative")`, `("CIGNA","ivr"/"representative")`.
- **Payer-parameterized "reach a human" phrase**: `InsuranceConfig.denial_ivr_request_phrase`
  — **"Representative"** for Humana (the default), **"customer service advocate"** for Cigna.
  Spoken at the pivot in `main.py::_pivot_to_denial_flow` (was hardcoded "Representative"; now config).
- **Rep-turn serialization**: per-call `asyncio.Lock` + sequence guard in `_handle_denial_speech`
  (fixed a double-TTS glitch). Background GPT reason-classification task (cancelled in cleanup).
- **Hold watchdog** (`_hold_watchdog`, 180s) says "Hello, are you still there?" if the rep parks us
  and wanders off (capped nudges). **CASE 0 presence checks** in the rep prompt always answer
  "Hello?/are you there?" — silence made reps think the line dropped.

## 19.3 Reason classification — "GPT is authoritative"
**The rule:** GPT decides the denial reason. Keyword rules only drop an **instant provisional guess**
(so the next turn already has a question checklist); GPT runs on every substantive rep turn and
**overrides** the guess. Flag: `CallState.denial_reason_provisional`. This session it also runs
**synchronously** on the first clear reason cue (see §19.14.5) so the very next question set is right.
- **Out-of-scope**: if GPT finds the reason matches none of the 13, → graceful wrap-up, verbatim to billing.
- **Gotchas that will re-bite if changed:** (1) `_strip_boilerplate()` must run before rule matching —
  generic legalese ("...medical necessity...") once mislabeled a denial; the bare "medical necessity"
  trigger was removed. (2) `match_reason_rules` picks the **longest / most specific** matched phrase,
  not first-in-registry — a real CARC phrase must beat a stray generic word ("billing error").

## 19.4 Rep/advocate conversation — the active rules
- **GPT evaluates each answer itself** (no auto-`[ANSWERED]` markers): a stall ("let me check") or a
  *related* remark ("Humana is secondary" — not the primary carrier's NAME) is NOT the value; keep pursuing.
- **COVERAGE, not markers.** Per-question `⬜/✔` keyword markers were abandoned (the bot paraphrases →
  false re-ask loops). GPT instead self-checks the last-N history ("have I asked each numbered
  question?"); CASE 4 blocks `endcall` until every mapped question is asked or the rep can't provide it
  (conditional questions skipped when N/A).
- **Never read a placeholder aloud.** Missing fields render as `__UNKNOWN__`; defer with what you DO have
  ("I don't have the facility name, but the provider is …").
- **Partial denial ≠ paid.** `post_call_upload._determine_outcome` forces `claim_status="denied"` when
  `denial_pivoted`.
- **Dead-air stall** — largely addressed this session (TTS pacing + rep debounce + CASE −1 wait-for-human,
  §19.14.2/§19.14.6). Root cause was the half-duplex gate (`stream.py:~253`) dropping rep audio while the
  bot's TTS plays. Remaining lever if it recurs: buffer/feed-silence to keep the recognizer stream alive.

## 19.5 Cigna denial flow — active facts
- **Cigna's IVR reads the denial reason aloud**, so the reason is often classified **from the readout
  before the advocate picks up** (Path A).
- **Reaching a human:** say **"customer service advocate"** → IVR asks **"are you calling about
  multiple patients?"** → answer **"No"** → it connects. "multiple patients?" must **NOT** be treated
  as a transfer (verified `is_transfer_signal(...) == False`); the `cigna_denial_ivr_template` answers "No".
- **Reason trap:** Cigna's "not covered *amount*" is amount-language, not a reason — keyword rules
  fell for it, GPT correctly saw through it (real case classified `invalid_dx`).
- **Files:** `cigna` config (`supports_denial_inquiry=True`), `cigna_denial_ivr_template.txt`,
  `cigna_denial_rep_template.txt`. The shared COB example is payer-neutral ("we are secondary").
- **Rep can't always answer, and that's OK** — CASE 3 handles "I can't provide that" gracefully
  (acknowledge the gap, never pretend); it's a data limitation, not a bot failure.

## 19.6 Cigna CLAIM-STATUS fixes (every Cigna claim call, denial flag or not — `cigna_prompt_template.txt`)
- **Name-confirm sound-alikes:** always CONFIRM through mangled/sound-alike readbacks (STT turned
  "Kathryn, right?" into "Catherine Wright") — the patient is already matched by member ID + DOB, so
  the readback is just Cigna confirming its own lookup. Still **reject a genuinely different person**
  (verified "John Smith" → dtmf 2).
- **Caller identity:** the IVR asks the CALLER (us) to "say and spell your first and last name" — this
  is the **agent**, not the patient. Agent name is spelled out; env `DENIAL_AGENT_PERSONA_NAME` +
  `DENIAL_AGENT_PERSONA_LAST_NAME` (persona now **Marcus Bell**, §19.14.0).
- **Claim-status history:** `main.py::_format_claim_history` passes the **last 2–3 turns** as
  `{conversation_history}` (short on purpose). CIGNA-only; other payers ignore the extra key.
- **Provider address:** added to the knowledge sheet (from `visit_data`, `__UNKNOWN__`-safe).

## 19.7 Voice  ⚠️ SUPERSEDED — see §19.14.2 (voice is now Google Chirp3-HD via `TTS_PROVIDER=google`, with an Azure "spells" hybrid). The Azure/Ava details below still describe the Azure fallback path.
- `src/services/azure/tts_service.py`: switched **`en-US-JennyNeural` (2021) → `en-US-AvaMultilingualNeural`**
  and added a **+6% rate** on prose ONLY (dates & spelled codes stay at default speed for clarity).
- **Env-configurable / revertable:** `AZURE_TTS_VOICE` (set to `en-US-JennyNeural` to revert, or
  `en-US-EmmaMultilingualNeural` to try Emma), `AZURE_TTS_RATE` (`+0%` to disable).
- **No `express-as` style** — Ava (multilingual) doesn't reliably support styles; adding one risks a
  broken SSML tag. If you want styles, Jenny supports them (`customer-service`, `chat`).
- **Caveat:** if Ava isn't available in the deployment's Azure Speech region, TTS fails → revert via env.

## 19.8 Test-endpoint auth + baked config (this session)
- `/v1/Billing-Agent/Call/Test` now requires **JWT** (`verify_token`, same as prod) and the
  **`ENABLE_TEST_CALL_ENDPOINT` env gate was removed**. → Test calls MUST send
  `Authorization: Bearer <token>`, and **`JWT_SECRET_KEY` must be set** in the environment.
- Persona/callback values are now **code defaults** (no env needed): agent **Marcus / Bell**
  (was Miranda — changed this session, see §19.14.0), callback **469-581-2936**. Env can still
  override. Delete any old `DENIAL_AGENT_PERSONA_NAME=Miranda` from QA/prod env.

## 19.9 Memory-leak audit (done)
Audited; leaks #1–4 fixed: `claims_agent._sessions`/`_locks` popped on `end_session`; `orchestrate.py`
pops a stranded `CallState` on watchdog-arm failure; denial pivot reorders timer create-before-cancel;
`conversation_history` capped at 400 turns (`add_history()` shifts `denial_history_start`; `full_transcript`
NOT capped). **Not fixed (reliability, not RAM):** the STT dispatch task (`stt_service.py:~245`) is
fire-and-forget with no strong ref → can be GC'd mid-turn ("Task was destroyed") — strong-ref-set fix
worth doing later.

## 19.10 Current state, deploy, and OPEN ITEMS
**State:** branch `feature/denial-follow-up` is a clean fast-forward over `main` (95cd1ed / PR 3588).
Pushed to **`origin` (GitHub)**; the deploy repo is **Azure DevOps `azure-new`**
(`.../PracticeEHR-AI/_git/...`) — push there for QA (`git push -u azure-new feature/denial-follow-up`).
Lead's plan: PR against `main`, deploy to QA. Merge is conflict-free.

**Deploy checklist / gotchas:**
- Set **`JWT_SECRET_KEY`** on QA (test endpoint now needs a Bearer token).
- Confirm **Ava** voice exists in QA's Azure Speech region (else `AZURE_TTS_VOICE=en-US-JennyNeural`).
- Test the denial flow via **`/Call/Test`** (inline data) **with the Bearer token** — bypasses the
  Clinical-API data gap.

**Production denial-flow prerequisites (NOT a deploy blocker; the feature is dormant until then):**
- **WHO sets `denial_follow_up: true`** on a real `/v1/Billing-Agent/Call` request? — frontend
  checkbox vs per-customer default. **UNDECIDED — product decision.**
- Clinical API should return `provider_address` (optional; bot defers gracefully if absent).

**Open work / next steps:** ⚠️ **See §19.14.9** — the current, authoritative open-items list (the
old items here were done this session: dead-air addressed, persistence built). Still designed-not-built:
**multi-practice scaling** — move agent name / outbound number / callback from global env to a
per-practice config keyed by `customer_id`, resolved at call start in `orchestrate.py`.

## 19.11 Constraints & preferences (honor these)
- **Commit only when asked** — the user says explicitly what to commit ("donot commit each change").
- **Don't modify other insurances when working on one** — CIGNA work was kept fully independent;
  Humana/Baylor/Oscar were left byte-for-byte unchanged wherever possible (extra prompt kwargs are
  simply ignored by templates that don't reference them). Humana's rep template was intentionally
  NOT given provider-address / first+last spelling to avoid touching it.
- **Verify after every change** with `./venv/Scripts/python.exe -c "from main import app; print('OK')"`
  and, for prompt logic, a quick `_call_gpt_api` probe.
- **Agent persona:** Marcus Bell (was Miranda — changed this session). Voice = Google Chirp3-HD
  (male-leaning), Azure as fallback. User wants it to sound like a real person. See §19.14.0/§19.14.2.

## 19.12 Reliable test data (via `/Call/Test`, inline `visit_data`, Bearer token)
- **Humana — Terry Mosley** (COB, DOS 11/20/2024): the most reliable end-to-end case (auth passes,
  real denied claim, primary carrier "AMERIBEN"/Meritain captured).
- **Humana — Thomas Gooden** (docs_required, DOS 04/27/2026): fax 866-305-6655, 18-mo deadline.
- **Cigna — Ronald Lovejoy** (member `U5121015901`, DOS 01/09/2025): OLD/closed → advocate can't
  answer, but exercises the full pivot→advocate→graceful-decline flow. `missing_info` reason.
- **Cigna — Kathryn Burg** (DOS 04/10/2026, $3795.55): fresher; provider James Bainbridge, Metro
  Denver Pain Management. (Note Cigna member IDs may start with "U"; the name step now survives the
  "Kathryn"→"Catherine Wright" STT mangle.)

## 19.13 The TEST endpoint we built — `POST /v1/Billing-Agent/Call/Test`
**File:** `src/api/v1/orchestrate_test.py` (router wired in `main.py`). This is how EVERY denial
test call in this feature was placed. It exists because the denial pivot needs to run against a
**real denied claim**, and the Clinical API has no seeded data for those — so this endpoint lets us
supply the visit data **inline** and dial a real payer IVR.

**What it does / how it differs from prod `/v1/Billing-Agent/Call`:**
- **`visit_data` comes INLINE in the request body** — no Auth API, no Clinical API lookup.
- **Routed by `insurance` NAME** (e.g. `"CIGNA"`, `"HUMANA"`), not a `payer_id`.
- **`denial_follow_up`** is passed inline (set it `true` to arm the pivot).
- **No `Billing-Agent/Log` row** is written (`ref_no=None`).
- **`CallState.is_test=True`** → the post-call pipeline runs the REAL outcome classification but
  **LOGS the would-be writes** ("🧪 TEST MODE — post-call summary …") instead of touching any
  PracticeEHR endpoint. So it never pollutes real data, but you still see exactly what it *would*
  have written (claimStatus, description, denial_reason_key, verbatim).
- **Recording:** not uploaded to PracticeEHR (test mode skips it). Pull it from **Telnyx** using the
  `call_session_id` printed in the `post_call_upload START` log line.
- **Everything else is the REAL production path** — Telnyx dial, webhooks, WebSocket audio,
  STT/GPT/TTS, the claim-status flow, AND the denial pivot. So a green test call ⇒ the real flow works.

**Auth (changed this session — see §19.8):** now requires **JWT** (`verify_token`, identical to the
prod endpoint). Send `Authorization: Bearer <token>` and make sure **`JWT_SECRET_KEY`** is set in the
environment. (The old `ENABLE_TEST_CALL_ENDPOINT` env gate was REMOVED.)

**Request body example:**
```json
{
  "insurance": "CIGNA",
  "visit_id": "TEST-CIGNA-96251681",
  "denial_follow_up": true,
  "visit_data": {
    "tax_id": "320333017", "npi": "1528365863", "member_id": "110141034",
    "member_name": "Kathryn Burg", "dob": "09/08/1948", "dos": "04/10/2026",
    "billed_amount": "3795.55", "provider_name": "James Bainbridge",
    "provider_address": "10700 E Geddes Ave Suite 100, Englewood, CO 80112-3861",
    "practice_name": "Metro Denver Pain Management PLLC", "group_npi": "1528365863"
  }
}
```
Required `visit_data` fields per payer are enforced by `missing_fields_for()` (tax_id, npi, member_id,
dob, member_name, dos for Cigna/Humana); `provider_address`, `practice_name`, `group_npi` are
optional and default to the `__UNKNOWN__` sentinel (bot defers gracefully).

═══════════════════════════════════════════════════════════════════════════════
# 19.14 SESSION 2 (2026-07-31 → 08-01) — updates, new work, problems & solutions
═══════════════════════════════════════════════════════════════════════════════
> Read this section for the CURRENT state — it supersedes older bits of §19.7 (Voice)
> and §19.10 (open items) noted below. Active branch is still **`feature/denial-follow-up`**
> (deployed to QA). Two small PRODUCTION fixes were split off `main` and merged (see §19.14.1).

## 19.14.0 What changed at a glance
- **Payers with denial pivot now: HUMANA, CIGNA, BAYLOR_SCOTT, OSCAR** (was Humana+Cigna).
- **Persona is now MARCUS BELL** (was Miranda Bell). Env `DENIAL_AGENT_PERSONA_NAME` default
  is `Marcus`; the M-A-R-C-U-S spell-out examples in all prompts were updated. If QA env still
  has `DENIAL_AGENT_PERSONA_NAME=Miranda`, remove it or set `Marcus`.
- **Voice is now Google Chirp3-HD** (more human) selectable via `TTS_PROVIDER=google`; Azure
  remains the default and the fallback. See §19.14.2 — this REPLACES §19.7's "Ava" story.
- **Persistence (the big §19.10 open item) is DONE** — a denial summarizer now writes the rep's
  reasons + corrective actions into the billing description. See §19.14.4.

## 19.14.1 Two production fixes merged to `main` (separate branches, now deleted)
- **`fix/live-agent-transfer`** — Cigna claim-status prompt: END the call when the IVR transfers
  to a live agent (it used to `fallback` forever → zombie call → risk of the number being blocked).
  PROMPT-ONLY, two CRITICAL blocks in `cigna_prompt_template.txt`: (1) transfer announced → `endcall`
  even if a follow-up question is bundled; (2) a live person greets by name → `endcall`. Guarded so
  the automated opening greeting and the Phase-4 "how may I help you" menu do NOT false-fire.
- **`fix/claim-summary-icn`** — `classifier.py` (`classify_claim`) now includes the **claim number /
  ICN** in the summary (reproduce digits exactly, never fabricate). It was dropping the ICN even
  though the IVR reads it out.
- Both merged to `main` via Azure DevOps PRs, then the local branches were deleted. When
  `feature/denial-follow-up` eventually merges to `main`, its Cigna prompt differs (callback/facility
  additions) — expect a small merge reconciliation on `cigna_prompt_template.txt`.

## 19.14.2 Google TTS engine + the "Azure spells, Google talks" hybrid
- New engine: `src/services/google/tts_service.py` (Chirp3-HD). Selected by `TTS_PROVIDER=google`
  (default `azure`). Emits the SAME 8 kHz mulaw wire format as Azure → NO change to Telnyx answer
  payload, STT, or stream.py. Bound in `main.py` behind the same `speak_with_azure` callable name so
  no caller changed.
- **Creds for cloud**: `get_tts_client()` reads `GOOGLE_APPLICATION_CREDENTIALS_JSON` (the key's
  JSON *content* pasted into an env var — preferred on Azure App Service), else falls back to
  `GOOGLE_APPLICATION_CREDENTIALS` (a file path, local dev). The lead chose to also commit the key
  under `src/secrets/` (against my advice) — it's referenced by path in env.
- **PROBLEM: Chirp3-HD cannot spell.** It has no SSML `say-as`, renders isolated letters/digits
  mushy, and warbles at the very start of an utterance. Member IDs like "BSW…" came out garbled and
  IVRs rejected them; NPIs read as "one billion…".
  **SOLUTION (hybrid):** `_should_spell_via_azure()` routes a STANDALONE code/ID token to the AZURE
  engine (crisp `<say-as>`), everything else (prose, money amounts, dates) stays on Google. Money
  amounts (a decimal) are explicitly excluded from spelling. For IDs embedded INSIDE a sentence
  (rep phase), `_shape_text_for_chirp` spells any run of 7+ digits digit-by-digit so an NPI in a
  sentence isn't read as a cardinal. Trade-off: the ID is spelled in the Azure voice mid-conversation;
  fine because IVR = machine, and rare in the rep phase.
- **PROBLEM: choppy/breaking voice on normal speech.** The chunk streaming used real-time pacing
  (800 B = 100 ms of audio, `sleep(0.1)`) → ZERO buffer headroom; when the event loop was busy
  (STT callbacks, background GPT), the next chunk was late → Telnyx underran → audible breaks.
  **SOLUTION:** send frames AHEAD (`SEND_PACING=0.015`) so Telnyx buffers a cushion, THEN hold the
  half-duplex echo gate (`is_tts_active`) for the TRUE audio duration (`len/8000` s) via a
  `time.monotonic()` wait — decoupling "sending" from "gate timing" so the mic doesn't re-open early
  and transcribe the bot's own voice. This is the key insight: the old real-time pacing was doing
  double duty (pace + echo-gate); they had to be split.

## 19.14.3 Baylor Scott + Oscar denial flows (new payers)
- **Baylor** (`baylor_scott/baylor_denial_ivr_template.txt` + `_rep_template.txt`, registered in
  `manager.py`, config `supports_denial_inquiry=True`, `denial_ivr_request_phrase="Representative"`).
  Reaches a rep by saying "Representative" → IVR confirms ("...representative or customer service
  advocate?") → `confirm:yes`. Claim prompt got: main-menu `dtmf:2` (more reliable than saying
  "claim status"), DOB/DOS via **DTMF** (the handler strips the slashes), and a Step-5b retry rule.
- **Oscar** reaches a human by **KEYPAD**, not a spoken phrase. New config field
  **`denial_ivr_request_dtmf`** (Oscar = `"2"`); the pivot sends this DTMF (`send_dtmf`) instead of
  speaking. Flow: press 2 (denied-line details / auto-transfer) → denial-IVR template presses 3 for
  the rep. Templates: `oscar/oscar_denial_ivr_template.txt` + `_rep_template.txt`, registered.
- **LATENT BUG (still present):** `main.py::_pivot_to_denial_flow` calls
  `config_manager.get_denial_ivr_request_phrase()` — **that getter does NOT exist** → it AttributeErrors
  every pivot and is caught ("Pivot request failed…"). Net effect: the pivot NEVER proactively speaks
  the request phrase for spoken-phrase payers; the denial-IVR template drives the next turn instead.
  We added `get_denial_ivr_request_dtmf` (works, Oscar uses it) but deliberately did NOT add the
  phrase getter, to avoid changing tested Humana/Cigna/Baylor pivot behavior. Fix it only if you
  want the pivot to speak "Representative" at the pivot moment.

## 19.14.4 Persistence — the denial summarizer (the §19.10 open item, now DONE)
- New module `src/services/billing_log/denial_summarizer.py::summarize_denial()` — a GPT pass over the
  REP-phase transcript that produces the biller-actionable description: denial reason(s) in the rep's
  words, the CORRECTIVE ACTION per denied line, and any fax/portal/mailing-address/deadline/primary
  carrier. Wired into `post_call_upload._determine_outcome` (denial branch); falls back to the
  claim-readout summary if it can't build. Runs post-call (zero call latency). Uses only transcript
  facts, reproduces the ICN exactly.
- **NOTE on the endpoint:** the lead wants the rich denial summary to eventually go to a NEW dedicated
  endpoint (not the existing `Billing-Agent/Log` `description`, which has only one free-text field).
  For now it goes into `description`. `_determine_outcome` already computes `claim_readout_summary` and
  `rep_summary` separately, so wiring a `denialDescription` column later is a one-liner.

## 19.14.5 Reason classification — now SYNCHRONOUS on first clear reason
- **PROBLEM:** classification was background-only and lagged / got cancelled at cleanup, so
  `denial_reason_key` was often `None` at the end → the reason-specific questions never surfaced and
  the outcome logged "denial reason not identified" even though the rep clearly said it.
- **SOLUTION:** `main.py::_ensure_reason_classified_sync()` — when the rep clearly states a reason
  (`has_reason_cue`) and no confirmed reason is set yet, classify SYNCHRONOUSLY (one ~1 s blocking
  call on that turn) BEFORE building the response prompt, so THAT turn already carries the mapped
  questions and the outcome label is correct. Keyword provisional + background fallback still cover
  the no-clear-cue case (and become no-ops once locked).

## 19.14.6 Rep-conversation behavior fixes (generic — all four rep templates)
Each was a real defect seen on a live call:
- **CASE −1 "wait for a human":** the phase can flip to `denial_rep` DURING the hold/transfer (via
  `is_transfer_signal` on hold phrases), so the bot was asking rep questions to the hold system before
  anyone picked up. Rule: until a real person GREETS you (personal-name intro), `fallback` to
  everything (hold music, "transferring you now", wait-time, the automated line reading a number).
  Relies on the denial-phase history showing no human greeting yet.
- **Short name re-ask:** first "your name?" = full intro; a later re-ask = just "It's Marcus Bell."
- **Ask the reason ONCE:** not during verification (answer their questions and wait), never re-ask if
  already asked, never tack it onto a verification answer.
- **Corrective-action-before-wrapup + multi-denial:** CASE 4 now requires "what do we need to correct/
  submit to reprocess?" before ending, and treats each denied line as its own reason+fix.
- **Accept offered details:** when the rep OFFERS a fax/mailing-address/portal/claim-number, say
  "Yes, please, go ahead" (it's captured) — only decline a pure RE-READ of a number already given.
  (Old rule wrongly declined ALL numbers, so we lost the where-to-send-the-corrected-claim.)
- **Payer system down / "can't access the claim":** ask ONCE "is there a better time to call back, or
  someone who can access this?", then wrap up.
- **ICN made PASSIVE:** `UNIVERSAL_QUESTIONS` is now empty — never actively ask the rep for the ICN
  (it's in the readout; asking confused reps). It still lands in the summary from the readout.

## 19.14.7 Reaching the rep — PERSIST (denial-IVR templates)
- **PROBLEM:** on a Humana call the bot asked "Representative" ONCE, the automated line offered
  self-service (claim line details, fax, "anything else?"), the bot said "No" to everything —
  including "anything else?" — and the payer hung up. Never reached a human, got nothing.
- **SOLUTION (Humana/Baylor/Cigna denial-IVR):** decline each alternative AND re-ask for a human in
  the same breath; on "is there anything else?" say "Yes, I'd like to speak with a representative,"
  never "No"; keep steering to a human until `rep_mode` or the IVR says none are available. (Oscar
  already persists via the keypad `dtmf:3`.)

## 19.14.8 Outcome flag fix (post_call_upload)
- **PROBLEM:** denial calls were stamped `requestStatus="call successful"` whenever the call merely
  COMPLETED — even when the rep couldn't give a reason (payer system down) → misleading.
- **SOLUTION:** for a denial call, "successful" now requires a reason to have been obtained
  (`denial_reason_key is not None`, registry key OR out-of-scope). Otherwise `requestStatus="call
  failed"` and the description gets "…a callback is needed."

## 19.14.9 Open items after this session
1. **Deploy + validate on QA** — most of §19.14.5–19.14.8 (sync classify, CASE −1, accept-offered,
   persist-for-rep, outcome flag) were verified OFFLINE and, at time of writing, are UNCOMMITTED on
   `feature/denial-follow-up`. Commit → push → deploy → re-run live calls to confirm. Live STT garble
   and real IVR timing are the things offline tests can't cover.
- Oscar/Baylor need a real DENIED claim on a live call to confirm the pivot, menu wording, and (Oscar)
  whether press-2 auto-transfers vs returns to the menu.
2. **The missing `get_denial_ivr_request_phrase` getter** (§19.14.3) — decide whether the pivot should
  speak the request phrase, then add it.
3. **Dedicated denial endpoint** (§19.14.4) — the rich denial summary is destined for a new endpoint;
  wire `denialDescription` when the backend adds the column.
4. **NPI mapping in test payloads:** `npi` = the group/billing NPI (pairs with tax ID for IVR
  verification); rendering-doctor NPIs differ. Swap only if verification is rejected.
5. **Debugging in App Insights:** the `c=…/v=…` prefix is a CONSOLE formatter only — App Insights
  `traces.message` is the RAW message, so `message contains "<visit_id>"` finds only lines that spell
  it out. Query the custom dimension instead: `customDimensions.visit_id == "…"` (fields set on the
  record in `logging_config.py`), or filter by a time window / `customDimensions.call_id`.

## 19.14.10 Constraints reaffirmed this session
- Commit ONLY when told; **commit messages must NOT reference Claude/AI** (lead's explicit rule).
- Generic behavior fixes were applied across ALL payer rep/IVR templates this session (the earlier
  "don't touch other insurances" rule was relaxed by the user for these shared-quality improvements).
- Verify after each change with `./venv/Scripts/python.exe -c "from main import app; print('OK')"`
  and GPT probes; test live via `/Call/Test` (JWT) with a real DENIED claim.
