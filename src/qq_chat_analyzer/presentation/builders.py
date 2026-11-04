"""Builders that turn analysis reports into display-ready view models.

The builder only reshapes and labels values that the analysis layer already
computed. It performs no counting, averaging, or sorting of raw messages.
"""

from __future__ import annotations

from dataclasses import replace
from collections import Counter
from itertools import islice
from ..identity_names import resolve_member_names
from ..analysis.stickers import StickerMedia
from .stickers import build_echo_stickers

from ..analysis.models import (
    AnalysisReports,
                          DistinctiveWordAvailability,
                          ExpressionCombinationUsage,
                          ExpressionReport,
                          ExpressionUsage,
    UserProfile,
)
from ..analysis.conversation_sessions import ConversationSessionReport
from .formatters import (
    format_active_period,
    format_duration,
    format_hour,
    format_weekday,
)
from .expression_assets import (
    WECHAT_ASSET_PREFIX,
    resolve_expression_asset_key,
)
from ..analysis.conversation_sessions import (
    ConversationSession,
)
from .models import (
    ChartPoint,
                          EchoMemberCard,
                          EchoExpressionCombination,
                          EchoExpressionCombinationMember,
                          EchoExpressionCulture,
    EchoExpressionItem,
    EchoMemberExpression,
    EchoLanguageMember,
    EchoLanguageProfile,
    EchoConversationSession,
    EchoConversationSessions,
    EchoSessionInitiator,
    EchoReportView,
    EchoNudgeMoment,
    EchoPrivateNudges,
    EchoSharedWord,
)


EMPTY_DESCRIPTION = "\u6ca1\u6709\u53ef\u5c55\u793a\u7684\u5206\u6790\u7ed3\u679c\u3002"

ECHO_REPORT_TITLE = "Echo Report"
ECHO_LANGUAGE_PRIMARY_WORD_LIMIT = 5
ECHO_LANGUAGE_CONTEXT_WORD_LIMIT = 3
ECHO_MEMBER_WORD_MIN_COUNT = 2
ECHO_GROUP_INSUFFICIENT_REASON = "样本不足，暂时无法比较成员特色词。"
ECHO_PRIVATE_INSUFFICIENT_REASON = "需要双方都有可用资料，才能展示两种声音。"
ECHO_UNKNOWN_CONVERSATION_REASON = "当前会话类型无法确定，暂不展示语言画像。"
ECHO_PRIVATE_SHARED_TOP_LIMIT = 8
ECHO_PRIVATE_SHARED_MIN_OCCURRENCE = 2
ECHO_EXPRESSION_TOP_LIMIT = 5
ECHO_EXPRESSION_MEMBER_TOP_LIMIT = 3
ECHO_EXPRESSION_MIN_COUNT = 2
ECHO_EXPRESSION_COMBINATION_TOP_LIMIT = 3
ECHO_EXPRESSION_COMBINATION_MIN_COUNT = 2
ECHO_EXPRESSION_COMBINATION_MEMBER_LIMIT = 4
ECHO_EXPRESSION_COMBINATION_MEMBER_MIN_COUNT = 2
ECHO_EXPRESSION_COMBINATION_MEMBER_SHARE_MIN = 20.0
# Keep the original raw ranking prefixes for report consumers, in addition
# to any lower-ranked candidates needed by the visible Echo leaderboards.
_EXPRESSION_REPORT_GLOBAL_PREFIX_LIMIT = 10
_EXPRESSION_REPORT_MEMBER_PREFIX_LIMIT = 3
_EXPRESSION_REPORT_COMBINATION_PREFIX_LIMIT = 3


