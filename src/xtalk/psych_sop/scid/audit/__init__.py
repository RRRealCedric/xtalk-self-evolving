"""Read-only, privacy-preserving Gate 1 audit projections for SCID episodes."""

from .projector import (
    AuditValidationError,
    build_audit_projection,
    load_episode_events,
    render_audit_markdown,
    write_audit_report,
)

__all__ = [
    "AuditValidationError",
    "build_audit_projection",
    "load_episode_events",
    "render_audit_markdown",
    "write_audit_report",
]
