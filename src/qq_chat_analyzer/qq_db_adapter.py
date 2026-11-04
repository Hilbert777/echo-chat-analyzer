"""Adapter for QQ DB text, expressions, custom PIC stickers and reply facts.

This module only interprets an already-materialized raw payload.  It does not
open a QQ database or otherwise acquire source data.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Any

from .identity_names import first_identity_name, resolve_member_names
from .qq_db_identity import QQ_DB_SELF_NAMESPACE, canonical_qq_uin
from .analysis.timestamps import to_epoch_seconds
from .rich_message import (
    EXPRESSION_KIND_PLATFORM_FACE,
    EXPRESSION_KIND_STICKER,
    ExpressionContent,
    ImageReference,
    StickerIdentity,
    MentionRelation,
    NudgeRelation,
    NonTextContent,
    MessageRelation,
    ReplyRelation,
    RichContent,
    RichMessage,
    SenderIdentity,
    TextContent,
)


QQ_DB_JSON_FORMAT = "qq-db-json"
WARNING_QQ_DB_RECORD_SKIPPED = "qq_db_record_skipped"
_LOGGER = logging.getLogger("qq_chat_analyzer.qq.private_nudges")


def load_qq_db_json(path: str | Path) -> dict[str, Any] | None:
    """Load an already-materialized QQ DB JSON payload, if valid JSON."""
    try:
        input_path = Path(path)
        with input_path.open("r", encoding="utf-8") as file:
            payload = json.load(file)
    except (OSError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def parse_qq_db_rich_messages(
    payload: Mapping[str, Any] | None,
) -> tuple[list[RichMessage], tuple[str, ...]]:
    """Interpret supported group/private content into source-neutral facts."""
    if not is_qq_db_payload(payload):
        return [], ()

    parsed_messages: list[RichMessage] = []
    skipped_record = False
    records = payload["records"]
    session_context = _session_context(payload)
    if session_context is None:
        return [], (WARNING_QQ_DB_RECORD_SKIPPED,) if records else ()
    self_uin = _self_uin(payload)
    query = payload["query"]
    nudge_context = _private_nudge_context(payload, session_context)
    reply_targets: dict[str, list[str | None]] = {}
    if session_context[0] == "group":
        for record in records:
            if not _record_in_session(record, query):
                continue
            sequence = _sequence_key(record["fields"].get("40003"))
            if sequence is not None:
                reply_targets.setdefault(sequence, []).append(
                    _stringify_identifier(record.get("record_id"))
                )
    for record in records:
        in_session = _record_in_session(record, query)
        parsed_message = _parse_record(
            record,
            session_context,
            self_uin,
            reply_targets if in_session else {},
            nudge_context if in_session else None,
        )
        if parsed_message is None:
            skipped_record = True
        else:
            parsed_messages.append(parsed_message)

    warnings = (WARNING_QQ_DB_RECORD_SKIPPED,) if skipped_record else ()
    if session_context[0] == "private":
        _LOGGER.info("QQ private_nudges records=%d accepted=%d identity_bound=%s",
                     len(records), sum(message.message_type == "nudge" for message in parsed_messages),
                     nudge_context is not None)
    return parsed_messages, warnings


def is_qq_db_payload(payload: Mapping[str, Any] | None) -> bool:
    return (
        isinstance(payload, Mapping)
        and payload.get("format") == QQ_DB_JSON_FORMAT
        and payload.get("format_version") == 0
        and payload.get("source") == "qq"
        and payload.get("source_type") == QQ_DB_JSON_FORMAT
        and isinstance(payload.get("records"), list)
    )


@dataclass(frozen=True, slots=True)
class QQPicMediaElement:
    """One PIC segment's locating facts; never a path, URL or display value."""

    segment_id: str | None = field(default=None, repr=False)
    file_uuid: str | None = field(default=None, repr=False)
    content_md5: str | None = field(default=None, repr=False)
    is_custom_sticker: bool = False


@dataclass(frozen=True, slots=True)
class QQPicMediaFact:
    """One already-materialized PIC message, reduced to mapping inputs.

    The media fetch uses these facts to revalidate one selected send. They do
    not authorize a file read or network request by themselves.
    """

    message_id: str = field(repr=False)
    timestamp: int | float | str = field(repr=False)
    peer_uid: str | None = field(default=None, repr=False)
    conversation_type: str = "unknown"
    is_self: bool | None = None
    elements: tuple[QQPicMediaElement, ...] = ()
    conversation_id: str | None = field(default=None, repr=False)
    group_id: str | None = field(default=None, repr=False)
    sender_uin: str | None = field(default=None, repr=False)
    sender_uid: str | None = field(default=None, repr=False)