def _build_echo_language_profile(
    reports: AnalysisReports,
    *,
    members: tuple[EchoMemberCard, ...],
    conversation_kind: str,
    viewer_speaker_key: str | None,
) -> EchoLanguageProfile:
    member_by_key = {member.speaker_key: member for member in members}

    if conversation_kind == "group":
        report = reports.distinctive_words
        if report is None or not report.available:
            reason = (
                ECHO_GROUP_INSUFFICIENT_REASON
                if report is not None
                and report.availability
                is DistinctiveWordAvailability.INSUFFICIENT_MEMBERS
                else "暂无可用的群聊特色词分析。"
            )
            return EchoLanguageProfile(
                mode="group_distinctive",
                available=False,
                unavailable_reason=reason,
            )
        language_members = []
        for distinctive_member in report.members:
            member = member_by_key.get(distinctive_member.speaker_key)
            if member is None:
                continue
            language_members.append(
                EchoLanguageMember(
                    speaker_key=member.speaker_key,
                    display_name=member.display_name,
                    heading=member.display_name,
                    primary_words=tuple(
                        word.word
                        for word in distinctive_member.words[
                            :ECHO_LANGUAGE_PRIMARY_WORD_LIMIT
                        ]
                    ),
                    context_words=tuple(
                        word for word in member.top_words
                        if not word.startswith("expression:")
                    )[:ECHO_LANGUAGE_CONTEXT_WORD_LIMIT],
                )
            )
        return EchoLanguageProfile(
            mode="group_distinctive",
            available=bool(language_members),
            members=tuple(language_members),
            unavailable_reason=(
                "" if language_members else "暂无可展示的成员特色词。"
            ),
        )

    if conversation_kind == "private":
        known_viewer_key = (
            viewer_speaker_key
            if any(member.is_viewer for member in members)
            else None
        )
        language_members = tuple(
            EchoLanguageMember(
                speaker_key=member.speaker_key,
                display_name=member.display_name,
                heading=_private_voice_heading(
                    member,
                    viewer_speaker_key=known_viewer_key,
                ),
                primary_words=tuple(
                    word for word in member.top_words
                    if not word.startswith("expression:")
                )[:ECHO_LANGUAGE_PRIMARY_WORD_LIMIT],
            )
            for member in members
        )
        available = len(language_members) == 2
        shared_words = _private_shared_words(
            reports.private_language,
            viewer_key=known_viewer_key,
            member_keys={
                member.speaker_key for member in language_members
            },
        )
        return EchoLanguageProfile(
            mode="private_common",
            available=available,
            members=language_members if available else (),
            unavailable_reason=(
                "" if available else ECHO_PRIVATE_INSUFFICIENT_REASON
            ),
            shared_words=shared_words if available else (),
        )

    return EchoLanguageProfile(
        mode="unavailable",
        available=False,
        unavailable_reason=ECHO_UNKNOWN_CONVERSATION_REASON,
    )


def _private_voice_heading(
    member: EchoMemberCard,
    *,
    viewer_speaker_key: str | None,
) -> str:
    if viewer_speaker_key is None:
        return f"{member.display_name} 常说"
    if member.is_viewer:
        return "你常说"
    return "TA 常说"


def _private_shared_words(
    report: object | None,
    *,
    viewer_key: str | None,
    member_keys: set[str],
) -> tuple[EchoSharedWord, ...]:
    """Select the common-word list from the existing private analysis."""
    if report is None or viewer_key is None:
        return ()

    shared_items = [
        item
        for item in report.shared_words
        if item.speaker_a in member_keys and item.speaker_b in member_keys
        and not item.word.startswith("expression:")
    ]
    return tuple(
        _to_echo_shared_word(item, viewer_key)
        for item in shared_items
        if item.occurrence_support >= ECHO_PRIVATE_SHARED_MIN_OCCURRENCE
    )[:ECHO_PRIVATE_SHARED_TOP_LIMIT]


def _to_echo_shared_word(
    item: object,
    viewer_key: str,
) -> EchoSharedWord:
    if viewer_key == item.speaker_a:
        self_count, peer_count = item.count_a, item.count_b
    else:
        self_count, peer_count = item.count_b, item.count_a
    return EchoSharedWord(
        word=item.word,
        self_count=self_count,
        peer_count=peer_count,
        emphasis="shared",
    )


