"""Source-neutral display names for a conversation member."""

from __future__ import annotations


def first_identity_name(*values: str | None) -> str | None:
    """Return the first usable human name, ignoring empty and zero sentinels."""
    for value in values:
        if isinstance(value, str):
            name = value.strip()
            if name and name != "0":
                return name
    return None


def identity_search_names(*values: str | None) -> tuple[str, ...]:
    """Retain every supplied human name without using names as identity keys."""
    return tuple(dict.fromkeys(name for value in values if (name := first_identity_name(value))))


def resolve_member_names(
    *,
    remark: str | None,
    contextual_name: str | None,
    nickname: str | None,
    safe_display_fallback: str,
    conversation_kind: str = "unknown",
) -> tuple[str, str | None, str | None]:
    """Choose one primary name and optional context for GUI and reports."""
    if conversation_kind == "private":
        contextual = first_identity_name(nickname, contextual_name)
    else:
        contextual = first_identity_name(contextual_name, nickname)
    primary = (
        first_identity_name(remark, contextual, safe_display_fallback)
        or "\u672a\u77e5\u6210\u5458"
    )
    secondary = None
    if remark and contextual and contextual.casefold() != primary.casefold():
        secondary = contextual
    return primary, secondary, contextual
