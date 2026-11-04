"""Stable JSON and self-contained HTML serialization for Echo Report views."""

from __future__ import annotations

import base64
import json
from dataclasses import asdict
from pathlib import Path

from ..resources import resource_path
from ..media_content import MEDIA_FILE_NAME, publish_media
from ..unicode_emoji import iter_emoji_clusters
from .echo_report_template import (
    ECHO_REPORT_APP_JS,
    ECHO_REPORT_CSS,
    ECHO_REPORT_HTML_SKELETON,
)
from .echo_rhythm import ECHO_RHYTHM_APP_JS, ECHO_RHYTHM_CSS
from .echo_language import ECHO_LANGUAGE_APP_JS, ECHO_LANGUAGE_CSS
from .echo_member_controls import ECHO_MEMBER_CONTROLS_CSS, ECHO_MEMBER_CONTROLS_JS
from .models import (
    ChartPoint,
    EchoConversationSessions,
    EchoLanguageProfile,
    EchoMemberCard,
    EchoReportView,
    EchoStickers,
    EchoStickerItem,
)
from .expression_assets import (
    expression_asset_data_uri,
    resolve_expression_asset_key,
)


ECHO_REPORT_SCHEMA_VERSION = "echo-report.v0.19"


def echo_report_to_dict(view: EchoReportView) -> dict[str, object]:
    """Convert an Echo view to frontend-ready JSON-compatible primitives."""
    return {
        "schema_version": ECHO_REPORT_SCHEMA_VERSION,
        "title": view.title,
        "conversation": {
            "kind": view.conversation_kind,
            "name": view.conversation_name,
            "time_span": view.time_span,
        },
        "overview": {
            "has_data": view.has_data,
            "total_message_count": view.total_message_count,
            "participant_count": view.participant_count,
            "empty_description": view.empty_description,
            "active_days": view.active_days,
            "average_messages_per_active_day": (
                view.average_messages_per_active_day
            ),
        },
        "activity": {
            "hourly": _points_to_list(view.hourly_activity),
            "weekday": _points_to_list(view.weekday_activity),
            "weekday_daily_average": _points_to_list(view.weekday_average_activity),
            "calendar_day_count": view.calendar_day_count,
        },
        "conversation_sessions": _conversation_sessions_to_dict(
            view.conversation_sessions
        ),
        "private_nudges": (
            asdict(view.private_nudges)
            if view.conversation_kind == "private" and view.private_nudges is not None else None
        ),
        "language_profile": _language_profile_to_dict(
            view.language_profile,
            view.expression_source,
        ),
        "language_sources": asdict(view.language_sources) if view.language_sources is not None else None,
        "expression_culture": _expression_culture_to_dict(
            view.expression_culture
        ),
        "stickers": _stickers_to_dict(view.stickers),
        "sticker_memories": [{
            "compact": memory.compact,
            "findings": [{
                "kind": finding.kind, "title": finding.title, "period": finding.period,
                "note": finding.note, "owner_label": finding.owner_label,
                "materials": [{"asset_key": item.asset_key} for item in finding.materials],
                "points": [{
                    "label": point.label, "value": point.value, "total": point.total,
                    "date_count": point.date_count, "start_date": point.start_date,
                    "end_date": point.end_date,
                    "materials": [{"asset_key": item.asset_key} for item in point.materials],
                } for point in finding.points],
            } for finding in memory.findings],
            "materials": [{"asset_key": item.asset_key} for item in memory.materials],
        } for memory in view.stickers.memories] if view.stickers and view.conversation_kind in {"private", "group"} else [],
        "members": [
            _member_to_dict(member, view.expression_source)
            for member in view.members
        ],
    }


def _sticker_item_to_dict(item: EchoStickerItem) -> dict[str, object]:
    return {
        "label": item.label,
        "count": item.count,
        "asset_key": item.asset_key,
        "side_counts": list(item.side_counts),
        "reliable_member_count": item.reliable_member_count,
        "unknown_sender_count": item.unknown_sender_count,
        "signature": asdict(item.signature) if item.signature else None,
    }


