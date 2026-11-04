"""Bounded sticker views selected from complete scoped analysis results."""
from __future__ import annotations

import logging
from collections import Counter
from dataclasses import replace
from typing import TYPE_CHECKING

from ..analysis.stickers import StickerConversationReport, StickerMaterialUsage, StickerMedia, StickerHabit
from ..media_content import StoredMedia
from .models import EchoStickerAsset, EchoStickerItem, EchoStickerMember, EchoStickers, EchoStickerSignature
from .sticker_memories import build_sticker_memories, build_group_sticker_memories
from ..analysis.sticker_stories import select_additional_sticker_stories

if TYPE_CHECKING:
    from ..analysis.sticker_features import PrivateStickerFeatures
    from ..analysis.group_stickers import GroupStickerFeatures


TOP_MATERIAL_LIMIT = 10
REPORT_STICKER_BYTE_BUDGET = 16 * 1024 * 1024
SHARED_RANKING_MIN_MATERIALS = 1


def sticker_report_targets(chapter: EchoStickers | None) -> tuple[EchoStickerItem, ...]:
    """The selected rankings/signature union, once per reliable material."""
    if chapter is None or not chapter.available:
        return ()
    # Global/shared leaders must get a chance before a large member long tail
    # consumes the foreground request budget. This changes acquisition order,
    # never a ranking or a member's qualification.
    items = list(chapter.total_top_materials) + list(chapter.shared_materials)
    for memory in chapter.memories:
        if not memory.compact:
            items.extend(memory.materials)
    for member in chapter.members:
        if chapter.conversation_kind == "group" and member.signature is not None:
            items.append(member.signature)
    member_limit = max((len(member.top_materials) for member in chapter.members), default=0)
    for rank in range(member_limit):
        for member in chapter.members:
            if rank < len(member.top_materials):
                items.append(member.top_materials[rank])
    # Compact discoveries use the remaining budget after the main report.
    for memory in chapter.memories:
        if memory.compact:
            items.extend(memory.materials)
    return tuple({item.media_key: item for item in items if item.media_key}.values())


def assemble_sticker_assets(chapter: EchoStickers, media: tuple[StickerMedia, ...]) -> EchoStickers:
    """Use the same targets for recovery and export; retain fixed failure states."""
    by_key = {item.media_key: item for item in media}
    remaining_bytes = REPORT_STICKER_BYTE_BUDGET
    mime_types = {"PNG": "image/png", "APNG": "image/png", "JPEG": "image/jpeg",
                  "GIF": "image/gif", "WEBP": "image/webp"}
    assets = []
    qq_stored = any(item.stored is not None or item.media_key.startswith("qq-pic:") for item in media)
    for target in sticker_report_targets(chapter):
        original = by_key.get(target.media_key)
        status, reason, uri = "not_requested", "no_media_requested", ""
        content = None
        if original is not None:
            status, reason = original.status, original.reason
            if status in {"available", "preview"}:
                raw, mime = original.original_bytes, mime_types.get((original.format or "").upper())
                if not mime or (not raw and original.stored is None):
                    status, reason = "unsupported", "report_image_unavailable"
                elif not qq_stored and len(raw) > remaining_bytes:
                    status, reason = "limited", "report_asset_budget"
                else:
                    # Retain the existing local acquisition/report allowance;
                    # only storage changes, never the image bytes or identity.
                    if not qq_stored:
                        remaining_bytes -= len(raw)
                    suffix = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}[mime]
                    uri = f"media/{original.sha256}.{suffix}"
                    content = original.stored if original.stored is not None else StoredMedia(raw)
        assets.append(EchoStickerAsset(target.asset_key, uri, status, reason, content))
    assembled = replace(chapter, assets=tuple(assets))
    if chapter.conversation_kind == "private":
        logging.getLogger("qq_chat_analyzer.desktop.analysis_diagnostics").info(
            "[sticker-media-counts] before=%d after=%d assets=%d usable_assets=%d",
            len(chapter.memories), len(assembled.memories), len(assets),
            sum(a.status in {"available", "preview"} for a in assets))
    return assembled


