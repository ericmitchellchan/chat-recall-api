"""Text extraction from ChatGPT message content types.

Handles all 12 known ChatGPT content types, extracting searchable
plain text from the structured content dict.
"""

from typing import Any


def extract_text(content: dict[str, Any] | None) -> str:
    """Extract searchable plain text from a message content dict."""
    if not content:
        return ""
    content_type = content.get("content_type", "")
    extractor = _EXTRACTORS.get(content_type, _extract_unknown)
    return extractor(content)


def _extract_text(content: dict) -> str:
    parts = content.get("parts")
    if not parts:
        return ""
    return "\n".join(p for p in parts if isinstance(p, str))


def _extract_code(content: dict) -> str:
    text = content.get("text", "")
    lang = content.get("language", "")
    if lang and text:
        return f"```{lang}\n{text}\n```"
    return text or ""


def _extract_multimodal_text(content: dict) -> str:
    parts = content.get("parts")
    if not parts:
        return ""
    text_parts = []
    for part in parts:
        if isinstance(part, str):
            text_parts.append(part)
        elif isinstance(part, dict):
            if part.get("content_type") == "image_asset_pointer":
                continue
            inner = part.get("text", "")
            if inner:
                text_parts.append(inner)
    return "\n".join(text_parts)


def _extract_reasoning_recap(content: dict) -> str:
    for key in ("recap", "content", "text", "summary"):
        val = content.get(key)
        if isinstance(val, str) and val:
            return val
    return ""


def _extract_thoughts(content: dict) -> str:
    text = content.get("text")
    if isinstance(text, str) and text:
        return text
    thoughts = content.get("thoughts")
    if isinstance(thoughts, list):
        parts = []
        for t in thoughts:
            if isinstance(t, dict):
                c = t.get("content", "")
                if c:
                    parts.append(c)
            elif isinstance(t, str):
                parts.append(t)
        return "\n".join(parts)
    return ""


def _extract_unknown(content: dict) -> str:
    for key in ("text", "parts", "result", "output", "message"):
        val = content.get(key)
        if isinstance(val, str) and val:
            return val
        if isinstance(val, list):
            text_parts = [p for p in val if isinstance(p, str)]
            if text_parts:
                return "\n".join(text_parts)
    return ""


_EXTRACTORS: dict[str, Any] = {
    "text": _extract_text,
    "code": _extract_code,
    "multimodal_text": _extract_multimodal_text,
    "reasoning_recap": _extract_reasoning_recap,
    "thoughts": _extract_thoughts,
    "computer_output": lambda c: c.get("text", "") or c.get("output", "") or "",
    "execution_output": lambda c: c.get("text", "") or c.get("output", "") or "",
    "system_error": lambda c: c.get("text", "") or c.get("message", "") or "",
    "tether_browsing_display": lambda c: c.get("result", "") or c.get("text", "") or c.get("summary", "") or "",
    "sonic_webpage": lambda c: c.get("text", "") or c.get("url", "") or "",
    "tether_quote": lambda c: " ".join(
        p for p in [
            f"[{c.get('title', '')}]" if c.get("title") else "",
            c.get("text", "") or c.get("quote", ""),
            f"({c.get('url', '')})" if c.get("url") else "",
        ] if p
    ),
    "user_editable_context": lambda c: "\n".join(
        c.get(k, "") for k in ("user_profile", "user_instructions", "text", "user_context")
        if c.get(k)
    ),
}