def extract_qq_pic_media_facts(
    payload: Mapping[str, Any] | None,
    message_ids: Collection[str] | None = None,
) -> tuple[QQPicMediaFact, ...]:
    """Return only requested PIC message facts from an existing payload.

    The whole ``40800`` blob is already preserved by acquisition; this function
    reads the locating fields the normal semantic model does not surface. It
    never opens a database, follows a path or URL, reads media, or writes a
    file. Message text, sender names, paths and URLs are never returned.
    """
    if not is_qq_db_payload(payload):
        return ()
    session_context = _session_context(payload)
    if session_context is None:
        return ()
    conversation_type, conversation_id = session_context
    self_uin = _self_uin(payload)
    wanted = None if message_ids is None else {str(value) for value in message_ids}
    facts: list[QQPicMediaFact] = []
    for record in payload["records"]:
        if not isinstance(record, Mapping):
            continue
        message_id = _stringify_identifier(record.get("record_id"))
        if message_id is None or (wanted is not None and message_id not in wanted):
            continue
        fields = record.get("fields")
        if not isinstance(fields, Mapping):
            continue
        timestamp = fields.get("40050")
        if (
            not isinstance(timestamp, (int, float, str))
            or isinstance(timestamp, bool)
            or record.get("blob_encoding") != "base64"
            or not isinstance(record.get("message_blob"), str)
        ):
            continue
        try:
            decoded = base64.b64decode(record["message_blob"], validate=True)
        except (binascii.Error, ValueError):
            continue
        elements: list[QQPicMediaElement] = []
        for field_number, wire_type, value in _protobuf_fields(decoded):
            if field_number != 40800 or wire_type != 2:
                continue
            segment = _protobuf_fields(value)
            values = {(tag, wire): data for tag, wire, data in segment}
            if values.get((45002, 0)) != 2:
                continue
            subtype = [item for tag, wire, item in segment if tag == 45003 and wire == 0]
            segment_id = values.get((45001, 0))
            file_uuid = _utf8_text(values.get((45503, 2)))
            if conversation_type == "group":
                # Repeated locating fields cannot silently pick the last value.
                if sum(tag == 45001 for tag, _, _ in segment) != 1:
                    segment_id = None
                if sum(tag == 45503 for tag, _, _ in segment) != 1:
                    file_uuid = None
            elements.append(
                QQPicMediaElement(
                    segment_id=(
                        str(segment_id)
                        if isinstance(segment_id, int)
                        and not isinstance(segment_id, bool)
                        and segment_id > 0
                        else None
                    ),
                    file_uuid=file_uuid,
                    content_md5=_pic_digest(segment),
                    is_custom_sticker=subtype == [1],
                )
            )
        if not elements:
            continue
        group_id = _stringify_identifier(fields.get("40030"))
        if not (
            conversation_type == "group" and group_id
            and re.fullmatch(r"[1-9][0-9]{4,19}", group_id)
            and _stringify_identifier(fields.get("40021")) == group_id == conversation_id
            and _record_in_session(record, payload["query"])
        ):
            group_id = None
        facts.append(
            QQPicMediaFact(
                message_id=message_id,
                timestamp=timestamp,
                peer_uid=(
                    _stringify_identifier(fields.get("40021"))
                    or _utf8_text(fields.get("40021"))
                ) if conversation_type == "private" else None,
                conversation_type=conversation_type,
                is_self=_resolve_is_self(fields.get("40033"), self_uin),
                elements=tuple(elements),
                conversation_id=conversation_id,
                group_id=group_id,
                sender_uin=canonical_qq_uin(fields.get("40033")) if conversation_type == "group" else None,
                sender_uid=(
                    _stringify_identifier(fields.get("40020")) or _utf8_text(fields.get("40020"))
                ) if conversation_type == "group" else None,
            )
        )
    return tuple(facts)


