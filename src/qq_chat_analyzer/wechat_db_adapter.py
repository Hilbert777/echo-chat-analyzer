"""Convert WCDB query exports of WeChat 4.x databases into rich messages.

The matching provider (``providers.wechat_database_provider``) writes a JSON
document that keeps the database rows exactly as ``wcdb_cli`` returned them,
plus the conversation the rows were read from:

    {
      "source": "wechat-db",
      "conversation": {"username": "wxid_example", "display_name": "..."},
      "messages": [
        {
          "local_id": 1,
          "server_id": 900001,
          "local_type": 1,
          "create_time": 1753412807,
          "message_content": "hello",
          "user_name": "wxid_sender"
        }
      ]
    }

This module never opens a database, never touches WCDB, and never decides
whether a message is worth analyzing. It maps rows onto the source-neutral
rich model, then projects that model for legacy analysis callers.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping
from typing import Any
from xml.etree import ElementTree

import zstandard as zstd

from .legacy_projection import project_legacy_message, project_legacy_messages
from .message import ChatMessage
from .analysis.timestamps import to_utc_datetime
from .rich_message import (
    EXPRESSION_KIND_PLATFORM_FACE,
    EXPRESSION_KIND_STICKER,
    ExpressionContent,
    NudgeRelation,
    RichContent,
    RichMessage,
    SenderIdentity,
    StickerIdentity,
    TextContent,
)
from .wechat_official_emojis import canonical_wechat_emoji_name


WECHAT_PLATFORM = "wechat"
DB_JSON_FORMAT = "wechat-db-json"
SOURCE_MARKER = "wechat-db"

TEXT_LOCAL_TYPE = 1
STICKER_LOCAL_TYPE = 47
PAT_LOCAL_TYPE = (62 << 32) | 49
_TEXT_TYPE = "text"
_STICKER_TYPE = "sticker"

# Types 1/47 carry authored text/stickers. The verified private pat subtype
# is handled separately as a relation without text; other types stay excluded.
_SUPPORTED_LOCAL_TYPES = {
    TEXT_LOCAL_TYPE: _TEXT_TYPE,
    STICKER_LOCAL_TYPE: _STICKER_TYPE,
}

_XML_UNSAFE_RE = re.compile(r"<!DOCTYPE|<!ENTITY", re.IGNORECASE)
_XML_PARSE_MAX_LEN = 20000
_CONTENT_TYPE_COMPRESSED = 4
_WECHAT_EMOJI_TOKEN_RE = re.compile(r"\[([^\[\]]+)\]")
_LOGGER = logging.getLogger(__name__)


def messages_from_payload(payload: Mapping[str, Any] | None) -> list[Any]:
    """Apply the export conversation context to already-loaded rows."""
    if payload is None or not is_wechat_db_payload(payload):
        return []

    messages = payload.get("messages")
    if not isinstance(messages, list):
        return []

    conversation_id = _conversation_id(payload)
    if conversation_id is None:
        return messages

    conversation_type = _conversation_type_from_payload(payload)
    self_username = _self_username(payload)
    rows: list[Any] = []
    for row in messages:
        if isinstance(row, Mapping) and "username" not in row:
            merged = dict(row)
            merged["username"] = conversation_id
            row = merged
        if isinstance(row, Mapping):
            if conversation_type and "conversation_type" not in row:
                row = dict(row)
                row["conversation_type"] = conversation_type
            if self_username and "self_username" not in row:
                row = dict(row)
                row["self_username"] = self_username
        rows.append(row)
    return rows


def parse_messages(raw_messages: Iterable[Any]) -> list[ChatMessage]:
    """Project supported database rows for existing analysis callers."""
    return project_legacy_messages(parse_rich_messages(raw_messages))


def parse_rich_messages(raw_messages: Iterable[Any]) -> list[RichMessage]:
    """Normalize supported database rows into source-neutral facts."""
    parsed_messages: list[RichMessage] = []
    pat_candidates = 0
    pat_accepted = 0

    try:
        iterator = iter(raw_messages)
    except TypeError:
        return parsed_messages

    for raw_message in iterator:
        if isinstance(raw_message, Mapping) and raw_message.get("local_type") == PAT_LOCAL_TYPE:
            pat_candidates += 1
        parsed_message = parse_rich_message(raw_message)
        if parsed_message is not None:
            parsed_messages.append(parsed_message)
            pat_accepted += parsed_message.message_type == "nudge"

    _LOGGER.info("wechat.pats.parse candidates=%d accepted=%d rejected=%d",
                 pat_candidates, pat_accepted, pat_candidates - pat_accepted)

    return parsed_messages


def parse_message(raw_message: Any) -> ChatMessage | None:
    """Project one database row for callers of the legacy adapter API."""
    rich_message = parse_rich_message(raw_message)
    if rich_message is None:
        return None
    return project_legacy_message(rich_message)


def parse_rich_message(raw_message: Any) -> RichMessage | None:
    """Convert one database row into a RichMessage, or ``None`` if unusable."""
    if not isinstance(raw_message, Mapping):
        return None

    local_type = raw_message.get("local_type")
    message_type = _normalize_local_type(local_type)
    if message_type is None:
        return None

    timestamp = raw_message.get("create_time")
    if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float, str)):
        return None

    message_content = raw_message.get("message_content")
    content_type = raw_message.get("WCDB_CT_message_content")
    if content_type is None:
        content_type = raw_message.get("content_type")
    decoded_content = _decode_message_content(message_content, content_type)

    sender_id = _clean_string(raw_message.get("user_name"))
    if sender_id is None:
        return None
    sender = _clean_string(raw_message.get("sender_name")) or sender_id
    self_username = _clean_string(raw_message.get("self_username"))
    is_self = None
    if self_username is not None:
        is_self = sender_id == self_username

    message_id = (
        _stringify_id(raw_message.get("server_id"))
        or _stringify_id(raw_message.get("local_id"))
    )
    conversation_type = _conversation_type(raw_message)
    relations = ()
    if message_type == "nudge":
        relation = _parse_private_pat(raw_message, decoded_content, sender_id)
        if relation is None or to_utc_datetime(timestamp) is None:
            return None
        message_id = str(raw_message["server_id"])
        contents = ()
        relations = (relation,)
    elif message_type == _TEXT_TYPE:
        if conversation_type == "group" and is_self is not True:
            decoded_content = _strip_sender_prefix(decoded_content, sender_id)
        if decoded_content is None:
            return None
        contents = _text_expression_contents(decoded_content)
    else:
        contents = (
            _sticker_expression(
                _strip_sender_prefix(decoded_content, sender_id),
                message_id=message_id,
            ),
        )

    return RichMessage(
        message_id=message_id,
        source=WECHAT_PLATFORM,
        conversation_id=_clean_string(raw_message.get("username")),
        conversation_type=conversation_type,
        sender=SenderIdentity(
            identity_id=sender_id,
            display_name=sender,
            remark=_clean_string(raw_message.get("sender_remark")),
            nickname=_clean_string(raw_message.get("sender_nickname")),
        ),
        is_self=is_self,
        timestamp=timestamp,
        message_type=message_type,
        contents=contents,
        source_type=local_type,
        relations=relations,
        is_system=message_type == "nudge",
    )


def _normalize_local_type(local_type: Any) -> str | None:
    if isinstance(local_type, bool) or not isinstance(local_type, int):
        return None
    if local_type == PAT_LOCAL_TYPE:
        return "nudge"
    # The provider selects the low 32-bit message type and exports the raw
    # value. Keep the same interpretation when high bits carry source flags.
    return _SUPPORTED_LOCAL_TYPES.get(local_type & 0xFFFFFFFF)


def _parse_private_pat(
    row: Mapping[str, Any], xml: str | None, sender_id: str,
) -> NudgeRelation | None:
    """Accept only the verified private pat shape, never infer from titles."""
    peer = _clean_string(row.get("username"))
    owner = _clean_string(row.get("self_username"))
    server_id = row.get("server_id")
    if (not peer or not owner or peer == owner or peer.endswith("@chatroom")
            or _conversation_type(row) != "private"
            or isinstance(server_id, bool)
            or not isinstance(server_id, (int, str))
            or not re.fullmatch(r"[1-9][0-9]*", str(server_id))
            or not xml or len(xml) > _XML_PARSE_MAX_LEN or _XML_UNSAFE_RE.search(xml)):
        return None
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        return None
    apps = root.findall("appmsg")
    if root.tag != "msg" or len(apps) != 1:
        return None
    app = apps[0]
    if len(app.findall("type")) != 1 or app.findtext("type") != "62":
        return None
    pats = app.findall("patinfo")
    if len(pats) != 1:
        return None
    pat = pats[0]
    values = {}
    for tag in ("fromusername", "pattedusername", "patsuffix"):
        elements = pat.findall(tag)
        if len(elements) > 1 or (tag != "patsuffix" and len(elements) != 1):
            return None
        if elements and len(elements[0]):
            return None
        values[tag] = pat.findtext(tag) or ""
    actor = values["fromusername"].strip()
    target = values["pattedusername"].strip()
    if actor != sender_id or (actor, target) not in ((owner, peer), (peer, owner)):
        return None
    # chatusername is the sender's view of the conversation, not the local peer.
    return NudgeRelation(target_identity_id=target, suffix=values["patsuffix"].strip())


def _clean_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _stringify_id(value: Any) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, str)):
        text = str(value).strip()
        return text or None
    return None


def _text_expression_contents(text: str) -> tuple[RichContent, ...]:
    """Keep the legacy text projection and expose official bracket emojis."""
    return (
        TextContent(text=text),
        *_extract_official_text_expressions(text),
    )


def _extract_official_text_expressions(text: str) -> tuple[ExpressionContent, ...]:
    """Extract known official WeChat emoji tokens without removing their text.

    English bracket aliases normalize to the canonical official name, so
    ``[Facepalm]`` and ``[捂脸]`` count as the same expression while the
    original bracket text is kept for display.
    """
    matches: list[tuple[re.Match[str], str]] = []
    for match in _WECHAT_EMOJI_TOKEN_RE.finditer(text):
        canonical = canonical_wechat_emoji_name(match.group(1).strip())
        if canonical is not None:
            matches.append((match, canonical))

    expressions: list[ExpressionContent] = []
    for position, (match, canonical) in enumerate(matches):
        previous_end = matches[position - 1][0].end() if position > 0 else 0
        next_start = (
            matches[position + 1][0].start()
            if position + 1 < len(matches)
            else len(text)
        )
        expressions.append(
            ExpressionContent(
                expression_kind=EXPRESSION_KIND_PLATFORM_FACE,
                expression_key=canonical,
                display_text=match.group(0),
                source=WECHAT_PLATFORM,
                position=position,
                text_before=text[previous_end : match.start()][-40:],
                text_after=text[match.end() : next_start][:40],
            )
        )
    return tuple(expressions)


def _decode_message_content(value: Any, content_type: Any) -> str | None:
    """Return decoded message text, handling WCDB zstd-compressed blobs."""
    if value is None:
        return None
    if isinstance(value, str):
        if (
            content_type == _CONTENT_TYPE_COMPRESSED
            and value.strip()
        ):
            try:
                raw = bytes.fromhex(value)
            except ValueError:
                return value
            return _decompress_text(raw)
        return value
    if isinstance(value, bytes):
        if content_type == _CONTENT_TYPE_COMPRESSED:
            return _decompress_text(value)
        return value.decode("utf-8", errors="replace")
    return None


def _decompress_text(raw: bytes) -> str | None:
    try:
        return zstd.ZstdDecompressor().decompress(raw).decode(
            "utf-8",
            errors="replace",
        )
    except (zstd.ZstdError, ValueError):
        return None


def _strip_sender_prefix(content: str | None, sender_id: str) -> str | None:
    """Remove the confirmed group sender header ahead of a message payload.

    WeChat 4.x stores another member's group content as ``<username>:\\n``
    followed by the message payload, while the local user's own rows carry no
    such header. Only the already confirmed sender's exact header is removed;
    every other shape is returned unchanged so the existing fallback holds.
    """
    if not content or content.startswith("<"):
        return content
    prefix = f"{sender_id}:\n"
    if content.startswith(prefix):
        return content[len(prefix):]
    return content


def _sticker_expression(
    content: str | None,
    *,
    message_id: str | None,
) -> ExpressionContent:
    """Build a source-neutral sticker from WeChat type-47 XML."""
    fallback_key = message_id or "unknown"
    md5 = None
    unambiguous = False
    emoji = None
    if (
        content
        and len(content) <= _XML_PARSE_MAX_LEN
        and not _XML_UNSAFE_RE.search(content)
    ):
        try:
            root = ElementTree.fromstring(content)
        except ElementTree.ParseError:
            root = None
        if root is not None:
            emoji = root.find(".//emoji")
            if emoji is not None:
                md5 = _clean_string(emoji.get("md5"))
                unambiguous = len(root.findall(".//emoji")) == 1

    expression_key = md5 or fallback_key
    # Preserve the legacy expression key/counting contract. Only a validated
    # digest may identify a material in the new data channel; fallback IDs and
    # arbitrary XML strings never authorize deduplication or resource matching.
    # Some Type-47 rows only retain androidmd5. It describes the
    # same content digest, so they can reuse an already verified original.
    # A malformed primary attribute must not be hidden by an alternate value.
    content_md5 = md5 or (_clean_string(emoji.get("androidmd5")) if emoji is not None else None)
    reliable_md5 = (
        content_md5.lower()
        if unambiguous and content_md5 and re.fullmatch(r"[0-9a-fA-F]{32}", content_md5)
        else None
    )
    return ExpressionContent(
        expression_kind=EXPRESSION_KIND_STICKER,
        expression_key=expression_key,
        display_text="[贴图]",
        source=WECHAT_PLATFORM,
        position=0,
        sticker=StickerIdentity(
            classification_basis="wechat_type_47",
            identity_basis="wechat_xml_md5" if reliable_md5 else "unresolved",
            identity_key=f"wechat:md5:{reliable_md5}" if reliable_md5 else None,
            content_md5=reliable_md5,
            # Only a digest-verified occurrence may carry an acquisition hint.
            recovery_url=(
                _clean_string(emoji.get("cdnurl")) if reliable_md5 and emoji is not None else None
            ),
        ),
    )


def _conversation_id(payload: Mapping[str, Any]) -> str | None:
    conversation = payload.get("conversation")
    if not isinstance(conversation, Mapping):
        return None
    return _clean_string(conversation.get("username"))


def _conversation_type_from_payload(payload: Mapping[str, Any]) -> str:
    conversation = payload.get("conversation")
    if not isinstance(conversation, Mapping):
        return "unknown"
    session_type = conversation.get("session_type")
    if isinstance(session_type, str):
        normalized = _normalize_conversation_type(session_type)
        if normalized != "unknown":
            return normalized
    return "unknown"


def _self_username(payload: Mapping[str, Any]) -> str | None:
    conversation = payload.get("conversation")
    if not isinstance(conversation, Mapping):
        return None
    return _clean_string(conversation.get("self_username"))


def _conversation_type(raw_message: Mapping[str, Any]) -> str:
    explicit = raw_message.get("conversation_type")
    if isinstance(explicit, str):
        normalized = _normalize_conversation_type(explicit)
        if normalized != "unknown":
            return normalized

    username = _clean_string(raw_message.get("username"))
    if not username:
        return "unknown"
    if username.endswith("@chatroom"):
        return "group"
    return "private"


def _normalize_conversation_type(raw_type: str) -> str:
    normalized = raw_type.strip().lower()
    if normalized in {"private", "group"}:
        return normalized
    return "unknown"


def is_wechat_db_payload(payload: Mapping[str, Any]) -> bool:
    if payload.get("source") != SOURCE_MARKER:
        return False
    return isinstance(payload.get("messages"), list)
