import os
import contextvars
import logging
from logging.config import dictConfig

LOGS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
os.makedirs(LOGS_DIR, exist_ok=True)

COLORS = {
    "DEBUG": "\033[37m",
    "INFO": "\033[32m",
    "WARNING": "\033[33m",
    "ERROR": "\033[31m",
    "CRITICAL": "\033[41m",
    "RESET": "\033[0m",
}


# ── Per-call context tag ────────────────────────────────────────────────────
# Set once at each call entry point (orchestrate, webhooks, stream, shutdown).
# asyncio.create_task inherits the current context, so background tasks —
# including the post-call upload — automatically carry the tag without
# any extra wiring.
#
# To filter logs for one call:   grep "DthoBVG0" app.log
# The short tag is the first 8 characters of the Telnyx call_control_id
# AFTER the "v3:" prefix, which is stable and unique per call.
# ────────────────────────────────────────────────────────────────────────────
_call_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("call_id", default="-")
# visit_id / customer_id are set at orchestrate/webhook/stream entry alongside
# set_call_id. Included on every log line so ops can grep a specific visit or
# customer's calls end-to-end.
_visit_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("visit_id", default="-")
_customer_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("customer_id", default="-")

SHORT_TAG_WIDTH = 8


def _shorten(call_control_id: str) -> str:
    """Turn a long call_control_id (e.g. 'v3:DthoBVG07lIX...') into a short
    tag for log lines (e.g. 'DthoBVG0'). The mapping is deterministic so
    you can always grep by the same short tag."""
    if not call_control_id:
        return "-"
    trimmed = call_control_id.split(":", 1)[-1]
    return trimmed[:SHORT_TAG_WIDTH] or "-"


def set_call_id(call_control_id: str) -> None:
    """Set the current call tag. Every subsequent log line on this task
    (and any asyncio task it spawns) will include this tag."""
    _call_id_var.set(_shorten(call_control_id))


def set_visit_context(visit_id, customer_id) -> None:
    """Attach visit_id and customer_id to the current task's log context.
    Grep in App Insights with 'v=<visit_id>' or 'cid=<customer_id>'."""
    _visit_id_var.set(str(visit_id) if visit_id is not None else "-")
    _customer_id_var.set(str(customer_id) if customer_id is not None else "-")


def get_call_id() -> str:
    return _call_id_var.get()


def shorten_call_id(call_control_id: str) -> str:
    """Public: turn a full call_control_id into the same short tag used in
    log lines. Use this when you have the id but not the ContextVar (e.g. in
    a detached background task) and need a value that matches the logs."""
    return _shorten(call_control_id)


class CallIdFilter(logging.Filter):
    """Injects the current call's short tag + visit/customer IDs onto every
    log record. All three are ContextVars → cost is a dict lookup per record,
    no I/O and no allocation of consequence."""
    def filter(self, record: logging.LogRecord) -> bool:
        record.call_id = _call_id_var.get()
        record.visit_id = _visit_id_var.get()
        record.customer_id = _customer_id_var.get()
        return True


class ColorFormatter(logging.Formatter):
    def format(self, record):
        log_color = COLORS.get(record.levelname, COLORS["RESET"])
        # Colour only the levelname field — leave call_id plain so it's grep-friendly.
        record.levelname = f"{log_color}{record.levelname}{COLORS['RESET']}"
        return super().format(record)


class BelowErrorFilter(logging.Filter):
    """Passes only records below ERROR. Used so INFO/WARNING go to stdout while
    ERROR/CRITICAL go to stderr — Azure App Service (and most container log
    collectors) classify *anything* on stderr as ERROR, so without this split a
    single stderr-defaulted StreamHandler made every INFO line surface as an error."""
    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno < logging.ERROR


LOG_FORMAT = "[%(asctime)s - %(levelname)s - c=%(call_id)-8s v=%(visit_id)s cid=%(customer_id)s - %(filename)s - %(funcName)s] %(message)s"

logging_config = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "call_id": {
            "()": CallIdFilter,
        },
        "below_error": {
            "()": BelowErrorFilter,
        },
    },
    "formatters": {
        "colored": {
            "()": ColorFormatter,
            "format": LOG_FORMAT,
            "datefmt": "%Y-%m-%d %H:%M:%S",
        },
        "file": {
            "format": LOG_FORMAT,
            "datefmt": "%Y-%m-%d %H:%M:%S",
        },
    },
    # Split output by level across two streams. A single StreamHandler defaults
    # to stderr, and Azure App Service classifies everything on stderr as ERROR —
    # which is why every INFO/WARNING line was showing up as an error. INFO and
    # WARNING now go to stdout; ERROR and CRITICAL go to stderr, so the platform
    # classifies each line correctly.
    "handlers": {
        "stdout": {
            "level": "INFO",
            "formatter": "colored",
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stdout",
            "filters": ["call_id", "below_error"],
        },
        "stderr": {
            "level": "ERROR",
            "formatter": "colored",
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stderr",
            "filters": ["call_id"],
        },
    },
    # Route uvicorn's own loggers through the same split so its INFO lines
    # (startup banner, "connection closed") aren't mislabeled as errors either.
    "loggers": {
        "uvicorn": {"level": "INFO", "handlers": ["stdout", "stderr"], "propagate": False},
        "uvicorn.error": {"level": "INFO", "handlers": ["stdout", "stderr"], "propagate": False},
        "uvicorn.access": {"level": "INFO", "handlers": ["stdout", "stderr"], "propagate": False},
    },
    "root": {
        "level": "INFO",
        "handlers": ["stdout", "stderr"],
    },
}


def setup_logging():
    """Initialize the global logging configuration (console output).

    Call this once at application startup (in main.py).
    Per-session file handlers are added later via get_session_logger().
    """
    dictConfig(logging_config)

    # Quiet noisy third-party loggers that print full URLs of every HTTP call
    # (would leak endpoint URLs — and sometimes path-embedded IDs — to logs).
    for noisy in ("httpx", "httpcore", "urllib3", "azure"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_session_logger(session_id: str) -> logging.Logger:
    """Return a logger dedicated to a specific WebSocket session."""
    logger_name = f"session.{session_id}"
    logger = logging.getLogger(logger_name)

    if not logger.handlers:
        logger.setLevel(logging.INFO)
        logger.propagate = True

        log_path = os.path.join(LOGS_DIR, f"{session_id}.log")
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt="%Y-%m-%d %H:%M:%S"))
        file_handler.addFilter(CallIdFilter())
        logger.addHandler(file_handler)

    return logger


def cleanup_session_logger(session_id: str):
    """Close and remove file handlers for a disconnected session."""
    logger_name = f"session.{session_id}"
    logger = logging.getLogger(logger_name)
    for handler in logger.handlers[:]:
        handler.close()
        logger.removeHandler(handler)
