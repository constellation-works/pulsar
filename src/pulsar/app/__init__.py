"""What every front end calls. ``App`` and ``Runtime`` are the interfaces a
front end and the verbs code against; ``LocalApp`` and ``LocalRuntime``
implement them over one home, and the entry point (``pulsar.main``) builds
the app and supplies it. Behind them: the operator verbs (``ops``), account
health (``health``), the write-log export and redacting logs (``writelog``)
and the import of historic posts (``importer``).

This is the front ends' one gateway: ``pulsar.surfaces`` imports nothing
below ``app`` directly, so what they need from the packages beneath (error
codes, plan, ledger states, media limits) is re-exported here.
"""

from __future__ import annotations

from pulsar.accounts import home_command
from pulsar.channels.x import MediaProcessingError, check_x_id, validate_text
from pulsar.errors import (
    API_ERROR,
    IDEMPOTENCY_CONFLICT,
    INTERNAL,
    INVALID_ARGUMENT,
    INVALID_CONFIG,
    INVALID_MEDIA,
    INVALID_PLAN,
    INVALID_TEXT,
    SECRET_DETECTED,
    UNSUPPORTED,
    OutcomeUnknown,
    PulsarError,
)
from pulsar.home import Prices, Settings, load_settings, resolve_home
from pulsar.jsonx import as_object
from pulsar.ledger import PUBLISHED, SKIPPED, PlanRecord, check_key, request_digest
from pulsar.plan import Plan
from pulsar.publishing import (
    MAX_IMAGE_BYTES,
    MAX_VIDEO_BYTES,
    STALE_SUBMITTING,
    Bound,
    Outcome,
    Prepared,
    load_media,
    open_beneath,
)

from .facade import LocalApp
from .health import attention, auth_report
from .importer import IMPORT_CALLER, import_posted
from .interfaces import App, Report, Runtime
from .ops import CLI_CALLER, HISTORY_LIMIT_DEFAULT, HISTORY_LIMIT_MAX, budget_report, check_limit
from .runtime import (
    CALLER_ENV,
    PLUGIN_STATE_ENV,
    LocalRuntime,
    RedactingFilter,
    configure_logging,
    default_paths,
)
from .writelog import WriteLog

__all__ = [
    # facade
    "LocalApp",
    # health
    "attention",
    "auth_report",
    # importer
    "IMPORT_CALLER",
    "import_posted",
    # interfaces
    "App",
    "Report",
    "Runtime",
    # ops
    "CLI_CALLER",
    "HISTORY_LIMIT_DEFAULT",
    "HISTORY_LIMIT_MAX",
    "budget_report",
    "check_limit",
    # runtime
    "CALLER_ENV",
    "PLUGIN_STATE_ENV",
    "configure_logging",
    "default_paths",
    "LocalRuntime",
    "RedactingFilter",
    # writelog
    "WriteLog",
    # re-exported from accounts for the front ends
    "home_command",
    # re-exported from channels.x for the front ends
    "check_x_id",
    "MediaProcessingError",
    "validate_text",
    # re-exported from errors for the front ends
    "API_ERROR",
    "IDEMPOTENCY_CONFLICT",
    "INTERNAL",
    "INVALID_ARGUMENT",
    "INVALID_CONFIG",
    "INVALID_MEDIA",
    "INVALID_PLAN",
    "INVALID_TEXT",
    "SECRET_DETECTED",
    "UNSUPPORTED",
    "OutcomeUnknown",
    "PulsarError",
    # re-exported from home for the front ends
    "load_settings",
    "Prices",
    "resolve_home",
    "Settings",
    # re-exported from jsonx for the front ends
    "as_object",
    # re-exported from ledger for the front ends
    "PUBLISHED",
    "SKIPPED",
    "check_key",
    "PlanRecord",
    "request_digest",
    # re-exported from plan for the front ends
    "Plan",
    # re-exported from publishing for the front ends
    "MAX_IMAGE_BYTES",
    "MAX_VIDEO_BYTES",
    "STALE_SUBMITTING",
    "Bound",
    "load_media",
    "open_beneath",
    "Outcome",
    "Prepared",
]