def _build_echo_sessions(
    report: ConversationSessionReport | None,
    *,
    members: tuple[EchoMemberCard, ...] = (),
) -> EchoConversationSessions | None:
    if report is None:
        return None
    private = report.private_stats
    group = report.group_stats
    member_by_key = {member.speaker_key: member for member in members}
    openers = []
    if group is not None:
        names = []
        for index, (key, _) in enumerate(group.initiator_counts):
            member = member_by_key.get(key)
            name = member.display_name.strip() if member else ""
            names.append(name if name and name != key else f"未命名成员（{index + 1}）")
        name_counts, ordinals, reserved = Counter(names), Counter(), set(names)
        for (key, count), name in zip(group.initiator_counts, names):
            label = name
            if name_counts[name] > 1:
                ordinals[name] += 1
                label = f"{name}（{ordinals[name]}）"
                while label in reserved:
                    ordinals[name] += 1
                    label = f"{name}（{ordinals[name]}）"
            reserved.add(label)
            member = member_by_key.get(key)
            openers.append(EchoSessionInitiator(label, count, bool(member and member.is_viewer),
                                              member.search_names if member else ()))
    top_member = (
        next(
            (
                member
                for member in members
                if group is not None
                and member.speaker_key == group.top_initiator_sender_key
            ),
            None,
        )
        if group is not None
        else None
    )
    return EchoConversationSessions(
        threshold_seconds=report.threshold_seconds,
        session_count=report.session_count,
        average_duration_seconds=report.average_duration_seconds,
        median_duration_seconds=report.median_duration_seconds,
        longest_duration_seconds=report.longest_duration_seconds,
        average_message_count=report.average_message_count,
        items=tuple(_session_to_echo(session) for session in report.sessions),
        private_self_count=(private.self_initiated_count if private else None),
        private_peer_count=(private.peer_initiated_count if private else None),
        private_unknown_count=(
            private.unknown_initiated_count if private else None
        ),
        private_self_to_peer_ratio=(
            private.self_to_peer_ratio if private else None
        ),
        private_self_share=(private.self_initiated_share if private else None),
        private_peer_share=(private.peer_initiated_share if private else None),
        private_unknown_share=(
            private.unknown_initiated_share if private else None
        ),
        private_self_peak_start_hour=report.private_self_peak_start_hour,
        private_peer_peak_start_hour=report.private_peer_peak_start_hour,
        private_reply_median_self_to_peer_seconds=(
            report.private_reply_timing.self_to_peer_median_seconds
            if report.private_reply_timing is not None
            else None
        ),
        private_reply_median_peer_to_self_seconds=(
            report.private_reply_timing.peer_to_self_median_seconds
            if report.private_reply_timing is not None
            else None
        ),
        group_self_count=(group.self_initiated_count if group else None),
        group_self_share=(group.self_initiated_share if group else None),
        group_top_initiator_name=(
            top_member.display_name if top_member is not None else None
        ),
        group_top_initiator_count=(
            group.top_initiated_count
            if group is not None and top_member is not None
            else None
        ),
        group_top_initiator_share=(
            group.top_initiated_share
            if group is not None and top_member is not None
            else None
        ),
        group_initiators=tuple(openers),
        group_unknown_count=group.unknown_initiated_count if group else 0,
        viewer_identity_reliable=(
            group is not None and group.self_initiated_count is not None
        ),
        start_hour_distribution=tuple(
            ChartPoint(
                label=f"{h.hour:02d}:00-{h.hour:02d}:59",
                value=float(h.count),
            )
            for h in report.start_hour_counts
        ),
        peak_start_hour=report.peak_start_hour,
        session_character=_session_character_label(report.session_character),
        loudest_most_messages=(
            _session_to_echo(report.sessions[report.loudest_most_messages])
            if report.loudest_most_messages is not None
            else None
        ),
        loudest_longest_duration=(
            _session_to_echo(report.sessions[report.loudest_longest_duration])
            if report.loudest_longest_duration is not None
            else None
        ),
        loudest_most_participants=(
            _session_to_echo(report.sessions[report.loudest_most_participants])
            if report.loudest_most_participants is not None
            else None
        ),
        loudest_densest=(
            _session_to_echo(report.sessions[report.loudest_densest])
            if report.loudest_densest is not None
            else None
        ),
        loudest_most_back_and_forth=(
            _session_to_echo(report.sessions[report.loudest_most_back_and_forth])
            if report.loudest_most_back_and_forth is not None
            else None
        ),
    )



