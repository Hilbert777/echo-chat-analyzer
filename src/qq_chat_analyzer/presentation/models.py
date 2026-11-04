"""Framework-neutral view models for presenting analysis reports.

These models expose display-ready primitives for Echo reports and sharing,
without exposing analysis report internals to the view layer. Nothing here
computes statistics: every number is copied from an already-built report.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from ..media_content import StoredMedia
from ..identity_names import identity_search_names


@dataclass(frozen=True, slots=True)
class ChartPoint:
    """One plottable value with its display label."""

    label: str
    value: float


@dataclass(frozen=True, slots=True)
class EchoMemberCard:
    """Display-ready Echo profile for one conversation member."""

    speaker_key: str
    display_name: str
    is_viewer: bool
    message_count: int
    message_share_percent: float
    average_length: float
    max_length: int
    active_period: str
    hourly_activity: tuple[ChartPoint, ...] = ()
    weekday_activity: tuple[ChartPoint, ...] = ()
    top_words: tuple[str, ...] = ()
    primary_name: str | None = None
    secondary_name: str | None = None
    remark: str | None = None
    contextual_name: str | None = None
    weekday_average_activity: tuple[ChartPoint, ...] = ()
    first_seen_order: int | None = None
    nickname: str | None = None

    @property
    def search_names(self) -> tuple[str, ...]:
        return identity_search_names(self.display_name, self.primary_name, self.secondary_name,
                                     self.remark, self.contextual_name, self.nickname)


@dataclass(frozen=True, slots=True)
class EchoLanguageMember:
    """One display-ready voice in Echo's language-profile chapter."""

    speaker_key: str
    display_name: str
    heading: str
    primary_words: tuple[str, ...] = ()
    context_words: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EchoSharedWord:
    """One display-ready private shared word with a restrained emphasis."""

    word: str
    self_count: int | None
    peer_count: int | None
    emphasis: str


@dataclass(frozen=True, slots=True)
class EchoExpressionItem:
    """One display-ready expression occurrence."""

    expression_key: str
    display_text: str
    count: int
    kind: str
    with_text_message_count: int = 0
    text_only_message_count: int = 0
    asset_key: str | None = None
    nearby_words: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EchoExpressionCombination:
    """Two display-safe expressions that often appeared together."""

    asset_keys: tuple[str | None, str | None]
    count: int
    common_members: tuple[EchoExpressionCombinationMember, ...] = ()
    labels: tuple[str, str] = ()


@dataclass(frozen=True, slots=True)
class EchoExpressionCombinationMember:
    """One sender frequently associated with an expression combination."""

    display_name: str
    count: int
    share_percent: float


@dataclass(frozen=True, slots=True)
class EchoMemberExpression:
    """Display-ready expression profile for one member."""

    speaker_key: str
    display_name: str
    expression_occurrence_count: int
    expression_message_count: int
    expression_share_percent: float
    expression_only_message_count: int
    top_expressions: tuple[EchoExpressionItem, ...] = ()


@dataclass(frozen=True, slots=True)
class EchoExpressionCulture:
    """Display-ready aggregate payload for Echo's expression chapter."""

    available: bool
    expression_message_count: int = 0
    expression_only_message_count: int = 0
    expression_only_rate: float = 0.0
    unique_expression_count: int = 0
    top_expressions: tuple[EchoExpressionItem, ...] = ()
    members: tuple[EchoMemberExpression, ...] = ()
    top_combinations: tuple[EchoExpressionCombination, ...] = ()
    unavailable_reason: str = ""


@dataclass(frozen=True, slots=True)
class EchoLanguageProfile:
    """Prepared group/private product branch for the Echo frontend."""

    mode: str
    available: bool
    members: tuple[EchoLanguageMember, ...] = ()
    unavailable_reason: str = ""
    shared_words: tuple[EchoSharedWord, ...] = ()


@dataclass(frozen=True, slots=True)
class EchoLanguageMessage:
    """One authored projection, shared by selected words and bounded context."""

    ref: int
    speaker_label: str
    timestamp: str
    text: str
    message_type: str
    before_refs: tuple[int, ...] = ()
    after_refs: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class EchoLanguageTerm:
    """Exact lexical matches; member_index addresses language_profile.members."""

    word: str
    member_index: int | None
    occurrence_count: int
    message_refs: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class EchoLanguageSources:
    """Offline originals for displayed language words, not a full chat export."""

    messages: tuple[EchoLanguageMessage, ...]
    terms: tuple[EchoLanguageTerm, ...]


@dataclass(frozen=True, slots=True)
class EchoConversationSession:
    """Display-neutral session detail carried to Echo serialization."""

    start_timestamp: int | None
    end_timestamp: int | None
    duration_seconds: int
    message_count: int
    initiator: str
    participant_count: int = 1
    initiator_sender_key: str | None = None
    self_message_count: int | None = None
    peer_message_count: int | None = None


