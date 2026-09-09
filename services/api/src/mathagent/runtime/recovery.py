"""Failures eligible for an explicitly budgeted, single unknown-outcome retry."""

UNKNOWN_TRANSPORT_FAILURES = frozenset({
    "transport_read_error", "transport_write_error", "transport_connect_error",
    "transport_read_timeout", "transport_write_timeout", "transport_connect_timeout",
    "transport_pool_timeout", "transport_remote_protocol_error", "transport_proxy_error",
    "transport_outcome_unknown", "request_deadline_exceeded",
})