def _parse_record(
    record: Any,
    session_context: tuple[str, str],
    self_uin: Any,
    reply_targets: dict[str, list[str | None]],
    nudge_context: Mapping[str, str] | None = None,
) -> RichMessage | None:
    if not isinstance(record, Mapping):
        return None
    fields = record.get("fields")
    if not isinstance(fields, Mapping):
        return None

    # These field numbers only describe the feasibility-supported Adapter v0
    # slice; they are not stable across QQ versions.
    sender_id = _stringify_identifier(fields.get("40033"))
    timestamp = fields.get("40050")
    if (
        not isinstance(timestamp, (int, float, str))
        or isinstance(timestamp, bool)
    ):
        return None

    contents, relations, has_reply, parties = _extract_semantics(record, reply_targets, nudge_context)
    if parties is not None:
        message_id = _nonzero_identifier(record.get("record_id"))
        if (message_id is None or re.fullmatch(r"-?[1-9][0-9]{0,19}", message_id) is None
                or not -(1 << 63) <= int(message_id) <= (1 << 64) - 1
                or message_id != _nonzero_identifier(fields.get("40001"))
                or to_epoch_seconds(timestamp) is None):
            return None
        actor, _ = parties
        is_self = actor == self_uin
        return RichMessage(
            message_id=message_id, source="qq", source_type=QQ_DB_JSON_FORMAT,
            conversation_id=session_context[1], conversation_type="private",
            sender=SenderIdentity(identity_id=actor, display_name="我" if is_self else
                                  first_identity_name(nudge_context.get("peer_name")) or "对方"),
            timestamp=timestamp, message_type="nudge", contents=(),
            relations=relations,
            is_system=True, is_self=is_self,
        )
    if sender_id is None or (not contents and not relations and not has_reply):
        return None

    conversation_type, conversation_id = session_context
    sender_data = record.get("sender")
    if not isinstance(sender_data, Mapping):
        sender_data = {}
    remark = first_identity_name(sender_data.get("remark"))
    nickname = first_identity_name(sender_data.get("nickname"), sender_data.get("name"))
    contextual_name = first_identity_name(sender_data.get("groupCard"))
    is_self = _resolve_is_self(fields.get("40033"), self_uin)
    display_name, _, _ = resolve_member_names(
        remark=remark,
        contextual_name=contextual_name,
        nickname=nickname,
        safe_display_fallback=(
            first_identity_name(sender_data.get("displayName"))
            or ("我" if is_self else "未知成员")
        ),
        conversation_kind=conversation_type,
    )
    if canonical_qq_uin(fields.get("40033")) is None:
        # Preserve the record, but a zero-value sender cannot identify a person.
        # Do not let an incidental label become a fallback participant identity.
        sender_id = None
        display_name = ""
        remark = nickname = contextual_name = None
    return RichMessage(
        message_id=_stringify_identifier(record.get("record_id")),
        source="qq",
        source_type=QQ_DB_JSON_FORMAT,
        conversation_id=conversation_id,
        conversation_type=conversation_type,
        sender=SenderIdentity(
            identity_id=sender_id,
            display_name=display_name,
            remark=remark,
            nickname=nickname,
            contextual_name=contextual_name,
        ),
        timestamp=timestamp,
        message_type="reply" if has_reply else _content_message_type(contents),
        contents=contents,
        relations=relations,
        is_self=is_self,
    )


def _content_message_type(contents: tuple[RichContent, ...]) -> str:
    nontext = [part for part in contents if isinstance(part, NonTextContent)]
    if not nontext:
        return "text"
    if len(nontext) != len(contents) or len({part.kind for part in nontext}) > 1:
        return "mixed"
    return nontext[0].kind


def _self_uin(payload: Mapping[str, Any]) -> Any:
    """Return the payload's bound self value, or ``None`` when absent.

    The namespace must match the Direct DB ``qq_uin`` namespace exactly; a
    mismatched or missing context is treated as unknown rather than guessed.
    """
    self_context = payload.get("self")
    if not isinstance(self_context, Mapping):
        return None
    if self_context.get("namespace") != QQ_DB_SELF_NAMESPACE:
        return None
    return self_context.get("value")


def _resolve_is_self(sender_value: Any, self_uin: Any) -> bool | None:
    """Compare canonical sender (40033) against canonical self identity.

    ``None`` means the source cannot reliably judge the sender; it is never
    collapsed to peer (``False``).  A zero / NULL / invalid sender or an
    unreliable self identity both yield ``None``.
    """
    canonical_sender = canonical_qq_uin(sender_value)
    canonical_self = canonical_qq_uin(self_uin)
    if canonical_sender is None or canonical_self is None:
        return None
    return canonical_sender == canonical_self


