import asyncio
import contextvars
import logging
import threading
import os
import time
import azure.cognitiveservices.speech as speechsdk
from azure.cognitiveservices.speech.audio import AudioStreamFormat, PushAudioInputStream
from typing import Dict, Callable, Optional
from enum import Enum
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

AZURE_SPEECH_KEY    = os.getenv("AZURE_SPEECH_KEY")
AZURE_SPEECH_REGION = os.getenv("AZURE_SPEECH_REGION")


class EventType(Enum):
    PARTIAL = "partial"
    FINAL   = "final"
    ERROR   = "error"


@dataclass
class TranscriptionEvent:
    event_type: EventType
    text: str
    websocket_id: str


def convert_mulaw_to_pcm(mulaw_data: bytes) -> bytes:
    """Convert 8 kHz μ-law to 16-bit PCM."""
    import audioop
    return audioop.ulaw2lin(mulaw_data, 2)


class AzureRealtimeSttService:
    """
    Real-time STT using Azure Speech SDK.
    Automatically segments on silence and never stops listening.
    """

    def __init__(self, websocket_id: str):
        self.websocket_id = websocket_id
        self.recognizer: Optional[speechsdk.SpeechRecognizer] = None
        self.push_stream: Optional[PushAudioInputStream] = None
        self.is_running = False
        self.recognition_thread: Optional[threading.Thread] = None
        # Main event loop + the call context (insurance / call-id ContextVars),
        # both captured when the async event handler is wired up. SDK callback
        # threads use them to schedule the async result callbacks (see _dispatch)
        # in the right context — the SDK's own threads carry neither.
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ctx: Optional[contextvars.Context] = None

        # Your callbacks
        self.on_partial_result: Optional[Callable[[str], asyncio.Future]] = None
        self.on_final_result:   Optional[Callable[[str], asyncio.Future]] = None
        self.on_error:          Optional[Callable[[str], asyncio.Future]] = None

    def initialize(
        self,
        on_partial_result: Optional[Callable[[str], asyncio.Future]] = None,
        on_final_result:   Optional[Callable[[str], asyncio.Future]] = None,
        on_error:          Optional[Callable[[str], asyncio.Future]] = None,
        segmentation_silence_ms: int = None,  # Accept per-insurer baseline
    ):
        """Configure the recognizer, enable dictation and silence segmentation."""
        self.on_partial_result = on_partial_result
        self.on_final_result   = on_final_result
        self.on_error          = on_error

        speech_config = speechsdk.SpeechConfig(
            subscription=AZURE_SPEECH_KEY,
            region=AZURE_SPEECH_REGION
        )
        speech_config.speech_recognition_language = "en-US"
        speech_config.enable_dictation()

        # End-of-utterance silence (keep if you use it globally)
        speech_config.set_property(
            speechsdk.PropertyId.SpeechServiceConnection_EndSilenceTimeoutMs,
            "1600"
        )

        # Segmentation silence — now parameterized
        speech_config.set_property(
            speechsdk.PropertyId.Speech_SegmentationSilenceTimeoutMs,
            str(segmentation_silence_ms)
        )

        # Audio format matches Telnyx μ-law stream
        audio_format = AudioStreamFormat(
            samples_per_second=8000, bits_per_sample=16, channels=1
        )
        self.push_stream = PushAudioInputStream(stream_format=audio_format)
        audio_config = speechsdk.audio.AudioConfig(stream=self.push_stream)

        self.recognizer = speechsdk.SpeechRecognizer(
            speech_config=speech_config,
            audio_config=audio_config
        )

        # Wire up event handlers
        self.recognizer.recognizing.connect(self._handle_recognizing)
        self.recognizer.recognized.connect(self._handle_recognized)
        self.recognizer.session_started.connect(self._handle_session_started)
        self.recognizer.session_stopped.connect(self._handle_session_stopped)
        self.recognizer.canceled.connect(self._handle_canceled)

        self.is_running = True

    # ---------- NEW: live update for segmentation timeout ----------
    def update_segmentation_timeout(self, segmentation_silence_ms: int):
        """
        Update the segmentation silence timeout at runtime.
        Preferred: set recognizer property directly.
        Fallback: recreate recognizer with new timeout if live update is ignored.
        """
        if not self.recognizer:
            return
        try:
            # Try live property update
            self.recognizer.properties.set_property(
                speechsdk.PropertyId.Speech_SegmentationSilenceTimeoutMs,
                str(segmentation_silence_ms)
            )
            logger.info(f"[{self.websocket_id}] 🔄 Updated Speech_SegmentationSilenceTimeoutMs to {segmentation_silence_ms} ms")
        except Exception as e:
            logger.warning(f"[{self.websocket_id}] ⚠️ Live update failed: {e}")
            self._recreate_with_new_timeout(segmentation_silence_ms)

    def _recreate_with_new_timeout(self, segmentation_silence_ms: int):
        """
        Stop current recognizer and rebuild with the new timeout.
        Reuses the same push stream; rewires event handlers; restarts if it was running.
        """
        try:
            was_running = self.is_running
            # Fully release the old recognizer (handlers + native connection)
            # before building the new one, so a timeout change doesn't leak a
            # Speech Service connection / SNAT port each time it happens.
            self._disconnect_and_release_recognizer()

            # Rebuild config
            speech_config = speechsdk.SpeechConfig(
                subscription=AZURE_SPEECH_KEY, region=AZURE_SPEECH_REGION
            )
            speech_config.speech_recognition_language = "en-US"
            speech_config.enable_dictation()

            # Keep these aligned with your baseline policy
            speech_config.set_property(
                speechsdk.PropertyId.SpeechServiceConnection_EndSilenceTimeoutMs,
                "1600"
            )
            speech_config.set_property(
                speechsdk.PropertyId.Speech_SegmentationSilenceTimeoutMs,
                str(segmentation_silence_ms)
            )

            # Reuse same audio stream
            audio_config = speechsdk.audio.AudioConfig(stream=self.push_stream)
            self.recognizer = speechsdk.SpeechRecognizer(
                speech_config=speech_config,
                audio_config=audio_config
            )

            # Re-wire events
            self.recognizer.recognizing.connect(self._handle_recognizing)
            self.recognizer.recognized.connect(self._handle_recognized)
            self.recognizer.session_started.connect(self._handle_session_started)
            self.recognizer.session_stopped.connect(self._handle_session_stopped)
            self.recognizer.canceled.connect(self._handle_canceled)

            if was_running:
                self.recognizer.start_continuous_recognition()

            logger.info(f"[{self.websocket_id}] ✅ Recreated recognizer with {segmentation_silence_ms} ms")
        except Exception as e:
            logger.error(f"[{self.websocket_id}] ❌ Recreate recognizer failed: {e}")
    # ---------- /NEW ------------------------------------------------

    def start_continuous_recognition(self):
        """Launch the background thread for continuous recognition."""
        if self.recognizer and not self.recognition_thread:
            self.recognition_thread = threading.Thread(
                target=self._recognition_worker, daemon=True
            )
            self.recognition_thread.start()

    def _recognition_worker(self):
        """Thread: run the recognizer until stopped."""
        try:
            self.recognizer.start_continuous_recognition()
            logger.info(f"[{self.websocket_id}] Started continuous recognition")
            while self.is_running:
                # Keep this worker thread alive while the SDK recognizes on its
                # own threads. Plain sleep — asyncio.run() here spun up and tore
                # down a fresh event loop 10x/sec per call, burning CPU.
                time.sleep(0.1)
        except Exception as e:
            logger.error(f"[{self.websocket_id}] Recognition worker error: {e}")
            if self.on_error:
                self._dispatch(self.on_error(str(e)))

    def feed_audio(self, audio_data: bytes):
        """Write each PCM chunk into the push stream."""
        if self.push_stream and self.is_running:
            try:
                self.push_stream.write(audio_data)
            except Exception as e:
                logger.warning(f"[{self.websocket_id}] Error feeding audio: {e}")

    def _dispatch(self, coro):
        """Schedule an async result callback on the main loop from an SDK
        callback thread, INSIDE the call context captured at wire-up, so the task
        (and anything it spawns — the debounce task → handle_user_speech) inherits
        the insurance / call-id ContextVars. The SDK's own callback thread carries
        neither. No worker thread is held — no executor/DNS starvation, no ceiling."""
        loop = self._loop
        ctx = self._ctx
        if loop is None or ctx is None or loop.is_closed():
            # Not wired up yet, or the loop is shutting down — close the coroutine
            # so it isn't GC'd as "never awaited".
            coro.close()
            return

        async def _runner():
            try:
                await coro
            except Exception as e:
                logger.error(f"[{self.websocket_id}] STT callback error: {e}")

        runner = _runner()

        def _schedule():
            # Runs on the loop thread inside `ctx`, so the created task copies
            # `ctx` (insurance + call-id) instead of the SDK thread's empty context.
            try:
                loop.create_task(runner)
            except Exception as e:
                # Very unlikely on a running loop, but keep the cleanup guarantee
                # airtight: release both coroutines on any failure.
                runner.close()
                coro.close()
                logger.error(f"[{self.websocket_id}] failed to schedule STT event: {e}")

        try:
            loop.call_soon_threadsafe(_schedule, context=ctx)
        except Exception as e:
            # Loop closed between the check above and scheduling — release both
            # coroutines so neither leaks as "never awaited".
            runner.close()
            coro.close()
            logger.error(f"[{self.websocket_id}] failed to schedule STT event: {e}")

    def _handle_recognizing(self, evt):
        """Intermediate (partial) results."""
        if evt.result.text and self.on_partial_result:
            self._dispatch(self.on_partial_result(evt.result.text))

    def _handle_recognized(self, evt):
        """Final results (utterance complete)."""
        if evt.result.text and self.on_final_result:
            logger.info(f"[STT {self.websocket_id}] Final: {evt.result.text}")
            self._dispatch(self.on_final_result(evt.result.text))

    def _handle_session_started(self, evt):
        logger.info(f"[STT {self.websocket_id}] Speech session started")

    def _handle_session_stopped(self, evt):
        logger.info(f"[STT {self.websocket_id}] Speech session stopped (session_id={getattr(evt, 'session_id', '?')})")

    def _handle_canceled(self, evt):
        # Always log the full cancellation context. If STT bails without
        # producing transcripts, this is the line that explains why. The real
        # reason/error_code/error_details live on evt.cancellation_details (the
        # top-level evt attributes come back empty on this SDK), so read that —
        # this is what surfaces e.g. ConnectionFailure / WS_OPEN_ERROR when the
        # recognizer can't open its WebSocket to the Speech endpoint.
        reason = getattr(evt, "reason", None)
        code = getattr(evt, "error_code", None)
        details = getattr(evt, "error_details", None)
        try:
            cd = getattr(evt, "cancellation_details", None)
            if cd is None and getattr(evt, "result", None) is not None:
                cd = speechsdk.CancellationDetails(evt.result)
            if cd is not None:
                reason = getattr(cd, "reason", reason)
                code = getattr(cd, "error_code", code)
                details = getattr(cd, "error_details", details)
        except Exception as exc:
            details = f"(could not read cancellation_details: {exc})"
        logger.error(
            f"[STT {self.websocket_id}] ❌ Recognition canceled: "
            f"reason={reason} code={code} details={details!r}"
        )
        if reason == speechsdk.CancellationReason.Error and self.on_error:
            self._dispatch(self.on_error(f"Error: {details}"))

    def _disconnect_and_release_recognizer(self):
        """Detach handlers, stop recognition, and release the native recognizer
        plus its Speech Service connection so the outbound (SNAT) port is freed
        promptly instead of lingering until GC finalizes the SDK object."""
        rec = self.recognizer
        if not rec:
            return
        # Detach handlers first so a teardown-triggered 'canceled'/'stopped'
        # event can't fire our callbacks (e.g. on_error) while we're closing,
        # and so the recognizer↔bound-method reference cycle is broken for GC.
        for signal_name in ("recognizing", "recognized", "session_started",
                            "session_stopped", "canceled"):
            try:
                getattr(rec, signal_name).disconnect_all()
            except Exception:
                pass
        try:
            rec.stop_continuous_recognition()
        except Exception:
            pass
        # Best-effort: close the underlying websocket now rather than waiting for
        # GC. Older SDKs may not expose this; the ref drop below still releases it.
        try:
            speechsdk.Connection.from_recognizer(rec).close()
        except Exception:
            pass
        self.recognizer = None

    def stop(self):
        """Stops recognition and releases the Speech Service connection."""
        self.is_running = False
        self._disconnect_and_release_recognizer()
        if self.push_stream:
            try:
                self.push_stream.close()
            except Exception:
                pass
            self.push_stream = None
        if self.recognition_thread:
            self.recognition_thread.join(timeout=5.0)
            self.recognition_thread = None
        logger.info(f"[STT {self.websocket_id}] Cleanup complete")

    def start_async_event_handler(self, loop: asyncio.AbstractEventLoop):
        """Capture the main event loop AND the current call context. This runs on
        the WebSocket task right after set_call_id() / set_active_insurance(), so
        the snapshot carries those ContextVars; _dispatch schedules the async
        result callbacks inside it so downstream tasks (handle_user_speech, the
        debounce task) see the right insurance/call-id. No pump thread is held."""
        self._loop = loop
        self._ctx = contextvars.copy_context()


class AzureRealtimeSttManager:
    """Keeps track of all concurrent STT sessions."""
    def __init__(self):
        self.active_sessions: Dict[str, AzureRealtimeSttService] = {}

    def create_session(self, websocket_id: str) -> AzureRealtimeSttService:
        if websocket_id in self.active_sessions:
            self.remove_session(websocket_id)
        session = AzureRealtimeSttService(websocket_id)
        self.active_sessions[websocket_id] = session
        return session

    def get_session(self, websocket_id: str) -> Optional[AzureRealtimeSttService]:
        return self.active_sessions.get(websocket_id)

    def remove_session(self, websocket_id: str):
        svc = self.active_sessions.pop(websocket_id, None)
        if svc:
            svc.stop()

    def cleanup_all(self):
        for wid in list(self.active_sessions):
            self.remove_session(wid)


# Global manager instance to import in your main app
stt_manager = AzureRealtimeSttManager()