def _stickers_to_dict(chapter: EchoStickers | None) -> dict[str, object] | None:
    if chapter is None:
        return None
    return {
        "available": chapter.available,
        "total_send_count": chapter.total_send_count,
        "unique_material_count": chapter.unique_material_count,
        "unknown_identity_count": chapter.unknown_identity_count,
        "unattributed_send_count": chapter.unattributed_send_count,
        "covered_message_count": chapter.covered_message_count,
        "members": [{
            "label": member.label, "send_count": member.send_count,
            "search_names": list(member.search_names),
            "message_count": member.message_count, "per_hundred_messages": member.per_hundred_messages,
            "unique_material_count": member.unique_material_count,
            "top_materials": [_sticker_item_to_dict(item) for item in member.top_materials],
            "exclusive_material_count": member.exclusive_material_count,
            "signature": _sticker_item_to_dict(member.signature) if member.signature else None,
        } for member in chapter.members],
        "shared_material_count": chapter.shared_material_count,
        "shared_materials": [_sticker_item_to_dict(item) for item in chapter.shared_materials],
        "assets": {asset.key: {"data_uri": asset.data_uri, "status": asset.status,
                                "reason": asset.reason} for asset in chapter.assets},
        "unavailable_reason": chapter.unavailable_reason,
        "conversation_kind": chapter.conversation_kind,
        "total_top_materials": [_sticker_item_to_dict(item) for item in chapter.total_top_materials],
        "exclusive_material_count": chapter.exclusive_material_count,
        "per_hundred_messages": chapter.per_hundred_messages,
        "shared_ranking_min_materials": chapter.shared_ranking_min_materials,
    }


def _expression_culture_to_dict(
    culture: object | None,
) -> dict[str, object] | None:
    if culture is None:
        return None
    return {
        "available": culture.available,
        "expression_message_count": culture.expression_message_count,
        "expression_only_message_count": (
            culture.expression_only_message_count
        ),
        "expression_only_rate": culture.expression_only_rate,
        "unique_expression_count": culture.unique_expression_count,
        "top_expressions": [
            _expression_item_to_dict(item)
            for item in culture.top_expressions
        ],
        "members": [
            {
                "speaker_key": member.speaker_key,
                "display_name": member.display_name,
                "expression_occurrence_count": (
                    member.expression_occurrence_count
                ),
                "expression_message_count": member.expression_message_count,
                "expression_share_percent": member.expression_share_percent,
                "expression_only_message_count": (
                    member.expression_only_message_count
                ),
                "top_expressions": [
                    _expression_item_to_dict(item)
                    for item in member.top_expressions
                ],
            }
            for member in culture.members
        ],
        "top_combinations": [
            _expression_combination_to_dict(combination)
            for combination in culture.top_combinations
        ],
        "unavailable_reason": culture.unavailable_reason,
    }


def _expression_item_to_dict(item: object) -> dict[str, object]:
    return {
        "display_text": item.display_text,
        "count": item.count,
        "kind": item.kind,
        "with_text_message_count": item.with_text_message_count,
        "text_only_message_count": item.text_only_message_count,
        "asset_key": item.asset_key,
        "nearby_words": list(getattr(item, "nearby_words", ())),
    }


def _expression_combination_to_dict(
    combination: object,
) -> dict[str, object]:
    return {
        "asset_keys": list(getattr(combination, "asset_keys", ())),
        "labels": list(getattr(combination, "labels", ())),
        "count": combination.count,
        "common_members": [
            {
                "display_name": member.display_name,
                "count": member.count,
                "share_percent": member.share_percent,
            }
            for member in getattr(combination, "common_members", ())
        ],
    }


def _language_profile_to_dict(
    profile: EchoLanguageProfile | None,
    expression_source: str | None = None,
) -> dict[str, object] | None:
    if profile is None:
        return None
    return {
        "mode": profile.mode,
        "available": profile.available,
        "unavailable_reason": profile.unavailable_reason,
        "shared_words": [
            _shared_word_to_dict(word, expression_source)
            for word in profile.shared_words
        ],
        "members": [
            {
                "speaker_key": member.speaker_key,
                "display_name": member.display_name,
                "heading": member.heading,
                "primary_words": [
                    _word_to_display(word, expression_source)
                    for word in member.primary_words
                ],
                "context_words": [
                    _word_to_display(word, expression_source)
                    for word in member.context_words
                ],
            }
            for member in profile.members
        ],
    }


def _shared_word_to_dict(
    word: object,
    expression_source: str | None = None,
) -> dict[str, object]:
    return {
        "word": _word_to_display(word.word, expression_source),
        "self_count": word.self_count,
        "peer_count": word.peer_count,
        "emphasis": word.emphasis,
    }


def _word_to_display(
    word: str,
    expression_source: str | None = None,
) -> str | dict[str, str]:
    """Turn an expression token into a display-safe word entry."""
    if isinstance(word, str) and word.startswith("expression:"):
        expression_key = word[len("expression:") :]
        asset_key = resolve_expression_asset_key(
            expression_key,
            expression_source,
        )
        if asset_key:
            return {"asset_key": asset_key}
        if _is_emoji_expression(expression_key):
            return {"text": expression_key}
        return {"text": "表情"}
    return word


def _is_emoji_expression(expression_key: str) -> bool:
    return tuple(iter_emoji_clusters(expression_key)) == (expression_key,)