def build_echo_stickers(
    reports: tuple[StickerConversationReport, ...],
    media: tuple[StickerMedia, ...],
    *,
    conversation_kind: str,
    member_names: dict[str, str] | None = None,
    member_search_names: dict[str, tuple[str, ...]] | None = None,
    private_features: PrivateStickerFeatures | None = None,
    group_features: GroupStickerFeatures | None = None,
) -> EchoStickers:
    if conversation_kind not in {"private", "group"}:
        return EchoStickers(available=False)
    if not reports:
        return EchoStickers(available=True, conversation_kind=conversation_kind,
                            members=(EchoStickerMember("我"), EchoStickerMember("TA")) if conversation_kind == "private" else ())
    if len(reports) != 1 or reports[0].conversation_type != conversation_kind or not reports[0].conversation_id:
        return EchoStickers(available=False, unavailable_reason="当前数据无法限定为同一会话。")
    report, = reports
    # Never combine multiple participants into one private side. A source must
    # reliably establish both sender identity and self marker for attribution.
    private = conversation_kind == "private"
    member_limit = 5
    if not private:
        sending_members = sum(member.send_count > 0 for member in report.members)
        member_limit = 3 if sending_members >= 100 else 5 if sending_members >= 10 else 10
    sides = []
    if private:
        for own in (True, False):
            candidates = [member for member in report.members if member.is_self is own]
            sides.append(candidates[0] if len(candidates) == 1 else None)
        member_labels = ["我", "TA"]
    else:
        # All reliable IDs remain distinct, including members without self markers.
        sides = sorted(report.members, key=lambda member: (-bool(report.signature(member.sender_id)),
                                                         -member.send_count, member.sender_id))
        names = {member.sender_id: (member_names or {}).get(member.sender_id, "").strip()
                 for member in sides}
        names = {sender: name for sender, name in names.items() if name and name != sender}
        name_counts = Counter(names.values())
        ordinals = Counter()
        labels_by_sender = {}
        reserved = set(names.values())
        for member in sorted(sides, key=lambda member: member.sender_id):
            name = names.get(member.sender_id)
            if name and name_counts[name] == 1:
                label = name
            else:
                base = name or "未命名成员"
                ordinals[base] += 1
                label = f"{base}（{ordinals[base]}）"
                while label in reserved:
                    ordinals[base] += 1
                    label = f"{base}（{ordinals[base]}）"
            reserved.add(label)
            labels_by_sender[member.sender_id] = label
        member_labels = [labels_by_sender[member.sender_id] for member in sides]
    labels = {item.identity_key: f"素材 {index:02d}" for index, item in
              enumerate(sorted(report.materials, key=lambda item: item.identity_key), 1)}
    asset_keys = {key: f"sticker:{index}" for index, key in enumerate(labels, 1)}

    def item_view(item: StickerMaterialUsage, count: int, *, habit: StickerHabit | None = None) -> EchoStickerItem:
        key = asset_keys[item.identity_key]
        counts = dict(item.member_counts)
        return EchoStickerItem(
            label=labels[item.identity_key], asset_key=key, count=count,
            media_key=item.media_key,
            side_counts=tuple(counts.get(side.sender_id, 0) for side in sides) if private and all(sides) else (),
            reliable_member_count=item.reliable_member_count, unknown_sender_count=item.unknown_sender_count,
            signature=EchoStickerSignature(
                habit.send_count, habit.dated_send_count, habit.date_count, habit.span_days,
                habit.week_count, habit.share, habit.first_date, habit.last_date,
            ) if habit else None,
        )

    members = []
    for label, side in zip(member_labels, sides):
        if side is None:
            members.append(EchoStickerMember(label))
            continue
        ranked = report.ranking(side.sender_id)
        signature = report.signature(side.sender_id)
        exclusive_count = report.exclusive_material_count(side.sender_id) if not private or all(sides) else None
        def personal_item(item, count):
            return item_view(item, count, habit=signature if signature and signature.identity_key == item.identity_key else None)
        signature_view = next((personal_item(item, count) for item, count in ranked
                               if signature and item.identity_key == signature.identity_key), None)
        members.append(EchoStickerMember(
            label=label, send_count=side.send_count, message_count=side.message_count,
            per_hundred_messages=side.per_hundred_messages,
            top_materials=tuple(personal_item(item, count) for item, count in ranked[:member_limit]),
            exclusive_material_count=exclusive_count,
            signature=signature_view,
            unique_material_count=len(ranked),
            search_names=(member_search_names or {}).get(side.sender_id, ()),
        ))
    shared = report.shared_ranking(tuple(side.sender_id for side in sides)) if private and all(sides) else (
        report.shared_ranking() if not private else ())
    shared_views = tuple(
        item_view(item, count) for item, count in shared[:TOP_MATERIAL_LIMIT]
    ) if not private and len(shared) >= SHARED_RANKING_MIN_MATERIALS else ()
    total_views = tuple(item_view(item, count) for item, count in report.ranking()[:TOP_MATERIAL_LIMIT]) if not private else ()
    chapter = EchoStickers(
        available=True, total_send_count=report.total_send_count,
        unique_material_count=len(report.materials), unknown_identity_count=report.unknown_identity_count,
        unattributed_send_count=report.total_send_count - sum(side.send_count for side in sides if side),
        covered_message_count=report.message_count, members=tuple(members),
        shared_material_count=None if private and not all(sides) and report.total_send_count else len(shared),
        shared_materials=shared_views,
        conversation_kind=conversation_kind, total_top_materials=total_views,
        exclusive_material_count=report.exclusive_material_count(),
        per_hundred_messages=100 * report.total_send_count / report.message_count if report.message_count else None,
        shared_ranking_min_materials=SHARED_RANKING_MIN_MATERIALS,
    )
    matched = (private and all(sides) and private_features is not None and private_features.available
            and private_features.source == report.source
            and private_features.conversation_id == report.conversation_id)
    if matched:
        selected_ids = set(private_features.stories.featured_ids)
        if selected_ids:
            selected_ids.update(story.story_id for story in select_additional_sticker_stories(private_features))
        selected_media = {key for story in private_features.stories.candidates
                          if story.story_id in selected_ids for key in story.media_keys}
        chapter = replace(chapter, memories=build_sticker_memories(private_features,
            {item.media_key: item_view(item, item.count) for item in report.materials if item.media_key in selected_media},
            {side.sender_id: label for side, label in zip(sides, member_labels)}))
    group_matched = (not private and group_features is not None and group_features.available
            and group_features.source == report.source and group_features.conversation_id == report.conversation_id)
    if group_matched:
        selected_media = {key for story in group_features.stories for key in story.media_keys}
        chapter = replace(chapter, memories=build_group_sticker_memories(group_features,
            {item.media_key: item_view(item, item.count) for item in report.materials if item.media_key in selected_media},
            labels_by_sender))
    if not private:
        logging.getLogger("qq_chat_analyzer.desktop.analysis_diagnostics").info(
            "[group-sticker-projection-counts] available=%d matched_scope=%d stories=%d memories=%d findings=%d",
            int(bool(group_features and group_features.available)), int(bool(group_matched)),
            len(group_features.stories) if group_features else 0,
            len(chapter.memories), sum(len(m.findings) for m in chapter.memories))
    if private:
        logging.getLogger("qq_chat_analyzer.desktop.analysis_diagnostics").info(
            "[sticker-projection-counts] featured=%d matched_scope=%d memories=%d findings=%d",
            len(private_features.stories.featured_ids) if private_features else 0, int(bool(matched)),
            len(chapter.memories), sum(len(m.findings) for m in chapter.memories))
    return assemble_sticker_assets(chapter, media)