def _session_context(payload: Mapping[str, Any]) -> tuple[str, str] | None:
    query = payload.get("query")
    if not isinstance(query, Mapping):
        return None
    session_type = query.get("session_type")
    if session_type not in {"group", "private"}:
        return None
    session_object = _nonzero_identifier(query.get("session_object"))
    if session_object is not None:
        return session_type, session_object
    internal_key = _stringify_identifier(query.get("internal_key"))
    if internal_key is None:
        return None
    return session_type, f"{session_type}:{internal_key}"


def _record_in_session(record: Any, query: Mapping[str, Any]) -> bool:
    if (
        not isinstance(record, Mapping)
        or not isinstance(record.get("fields"), Mapping)
    ):
        return False
    fields = record["fields"]
    source_meta = record.get("source_meta")
    if isinstance(source_meta, Mapping):
        table = source_meta.get("table")
        expected = (
            "group_msg_table"
            if query.get("session_type") == "group"
            else "c2c_msg_table"
        )
        if table is not None and table != expected:
            return False
    internal_key = _stringify_identifier(query.get("internal_key"))
    if internal_key is not None:
        return _stringify_identifier(fields.get("40027")) == internal_key
    session_object = _nonzero_identifier(query.get("session_object"))
    return (
        session_object is not None
        and _nonzero_identifier(fields.get("40030")) == session_object
    )


def _sequence_key(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value) if value > 0 else None
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        try:
            number = int(value)
        except ValueError:
            return None
        return str(number) if number > 0 else None
    return None


def _extract_semantics(
    record: Mapping[str, Any],
    reply_targets: dict[str, list[str | None]],
    nudge_context: Mapping[str, str] | None = None,
) -> tuple[tuple[RichContent, ...], tuple[MessageRelation, ...], bool, tuple[str, str] | None]:
    if record.get("blob_encoding") != "base64":
        return (), (), False, None
    blob = record.get("message_blob")
    if not isinstance(blob, str):
        return (), (), False, None
    try:
        decoded = base64.b64decode(blob, validate=True)
    except (binascii.Error, ValueError):
        return (), (), False, None

    segments = [_protobuf_fields(value) for tag, wire, value in _protobuf_fields(decoded) if tag == 40800 and wire == 2]
    if nudge_context is not None and len(segments) == 1:
        poke = _parse_private_poke(segments[0], nudge_context)
        if poke is not None:
            actor, target, action, suffix = poke
            return (), (NudgeRelation(target_identity_id=target, action=action, suffix=suffix),), False, (actor, target)

    # These field numbers only describe the feasibility-supported Adapter v0
    # slice; they are not stable across QQ versions.
    contents: list[RichContent] = []
    relations: list[MessageRelation] = []
    has_reply = False
    expression_position = 0
    for fields in segments:
        values = {(tag, wire): data for tag, wire, data in fields}
        content_type = values.get((45002, 0))
        if content_type == 7:
            has_reply = True
            sequence = _sequence_key(values.get((47402, 0)))
            targets = reply_targets.get(sequence, []) if sequence is not None else []
            if len(targets) == 1 and targets[0] is not None:
                relations.append(ReplyRelation(target_message_id=targets[0]))
            continue
        mention_type = values.get((45102, 0))
        if (
            content_type == 1
            and isinstance(mention_type, int)
            and mention_type != 0
        ):
            # 45105 is an NT UID, not this adapter's QQ UIN namespace.
            # Preserve the mention occurrence without inventing a UIN target.
            relations.append(
                MentionRelation(
                    target_identity_id=None,
                    display_text=_utf8_text(values.get((45101, 2))),
                )
            )
            continue
        content = _content_from_segment(fields, expression_position)
        if content is not None:
            contents.append(content)
            if isinstance(content, ExpressionContent):
                expression_position += 1
    return tuple(contents), tuple(relations), has_reply, None


def _private_nudge_context(payload: Mapping[str, Any], session: tuple[str, str]) -> Mapping[str, str] | None:
    context = payload.get("private_nudge_context")
    if session[0] != "private" or not isinstance(context, Mapping):
        return None
    if (context.get("namespace") != QQ_DB_SELF_NAMESPACE
            or context.get("conversation_id") != session[1]
            or not isinstance(context.get("session_key"), str)
            or not context.get("session_key")
            or context.get("session_key") != payload["query"].get("internal_key")
            or context.get("owner_uin") != _self_uin(payload)):
        return None
    for key in ("owner_uin", "peer_uin"):
        value = context.get(key)
        if not isinstance(value, str) or re.fullmatch(r"[1-9][0-9]{4,19}", value) is None:
            return None
    for key in ("owner_uid", "peer_uid"):
        value = context.get(key)
        if not isinstance(value, str) or re.fullmatch(r"u_[A-Za-z0-9_-]{10,64}", value) is None:
            return None
    if context["owner_uin"] == context["peer_uin"] or context["owner_uid"] == context["peer_uid"]:
        return None
    return context