def _conversation_sessions_to_dict(
    sessions: EchoConversationSessions | None,
) -> dict[str, object] | None:
    if sessions is None:
        return None
    private_initiators = None
    if sessions.private_self_count is not None:
        private_initiators = {
            "self_count": sessions.private_self_count,
            "peer_count": sessions.private_peer_count,
            "unknown_count": sessions.private_unknown_count,
            "self_to_peer_ratio": sessions.private_self_to_peer_ratio,
            "self_share": sessions.private_self_share,
            "peer_share": sessions.private_peer_share,
            "unknown_share": sessions.private_unknown_share,
        }
    group_initiators = None
    if (
        sessions.group_self_count is not None
        or sessions.group_top_initiator_name is not None
        or sessions.group_initiators
        or sessions.group_unknown_count
    ):
        top_member = None
        if sessions.group_top_initiator_name is not None:
            top_member = {
                "display_name": sessions.group_top_initiator_name,
                "count": sessions.group_top_initiator_count,
                "share": sessions.group_top_initiator_share,
            }
        group_initiators = {
            "self_count": sessions.group_self_count,
            "self_share": sessions.group_self_share,
            "top_member": top_member,
            "members": [asdict(member) for member in sessions.group_initiators],
            "unknown_count": sessions.group_unknown_count,
        }
    return {
        "threshold_seconds": sessions.threshold_seconds,
        "session_count": sessions.session_count,
        "average_duration_seconds": sessions.average_duration_seconds,
        "median_duration_seconds": sessions.median_duration_seconds,
        "longest_duration_seconds": sessions.longest_duration_seconds,
        "average_message_count": sessions.average_message_count,
        "private_initiators": private_initiators,
        "group_initiators": group_initiators,
        "private_self_peak_start_hour": sessions.private_self_peak_start_hour,
        "private_peer_peak_start_hour": sessions.private_peer_peak_start_hour,
        "private_reply_median_self_to_peer_seconds": (
            sessions.private_reply_median_self_to_peer_seconds
        ),
        "private_reply_median_peer_to_self_seconds": (
            sessions.private_reply_median_peer_to_self_seconds
        ),
        "items": [_session_to_dict(item) for item in sessions.items],
        "viewer_identity_reliable": sessions.viewer_identity_reliable,
        "start_hour_distribution": [
            {"label": p.label, "value": p.value}
            for p in sessions.start_hour_distribution
        ],
        "peak_start_hour": sessions.peak_start_hour,
        "session_character": sessions.session_character,
        "loudest_most_messages": _session_to_dict(sessions.loudest_most_messages),
        "loudest_longest_duration": _session_to_dict(sessions.loudest_longest_duration),
        "loudest_most_participants": _session_to_dict(sessions.loudest_most_participants),
        "loudest_densest": _session_to_dict(sessions.loudest_densest),
        "loudest_most_back_and_forth": _session_to_dict(
            sessions.loudest_most_back_and_forth
        ),
    }



def _session_to_dict(session: EchoConversationSession | None) -> dict[str, object] | None:
    """Convert an EchoConversationSession to a dict, or None."""
    if session is None:
        return None
    return {
        "start_timestamp": session.start_timestamp,
        "end_timestamp": session.end_timestamp,
        "duration_seconds": session.duration_seconds,
        "message_count": session.message_count,
        "participant_count": session.participant_count,
        "initiator": session.initiator,
        "initiator_sender_key": session.initiator_sender_key,
        "self_message_count": session.self_message_count,
        "peer_message_count": session.peer_message_count,
    }


def export_echo_report_json(
    view: EchoReportView,
    output_path: str | Path,
    *, cancelled=lambda: False,
) -> Path:
    """Write an Echo view as UTF-8 JSON and return the destination path."""
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    _export_sticker_files(view, destination.parent, cancelled=cancelled)
    with destination.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(
            echo_report_to_dict(view),
            handle,
            ensure_ascii=False,
            indent=2,
        )
        handle.write("\n")
    return destination


def _member_to_dict(
    member: EchoMemberCard,
    expression_source: str | None = None,
) -> dict[str, object]:
    return {
        "speaker_key": member.speaker_key,
        "display_name": member.display_name,
        "primary_name": member.primary_name,
        "secondary_name": member.secondary_name,
        "remark": member.remark,
        "contextual_name": member.contextual_name,
        "search_names": list(member.search_names),
        "is_viewer": member.is_viewer,
        "message_count": member.message_count,
        "first_seen_order": member.first_seen_order,
        "message_share_percent": member.message_share_percent,
        "average_length": member.average_length,
        "max_length": member.max_length,
        "active_period": member.active_period,
        "activity": {
            "hourly": _points_to_list(member.hourly_activity),
            "weekday": _points_to_list(member.weekday_activity),
            "weekday_daily_average": _points_to_list(member.weekday_average_activity),
        },
        "top_words": [
            _word_to_display(word, expression_source)
            for word in member.top_words
        ],
    }