def prepare_expression_report(
    report: ExpressionReport,
    *,
    expression_source: str | None,
) -> ExpressionReport:
    """Bound retained candidates before the application builds its report DTO.

    Keep raw ranking prefixes and selected display candidates in their original
    order. All aggregate statistics and the incoming immutable report stay intact.
    """
    top_expressions = _retain_ranked_items(
        report.top_expressions,
        _EXPRESSION_REPORT_GLOBAL_PREFIX_LIMIT,
        _display_expression_items(report.top_expressions, ECHO_EXPRESSION_TOP_LIMIT),
    )
    members = []
    for member in report.members:
        items = _retain_ranked_items(
            member.top_expressions,
            _EXPRESSION_REPORT_MEMBER_PREFIX_LIMIT,
            _display_expression_items(member.top_expressions, ECHO_EXPRESSION_MEMBER_TOP_LIMIT),
        )
        members.append(
            member if items is member.top_expressions else replace(member, top_expressions=items)
        )
    top_combinations = _retain_ranked_items(
        report.top_combinations,
        _EXPRESSION_REPORT_COMBINATION_PREFIX_LIMIT,
        _display_expression_combinations(report.top_combinations, expression_source),
    )
    if (
        top_expressions is report.top_expressions
        and top_combinations is report.top_combinations
        and all(current is original for current, original in zip(members, report.members))
    ):
        return report
    return replace(
        report, top_expressions=top_expressions,
        members=tuple(members), top_combinations=top_combinations,
    )


def _retain_ranked_items(items, prefix_limit: int, selected):
    keep_ids = {id(item) for item in (*items[:prefix_limit], *selected)}
    retained = tuple(item for item in items if id(item) in keep_ids)
    return items if len(retained) == len(items) else retained


def _build_echo_expression_culture(
    report: ExpressionReport | None,
    *,
    members: tuple[EchoMemberCard, ...] = (),
    expression_source: str | None = None,
) -> EchoExpressionCulture | None:
    """Map the core expression report into the Echo expression chapter."""
    if report is None or report.expression_message_count <= 0:
        return None
    member_by_key = {member.speaker_key: member for member in members}
    culture_members = tuple(
        EchoMemberExpression(
            speaker_key=member.speaker_key,
            display_name=(
                member_by_key[member.speaker_key].display_name
                if member.speaker_key in member_by_key
                else member.speaker_key
            ),
            expression_occurrence_count=member.expression_occurrence_count,
            expression_message_count=member.expression_message_count,
            expression_share_percent=member.expression_share_percent,
            expression_only_message_count=member.expression_only_message_count,
            top_expressions=tuple(
                _to_echo_expression_item(item, expression_source)
                for item in _display_expression_items(
                    member.top_expressions,
                    ECHO_EXPRESSION_MEMBER_TOP_LIMIT,
                )
            ),
        )
        for member in report.members
    )
    # Sticker-only and nonrecurring expression senders can have nonzero
    # aggregates while the display selection contains nothing to show.
    culture_members = tuple(member for member in culture_members if member.top_expressions)
    return EchoExpressionCulture(
        available=bool(culture_members),
        expression_message_count=report.expression_message_count,
        expression_only_message_count=report.expression_only_message_count,
        expression_only_rate=report.expression_only_rate,
        unique_expression_count=report.unique_expression_count,
        top_expressions=tuple(
            _to_echo_expression_item(item, expression_source)
            for item in _display_expression_items(
                report.top_expressions,
                ECHO_EXPRESSION_TOP_LIMIT,
            )
        ),
        top_combinations=tuple(
            _to_echo_expression_combination(
                item,
                member_by_key,
                expression_source,
            )
            for item in _display_expression_combinations(
                report.top_combinations, expression_source,
            )
        ),
        members=culture_members,
        unavailable_reason=(
            "" if culture_members else "暂无可展示的表达文化。"
        ),
    )


def _display_expression_items(
    items,
    limit: int,
):
    """Keep presentation limited to recurring non-sticker expressions."""
    return tuple(islice((
        item
        for item in items
        if item.count >= ECHO_EXPRESSION_MIN_COUNT
        and item.kind != "sticker"
    ), limit))


def _display_expression_combinations(items, _expression_source: str | None):
    """Keep recurring combinations even when a member lacks a bundled image.

    The frontend falls back to text for any member whose asset is missing, so
    one unbundled expression must not hide the whole combination.
    """
    return tuple(islice((
        item for item in items
        if item.count >= ECHO_EXPRESSION_COMBINATION_MIN_COUNT
    ), ECHO_EXPRESSION_COMBINATION_TOP_LIMIT))