def _parse_private_poke(fields: tuple, context: Mapping[str, str]) -> tuple[str, str, str, str] | None:
    """Private gray-tip 1061: structured parties and JSON must agree in order."""
    def values(tag, wire):
        return [value for number, kind, value in fields if number == tag and kind == wire]

    if values(45002, 0) != [8] or values(48273, 0) != [1061]:
        return None
    if values(48212, 0) not in ([], [1061]):
        return None
    encoded, structured = values(48271, 2), values(48210, 2)
    if len(encoded) != 1 or len(encoded[0]) > 65536 or len(structured) != 2:
        return None

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        data = json.loads(encoded[0].decode("utf-8"), object_pairs_hook=unique_object)
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list) or len(items) > 64 or any(not isinstance(item, dict) for item in items):
            return None
        parties = [item["uid"] for item in items if item.get("uid")]
        expected = {context["owner_uid"], context["peer_uid"]}
        if len(parties) != 2 or any(not isinstance(uid, str) for uid in parties) or set(parties) != expected:
            return None
        for segment, uid in zip(structured, parties):
            encoded_uid = [value for tag, wire, value in _protobuf_fields(segment) if tag == 1005 and wire == 2]
            if len(encoded_uid) != 1 or encoded_uid[0].decode("utf-8") != uid:
                return None
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None
    identities = {context["owner_uid"]: context["owner_uin"], context["peer_uid"]: context["peer_uin"]}
    action, suffix = _poke_style(items, context)
    return identities[parties[0]], identities[parties[1]], action, suffix


def _poke_style(items: list[dict[str, Any]], context: Mapping[str, str]) -> tuple[str, str]:
    """Read only text around the two confirmed roles; never names, links or images."""
    positions = [index for index, item in enumerate(items) if item.get("uid")]
    actor, target = positions
    if actor != 0 or any(items[index].get("type") != "qq" for index in positions):
        return "", ""

    def text(parts):
        chunks = []
        for item in parts:
            if item.get("type") == "img" and "txt" not in item:
                continue
            if item.get("type") != "nor" or not isinstance(item.get("txt"), str):
                return None
            chunks.append(item["txt"])
        return "".join(chunks)

    action, suffix = text(items[actor + 1:target]), text(items[target + 1:])
    if not action or not action.strip() or suffix is None:
        return "", ""
    # A malformed display fragment must not expose the bound source identities.
    if any(context[key] in action + suffix for key in ("owner_uid", "peer_uid", "owner_uin", "peer_uin")):
        return "", ""
    return action, suffix


def _pic_digest(fields: tuple[tuple[int, int, int | bytes], ...]) -> str | None:
    """Return one trustworthy PIC MD5, or ``None`` on missing conflict.

    45406 is the raw 16-byte digest. 45424 has version-dependent encoding and
    weaker documented semantics: it may veto conflicting evidence, but cannot
    authorize a match alone. Repeated digest fields are ambiguous.
    """
    values = {(tag, wire): value for tag, wire, value in fields}
    raw_md5 = values.get((45406, 2))
    digest = raw_md5.hex() if isinstance(raw_md5, bytes) and len(raw_md5) == 16 else None
    alternate = values.get((45424, 2))
    if isinstance(alternate, bytes):
        if len(alternate) == 16:
            alternate_digest = alternate.hex()
        else:
            text = _utf8_text(alternate)
            alternate_digest = (
                text.lower() if text and re.fullmatch(r"[0-9a-fA-F]{32}", text) else None
            )
        if alternate_digest != digest:
            digest = None
    if any(len({value for tag, wire, value in fields if tag == digest_tag and wire == 2}) > 1
           for digest_tag in (45406, 45424)):
        digest = None
    return digest