def _points_to_list(points: tuple[ChartPoint, ...]) -> list[dict[str, object]]:
    return [
        {"label": point.label, "value": point.value}
        for point in points
    ]


_ECHO_WORDMARK_RELATIVE_PATH = "assets/branding/echo/echo_wordmark_report.webp"
_ECHO_FAVICON_RELATIVE_PATH = "assets/branding/echo/echo_icon_32.png"


def export_echo_report_html(
    view: EchoReportView,
    output_path: str | Path,
    *, cancelled=lambda: False,
) -> Path:
    """Write offline HTML and its shared, byte-identical sticker files."""
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    _export_sticker_files(view, destination.parent, cancelled=cancelled)
    html = _build_echo_report_html(view)
    with destination.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(html)
    return destination


def _export_sticker_files(view: EchoReportView, directory: Path, *, cancelled=lambda: False) -> None:
    if cancelled():
        raise InterruptedError("media_export_cancelled")
    for asset in view.stickers.assets if view.stickers is not None else ():
        if asset.content is None:
            continue
        if not asset.data_uri.startswith("media/") or not MEDIA_FILE_NAME.fullmatch(asset.data_uri[6:]):
            raise ValueError("invalid_media_export_reference")
        publish_media(asset.content.chunks(), directory / asset.data_uri, cancelled=cancelled)


def _build_echo_report_html(view: EchoReportView) -> str:
    favicon_data_uri = _brand_data_uri(_ECHO_FAVICON_RELATIVE_PATH)
    wordmark_data_uri = _brand_data_uri(_ECHO_WORDMARK_RELATIVE_PATH)
    favicon_tag = (
        f'<link rel="icon" type="image/png" href="{favicon_data_uri}">'
        if favicon_data_uri
        else ""
    )
    logo_tag = (
        f'<img class="brand-logo" src="{wordmark_data_uri}" alt="余音 Echo">'
        if wordmark_data_uri
        else '<span class="brand-name">余音 Echo</span>'
    )
    return (
        ECHO_REPORT_HTML_SKELETON.replace("__ECHO_FAVICON_TAG__", favicon_tag)
        .replace("__ECHO_LOGO_TAG__", logo_tag)
        .replace("__ECHO_CSS__", ECHO_REPORT_CSS + ECHO_RHYTHM_CSS + ECHO_LANGUAGE_CSS + ECHO_MEMBER_CONTROLS_CSS)
        .replace("__ECHO_ASSETS__", _encode_echo_assets(view))
        .replace("__ECHO_APP_JS__", ECHO_REPORT_APP_JS + ECHO_RHYTHM_APP_JS + ECHO_LANGUAGE_APP_JS + ECHO_MEMBER_CONTROLS_JS)
        # Authored text may contain any template marker. Insert it last so it
        # can never be interpreted as another template substitution.
        .replace("__ECHO_DATA__", _encode_echo_data(view))
    )


def _encode_echo_data(view: EchoReportView) -> str:
    """Serialize the view as a JS-safe JSON object literal."""
    payload = json.dumps(
        echo_report_to_dict(view),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return (
        payload.replace("</", "<\\/")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _encode_echo_assets(view: EchoReportView) -> str:
    """Inline only the expression images needed by this report."""
    assets: dict[str, str] = {}
    for item in _iter_expression_items(view):
        asset_key = getattr(item, "asset_key", None)
        if not isinstance(asset_key, str) or not asset_key:
            continue
        data_uri = expression_asset_data_uri(asset_key)
        if data_uri:
            assets[asset_key] = data_uri
    culture = getattr(view, "expression_culture", None)
    if culture is not None:
        for combination in getattr(culture, "top_combinations", ()):
            for asset_key in getattr(combination, "asset_keys", ()):
                if not isinstance(asset_key, str) or not asset_key:
                    continue
                data_uri = expression_asset_data_uri(asset_key)
                if data_uri:
                    assets[asset_key] = data_uri
    return json.dumps(
        assets,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _iter_expression_items(view: EchoReportView):
    culture = view.expression_culture
    if culture is None:
        return
    yield from getattr(culture, "top_expressions", ())
    for member in getattr(culture, "members", ()):
        yield from getattr(member, "top_expressions", ())


def _brand_data_uri(relative_path: str) -> str:
    """Inline a bundled brand image with its actual encoding."""
    source = resource_path(relative_path)
    if not source.is_file():
        return ""
    encoded = base64.b64encode(source.read_bytes()).decode("ascii")
    mime = {".png": "image/png", ".webp": "image/webp"}[source.suffix.lower()]
    return f"data:{mime};base64,{encoded}"