def _to_echo_expression_item(
    item: ExpressionUsage,
    expression_source: str | None,
) -> EchoExpressionItem:
    asset_key = resolve_expression_asset_key(
        item.expression_key,
        expression_source,
    )
    display_text = item.display_text or ""
    if asset_key and asset_key.startswith(WECHAT_ASSET_PREFIX):
        display_text = item.expression_key
    if (
        item.kind == "sticker"
        and not asset_key
        and (display_text in {"[贴图]", "[QQ贴图]"} or not display_text)
    ):
        display_text = "微信自定义表情"
    if not asset_key and display_text.startswith("["):
        display_text = "表情"
    return EchoExpressionItem(
        expression_key=item.expression_key,
        display_text=display_text,
        count=item.count,
        kind=item.kind,
        with_text_message_count=item.with_text_message_count,
        text_only_message_count=item.text_only_message_count,
        asset_key=asset_key,
        nearby_words=tuple(word.word for word in item.nearby_words[:5]),
    )


def _combination_fallback_label(display_text: str) -> str:
    """Text shown for a combination member whose asset is unavailable."""
    text = display_text or ""
    if text.startswith("["):
        return "表情"
    return text or "表情"


def _to_echo_expression_combination(
    item: ExpressionCombinationUsage,
    member_by_key: dict[str, EchoMemberCard],
    expression_source: str | None,
) -> EchoExpressionCombination:
    total = item.count
    common_members = []
    for member_count in item.member_counts:
        member = member_by_key.get(member_count.speaker_key)
        if (
            member is None
            or member_count.count < ECHO_EXPRESSION_COMBINATION_MEMBER_MIN_COUNT
        ):
            continue
        share_percent = (
            round(member_count.count * 100.0 / total, 1)
            if total > 0
            else 0.0
        )
        if share_percent < ECHO_EXPRESSION_COMBINATION_MEMBER_SHARE_MIN:
            continue
        common_members.append(
            EchoExpressionCombinationMember(
                display_name=member.display_name,
                count=member_count.count,
                share_percent=share_percent,
            )
        )
        if len(common_members) >= ECHO_EXPRESSION_COMBINATION_MEMBER_LIMIT:
            break
    asset_keys = []
    labels = []
    for member in item.expressions:
        asset_keys.append(
            resolve_expression_asset_key(
                member.expression_key,
                expression_source,
            )
        )
        labels.append(_combination_fallback_label(member.display_text))
    return EchoExpressionCombination(
        asset_keys=tuple(asset_keys),
        count=item.count,
        common_members=tuple(common_members),
        labels=tuple(labels),
    )


def _session_to_echo(session: ConversationSession) -> EchoConversationSession:
    """Convert a core ConversationSession to an EchoConversationSession."""
    return EchoConversationSession(
        start_timestamp=session.start_timestamp,
        end_timestamp=session.end_timestamp,
        duration_seconds=session.duration_seconds,
        message_count=session.message_count,
        participant_count=session.participant_count,
        initiator=session.initiator,
        initiator_sender_key=session.initiator_sender_key,
        self_message_count=session.self_message_count,
        peer_message_count=session.peer_message_count,
    )


_SESSION_CHARACTER_LABELS: dict[str, str] = {
    "quick_and_brief": "来得快，也散得快",
    "long_running": "一旦聊开，就会聊很久",
    "mixed": "有长有短，节奏不一",
}


def _session_character_label(key: str | None) -> str | None:
    """Map a core session character key to its Chinese label."""
    if key is None:
        return None
    return _SESSION_CHARACTER_LABELS.get(key, key)


