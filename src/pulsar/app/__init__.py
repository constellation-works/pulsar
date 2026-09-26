"""What every front end calls: the runtime that joins providers to the core,
the operator verbs (``ops``) and account health (``health``).

This module is app's public API and the front ends' only way down: ``cli``,
``mcp`` and ``orbit_tool`` import from ``pulsar.app`` only, and only what
``__all__`` lists; they never import ``core`` or ``providers``. What they need
from below is re-exported here. ``tests/test_layering.py`` enforces it.
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
    AccountRegistry,
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
    load_client_id,
    login,
    validate_text,
)

from .health import attention, auth_report
from .ops import (
    CLI_CALLER,
    HISTORY_LIMIT_DEFAULT,
    HISTORY_LIMIT_MAX,
    budget_report,
    check_limit,
    history_report,
    import_report,
    migrate_report,
    publish_report,
    reconcile_report,
    validate_report,
)
from .runtime import Runtime, configure_logging, default_paths

__all__ = [
    # runtime
    "configure_logging",
    "default_paths",
    "Runtime",
    # ops: the operator verbs
    "CLI_CALLER",
    "HISTORY_LIMIT_DEFAULT",
    "HISTORY_LIMIT_MAX",
    "budget_report",
    "check_limit",
    "history_report",
    "import_report",
    "migrate_report",
    "publish_report",
    "reconcile_report",
    "validate_report",
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
    "AccountRegistry",
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
    "load_client_id",
    "login",
    "MediaProcessingError",
    "validate_text",
]
