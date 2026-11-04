"""Small package-owned summary projected from one completed analysis."""

from __future__ import annotations

from datetime import datetime

from ..analysis.revision import ANALYSIS_REVISION
from ..presentation.echo_serializer import ECHO_REPORT_SCHEMA_VERSION
from ..version import APP_VERSION
from ..identity_names import identity_search_names
from .dto import AnalysisResultDTO
from .scope_filter import AnalysisScope, AnalysisScopeMode


def build_report_metadata(
    *,
    result: AnalysisResultDTO,
    source: str,
    scope: AnalysisScope,
    generated_at: datetime,
) -> dict[str, object]:
    """Use existing report facts and resolved context without reading input."""
    view = result.echo_report_view
    if view is None:
        raise ValueError("Echo report view is required for package metadata.")
    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        raise ValueError("Report generation time must include a timezone.")
    if source not in {"qq", "wechat"}:
        raise ValueError("Unsupported report source.")

    conversations = result.reports.conversations
    summaries = conversations.conversations if conversations is not None else ()
    conversation = summaries[0] if len(summaries) == 1 else None
    bounded = scope.mode is not AnalysisScopeMode.ALL
    return {
        "schema_version": "echo-report-meta.v1",
        "app_version": APP_VERSION,
        "report_schema_version": ECHO_REPORT_SCHEMA_VERSION,
        "analysis_revision": ANALYSIS_REVISION,
        "generated_at": generated_at.isoformat(),
        "source": source,
        "conversation_name": view.conversation_name,
        "search_names": list(identity_search_names(
            view.conversation_name, *(name for member in view.members for name in member.search_names))),
        "conversation_kind": view.conversation_kind,
        "message_count": view.total_message_count,
        "active_days": view.active_days,
        "participant_count": view.participant_count,
        "message_start_timestamp": (
            conversation.start_timestamp if conversation is not None else None
        ),
        "message_end_timestamp": (
            conversation.end_timestamp if conversation is not None else None
        ),
        "analysis_scope": {
            "mode": scope.mode.value,
            "start_date": (
                scope.start_date.isoformat() if bounded and scope.start_date else None
            ),
            "end_date": (
                scope.end_date.isoformat() if bounded and scope.end_date else None
            ),
        },
    }