def build_echo_report_view(
    reports: AnalysisReports | None,
    *,
    viewer_speaker_key: str | None = None,
    conversation_kind: str = "unknown",
    title: str = ECHO_REPORT_TITLE,
    expression_source: str | None = None,
    sticker_media: tuple[StickerMedia, ...] = (),
) -> EchoReportView:
    """Build an Echo report, highlighting only an explicitly supplied key."""
    reports = reports or AnalysisReports()
    activity = reports.activity
    profiles = reports.user_profiles
    conversation = (
        reports.conversations.conversations[0]
        if reports.conversations
        and reports.conversations.conversations
        else None
    )

    hourly_activity = tuple(
        ChartPoint(label=format_hour(entry.hour), value=float(entry.count))
        for entry in (activity.hourly_counts if activity else ())
    )
    weekday_activity = tuple(
        ChartPoint(
            label=format_weekday(entry.weekday),
            value=float(entry.count),
        )
        for entry in (activity.weekday_counts if activity else ())
    )
    members = tuple(
        _build_echo_member(profile, viewer_speaker_key, conversation_kind)
        for profile in (profiles.profiles if profiles else ())
    )
    total_message_count = activity.total_message_count if activity else 0
    participant_count = profiles.speaker_count if profiles else 0
    has_data = (
        activity is not None
        or profiles is not None
        or conversation is not None
    )

    return EchoReportView(
        title=title,
        has_data=has_data,
        conversation_kind=conversation_kind,
        conversation_name=(
            conversation.resolved_display_name if conversation else ""
        ),
        time_span=(
            format_duration(conversation.duration_seconds)
            if conversation
            else ""
        ),
        total_message_count=total_message_count,
        participant_count=participant_count,
        hourly_activity=hourly_activity,
        weekday_activity=weekday_activity,
        weekday_average_activity=tuple(
            ChartPoint(label=format_weekday(entry.weekday), value=entry.average_per_day)
            for entry in (activity.weekday_counts if activity else ())
            if entry.average_per_day is not None
        ),
        calendar_day_count=activity.calendar_day_count if activity else 0,
        members=members,
        conversation_sessions=_build_echo_sessions(
            reports.conversation_sessions,
            members=members,
        ),
        private_nudges=(
            EchoPrivateNudges(
                self_count=reports.private_nudges.self_count,
                peer_count=reports.private_nudges.peer_count,
                source=reports.private_nudges.source,
                self_suffix=reports.private_nudges.self_suffix,
                peer_suffix=reports.private_nudges.peer_suffix,
                self_action=reports.private_nudges.self_action,
                peer_action=reports.private_nudges.peer_action,
                recent=tuple(EchoNudgeMoment(item.timestamp, item.is_self, item.suffix)
                             for item in reports.private_nudges.recent),
            ) if conversation_kind == "private" and reports.private_nudges is not None else None
        ),
        language_profile=_build_echo_language_profile(
            reports,
            members=members,
            conversation_kind=conversation_kind,
            viewer_speaker_key=viewer_speaker_key,
        ),
        expression_culture=_build_echo_expression_culture(
            reports.expression,
            members=members,
            expression_source=expression_source,
        ),
        expression_source=expression_source,
        stickers=build_echo_stickers(
            reports.stickers, sticker_media, conversation_kind=conversation_kind,
            member_names={member.speaker_key: member.display_name for member in members},
            member_search_names={member.speaker_key: member.search_names for member in members},
            private_features=reports.private_stickers,
            group_features=reports.group_stickers,
        ),
        empty_description="" if has_data else EMPTY_DESCRIPTION,
        active_days=(reports.activity.active_days if reports.activity else 0),
        average_messages_per_active_day=(
            reports.activity.average_messages_per_active_day
            if reports.activity
            else 0.0
        ),
    )


def _build_echo_member(
    profile: UserProfile,
    viewer_speaker_key: str | None,
    conversation_kind: str = "unknown",
) -> EchoMemberCard:
    speaker_key = profile.speaker_key or profile.speaker
    primary_name, secondary_name, contextual_name = resolve_member_names(
        remark=profile.remark,
        contextual_name=profile.contextual_name,
        nickname=profile.nickname,
        safe_display_fallback=profile.resolved_display_name,
        conversation_kind=conversation_kind,
    )
    return EchoMemberCard(
        speaker_key=speaker_key,
        display_name=primary_name,
        primary_name=primary_name,
        secondary_name=secondary_name,
        remark=profile.remark,
        contextual_name=contextual_name,
        nickname=profile.nickname,
        is_viewer=(
            viewer_speaker_key is not None
            and speaker_key == viewer_speaker_key
        ),
        message_count=profile.message_count,
        first_seen_order=profile.first_seen_order,
        message_share_percent=profile.message_share_percent,
        average_length=profile.average_length,
        max_length=profile.max_length,
        active_period=format_active_period(
            profile.busiest_hour,
            profile.busiest_weekday,
        ),
        hourly_activity=tuple(
            ChartPoint(label=format_hour(entry.hour), value=float(entry.count))
            for entry in profile.hourly_counts
        ),
        weekday_activity=tuple(
            ChartPoint(
                label=format_weekday(entry.weekday),
                value=float(entry.count),
            )
            for entry in profile.weekday_counts
        ),
        top_words=tuple(
            word.word for word in profile.top_words
            if word.count >= ECHO_MEMBER_WORD_MIN_COUNT
        ),
        weekday_average_activity=tuple(
            ChartPoint(label=format_weekday(entry.weekday), value=entry.average_per_day)
            for entry in profile.weekday_counts
            if entry.average_per_day is not None
        ),
    )