def _content_from_segment(
    fields: tuple[tuple[int, int, int | bytes], ...], expression_position: int,
) -> RichContent | None:
    values = {(tag, wire): value for tag, wire, value in fields}
    content_type = values.get((45002, 0))
    if content_type == 1:
        text = _utf8_text(values.get((45101, 2)))
        return TextContent(text) if text is not None else None
    if content_type == 2:
        raw_md5 = values.get((45406, 2))
        digest = _pic_digest(fields)
        paths = tuple(
            text for tag in (45403, 45812)
            if (text := _utf8_text(values.get((tag, 2))))
        )
        original = values.get((45418, 0))
        media = ImageReference(
            content_md5=digest,
            is_original=bool(original) if original in (0, 1) else None,
            local_paths=paths,
        ) if raw_md5 is not None or paths or original is not None else None
        # Only the observed Windows QQ value 45003=1 marks a custom sticker send.
        # Ordinary images, unobserved values and repeated fields remain images.
        subtype = [value for tag, wire, value in fields if tag == 45003 and wire == 0]
        sticker = StickerIdentity(
            classification_basis="qq_pic_45003_custom",
            identity_basis="qq_pic_45406_md5" if digest else "unresolved",
            # Separate namespace from qq-marketface and wechat:md5 identities.
            identity_key=f"qq-pic:md5:{digest}" if digest else None,
            content_md5=digest,
            local_paths=paths,
        ) if subtype == [1] else None
        return NonTextContent("image", media=media, sticker=sticker)
    if content_type == 6:
        face_index = values.get((47601, 0))
        if not isinstance(face_index, int):
            return None
        key = str(face_index)
        kind = EXPRESSION_KIND_PLATFORM_FACE
        label = _utf8_text(values.get((47602, 2)))
        fallback = f"[QQ表情 {key}]"
    elif content_type == 11:
        identity = values.get((45600, 2))
        if not isinstance(identity, bytes) or not identity:
            return None
        # Opaque content-derived identity, not a decoded QQ emoji ID or a
        # resource MD5. Keep its namespace separate from numeric QQ faces.
        key = "qq-marketface:sha256:" + hashlib.sha256(identity).hexdigest()
        kind = EXPRESSION_KIND_STICKER
        label = _utf8_text(values.get((80900, 2)))
        fallback = "[贴图]"
    else:
        # In particular, do not recurse into ReplyBlock quoted content or
        # interpret 80900 outside the MarketFace context.
        return NonTextContent("unknown")
    if not label:
        label = next(
            (text for tag, wire, value in fields
             if tag == 45815 and wire == 2 and (text := _utf8_text(value))),
            None,
        )
    return ExpressionContent(
        expression_kind=kind,
        expression_key=key,
        display_text=label or fallback,
        source="qq",
        position=expression_position,
        sticker=StickerIdentity(
            classification_basis="qq_content_type_11",
            identity_basis="qq_45600_sha256_opaque",
            identity_key=key,
            local_paths=tuple(text for tag in (45812, 45814)
                              if (text := _utf8_text(values.get((tag, 2))))),
        ) if kind == EXPRESSION_KIND_STICKER else None,
    )


def _utf8_text(value: int | bytes | None) -> str | None:
    if not isinstance(value, bytes):
        return None
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _protobuf_fields(data: bytes) -> tuple[tuple[int, int, int | bytes], ...]:
    fields: list[tuple[int, int, int | bytes]] = []
    offset = 0
    while offset < len(data):
        tag = _read_varint(data, offset)
        if tag is None:
            return ()
        tag_value, offset = tag
        field_number = tag_value >> 3
        wire_type = tag_value & 0x07
        if field_number == 0:
            return ()
        if wire_type == 0:
            value = _read_varint(data, offset)
            if value is None:
                return ()
            scalar, offset = value
            fields.append((field_number, wire_type, scalar))
        elif wire_type in {1, 5}:
            # Fixed-width fields carry no semantics consumed by this adapter.
            # Skip only a complete value; never salvage a truncated container.
            end = offset + (8 if wire_type == 1 else 4)
            if end > len(data):
                return ()
            offset = end
        elif wire_type == 2:
            length = _read_varint(data, offset)
            if length is None:
                return ()
            size, offset = length
            end = offset + size
            if end > len(data):
                return ()
            fields.append((field_number, wire_type, data[offset:end]))
            offset = end
        else:
            return ()
    return tuple(fields)


def _read_varint(data: bytes, offset: int) -> tuple[int, int] | None:
    value = 0
    for shift in range(0, 70, 7):
        if offset >= len(data):
            return None
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
    return None


def _nonzero_identifier(value: Any) -> str | None:
    identifier = _stringify_identifier(value)
    return identifier if identifier not in {None, "0"} else None


def _stringify_identifier(value: Any) -> str | None:
    if isinstance(value, str):
        return value or None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return None