@dataclass(frozen=True, slots=True)
class EchoSessionInitiator:
    """A named group opener without exposing the source identity key."""

    display_name: str
    count: int
    is_viewer: bool = False
    search_names: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EchoConversationSessions:
    """Session aggregate payload prepared for Echo."""

    threshold_seconds: int
    session_count: int
    average_duration_seconds: float
    median_duration_seconds: float
    longest_duration_seconds: int
    average_message_count: float
    items: tuple[EchoConversationSession, ...] = ()
    private_self_count: int | None = None
    private_peer_count: int | None = None
    private_unknown_count: int | None = None
    private_self_to_peer_ratio: float | None = None
    private_self_share: float | None = None
    private_peer_share: float | None = None
    private_unknown_share: float | None = None
    private_self_peak_start_hour: int | None = None
    private_peer_peak_start_hour: int | None = None
    private_reply_median_self_to_peer_seconds: float | None = None
    private_reply_median_peer_to_self_seconds: float | None = None
    group_self_count: int | None = None
    group_self_share: float | None = None
    group_top_initiator_name: str | None = None
    group_top_initiator_count: int | None = None
    group_top_initiator_share: float | None = None
    group_initiators: tuple[EchoSessionInitiator, ...] = ()
    group_unknown_count: int = 0
    viewer_identity_reliable: bool = False
    start_hour_distribution: tuple[ChartPoint, ...] = ()
    peak_start_hour: int | None = None
    session_character: str | None = None
    loudest_most_messages: EchoConversationSession | None = None
    loudest_longest_duration: EchoConversationSession | None = None
    loudest_most_participants: EchoConversationSession | None = None
    loudest_densest: EchoConversationSession | None = None
    loudest_most_back_and_forth: EchoConversationSession | None = None


@dataclass(frozen=True, slots=True)
class EchoStickerSignature:
    send_count: int
    dated_send_count: int
    date_count: int
    span_days: int
    week_count: int
    share: float
    first_date: str | None
    last_date: str | None


@dataclass(frozen=True, slots=True)
class EchoStickerItem:
    label: str
    count: int
    asset_key: str
    side_counts: tuple[int, ...] = ()
    reliable_member_count: int = 0
    unknown_sender_count: int = 0
    signature: EchoStickerSignature | None = None
    # Private in-process linkage for report media acquisition; never serialized.
    media_key: str = field(default="", repr=False)


@dataclass(frozen=True, slots=True)
class EchoStickerMember:
    label: str
    send_count: int | None = None
    message_count: int | None = None
    per_hundred_messages: float | None = None
    top_materials: tuple[EchoStickerItem, ...] = ()
    exclusive_material_count: int | None = None
    signature: EchoStickerItem | None = None
    unique_material_count: int | None = None
    search_names: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EchoStickerAsset:
    key: str
    data_uri: str
    status: str
    reason: str | None = None
    content: StoredMedia | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class EchoStickerMemoryPoint:
    """A display value/date/turn, never an analysis message reference."""

    label: str
    value: int | float | None = None
    total: int | None = None
    date_count: int | None = None
    start_date: str | None = None
    end_date: str | None = None
    materials: tuple[EchoStickerItem, ...] = ()


@dataclass(frozen=True, slots=True)
class EchoStickerFinding:
    kind: str
    title: str
    period: str = ""
    points: tuple[EchoStickerMemoryPoint, ...] = ()
    materials: tuple[EchoStickerItem, ...] = ()
    note: str = ""
    # The existing unique display label, never a source account/sender ID.
    owner_label: str = ""


@dataclass(frozen=True, slots=True)
class EchoStickerMemory:
    """One selected story, with safe visual findings and bounded media targets."""

    findings: tuple[EchoStickerFinding, ...]
    materials: tuple[EchoStickerItem, ...] = ()
    compact: bool = False


@dataclass(frozen=True, slots=True)
class EchoStickers:
    available: bool
    total_send_count: int = 0
    unique_material_count: int = 0
    unknown_identity_count: int = 0
    unattributed_send_count: int = 0
    covered_message_count: int = 0
    members: tuple[EchoStickerMember, ...] = ()
    shared_material_count: int | None = 0
    shared_materials: tuple[EchoStickerItem, ...] = ()
    assets: tuple[EchoStickerAsset, ...] = ()
    unavailable_reason: str = ""
    conversation_kind: str = "private"
    total_top_materials: tuple[EchoStickerItem, ...] = ()
    exclusive_material_count: int = 0
    per_hundred_messages: float | None = None
    shared_ranking_min_materials: int = 1
    memories: tuple[EchoStickerMemory, ...] = ()


@dataclass(frozen=True, slots=True)
class EchoNudgeMoment:
    timestamp: int
    is_self: bool
    suffix: str = field(default="", repr=False)


@dataclass(frozen=True, slots=True)
class EchoPrivateNudges:
    self_count: int
    peer_count: int
    recent: tuple[EchoNudgeMoment, ...]
    source: str = "wechat"
    self_suffix: str = field(default="", repr=False)
    peer_suffix: str = field(default="", repr=False)
    self_action: str = field(default="", repr=False)
    peer_action: str = field(default="", repr=False)


@dataclass(frozen=True, slots=True)
class EchoReportView:
    """Display-only payload for Echo Report v0.1 Phase A."""

    title: str
    has_data: bool = False
    conversation_kind: str = "unknown"
    conversation_name: str = ""
    time_span: str = ""
    total_message_count: int = 0
    participant_count: int = 0
    hourly_activity: tuple[ChartPoint, ...] = ()
    weekday_activity: tuple[ChartPoint, ...] = ()
    members: tuple[EchoMemberCard, ...] = ()
    conversation_sessions: EchoConversationSessions | None = None
    private_nudges: EchoPrivateNudges | None = None
    language_profile: EchoLanguageProfile | None = None
    language_sources: EchoLanguageSources | None = None
    expression_culture: EchoExpressionCulture | None = None
    expression_source: str | None = None
    stickers: EchoStickers | None = None
    empty_description: str = ""
    active_days: int = 0
    average_messages_per_active_day: float = 0.0
    calendar_day_count: int = 0
    weekday_average_activity: tuple[ChartPoint, ...] = ()
