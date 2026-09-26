"""What every front end calls. ``App`` and ``Runtime`` are the interfaces a
front end and the verbs code against; ``LocalApp`` and ``LocalRuntime``
implement them over one home, and the entry point (``pulsar.main``) builds
the app and supplies it. Behind them: the operator verbs (``ops``) and
account health (``health``).
What the front ends need from ``core`` and ``providers`` is re-exported here.
"""

from __future__ import annotations

from pulsar.core import (
    API_ERROR,
    IDEMPOTENCY_CONFLICT,
    INTERNAL,
    INVALID_ARGUMENT,
    INVALID_CONFIG,
    INVALID_MEDIA,
    INVALID_PLAN,
    INVALID_TEXT,
    MAX_IMAGE_BYTES,
    MAX_VIDEO_BYTES,
    PUBLISHED,
    SECRET_DETECTED,
    SKIPPED,
    STALE_SUBMITTING,
    UNSUPPORTED,
    Bound,
    Outcome,
    OutcomeUnknown,
    Paths,
    Plan,
    PlanRecord,
    Prepared,
    Prices,
    PulsarError,
    Settings,
    as_object,
    check_key,
    home_command,
    load_media,
    load_settings,
    open_beneath,
    request_digest,
    resolve_home,
)
from pulsar.providers.x import (
    MediaProcessingError,
    check_x_id,
    validate_text,
)

from .facade import LocalApp
from .health import attention, auth_report
from .interfaces import App, Report, Runtime
from .ops import (
    CLI_CALLER,
    HISTORY_LIMIT_DEFAULT,
    HISTORY_LIMIT_MAX,
    budget_report,
    check_limit,
)
from .runtime import PLUGIN_STATE_ENV, LocalRuntime, configure_logging, default_paths

__all__ = [
    # the interfaces the front ends and verbs code against
    "App",
    "Report",
    "Runtime",
    # their implementations over one home: what the entry point builds
    "LocalApp",
    "LocalRuntime",
    # runtime
    "configure_logging",
    "PLUGIN_STATE_ENV",
    "default_paths",
    # ops: the operator verbs
    "CLI_CALLER",
    "HISTORY_LIMIT_DEFAULT",
    "HISTORY_LIMIT_MAX",
    "budget_report",
    "check_limit",
    # health
    "attention",
    "auth_report",
    # re-exported from core
    "API_ERROR",
    "IDEMPOTENCY_CONFLICT",
    "INTERNAL",
    "INVALID_ARGUMENT",
    "INVALID_CONFIG",
    "INVALID_MEDIA",
    "INVALID_PLAN",
    "INVALID_TEXT",
    "MAX_IMAGE_BYTES",
    "MAX_VIDEO_BYTES",
    "PUBLISHED",
    "SECRET_DETECTED",
    "SKIPPED",
    "STALE_SUBMITTING",
    "UNSUPPORTED",
    "as_object",
    "Bound",
    "check_key",
    "home_command",
    "load_media",
    "load_settings",
    "open_beneath",
    "Outcome",
    "OutcomeUnknown",
    "Paths",
    "Plan",
    "PlanRecord",
    "Prepared",
    "Prices",
    "PulsarError",
    "request_digest",
    "resolve_home",
    "Settings",
    # re-exported from the X provider
    "check_x_id",
    "MediaProcessingError",
    "validate_text",
]
