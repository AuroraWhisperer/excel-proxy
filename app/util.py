"""Pure stateless utility functions for ghcp_proxy."""

import gzip
import json
import zlib

from datetime import datetime, timezone
from fastapi import HTTPException, Request

try:
    import compression.zstd as _stdlib_zstd
except ImportError:
    _stdlib_zstd = None

try:
    import zstandard as _zstandard
except ImportError:
    _zstandard = None

try:
    import brotli
except ImportError:
    brotli = None


# ---------------------------------------------------------------------------
# JSON / coercion helpers
# ---------------------------------------------------------------------------


def zstd_compress(data: bytes) -> bytes:
    if _stdlib_zstd is not None:
        return _stdlib_zstd.compress(data)
    if _zstandard is not None:
        return _zstandard.ZstdCompressor().compress(data)
    raise RuntimeError("zstd support requires Python 3.14+ or the zstandard package")


def zstd_decompress(data: bytes) -> bytes:
    if _stdlib_zstd is not None:
        return _stdlib_zstd.decompress(data)
    if _zstandard is not None:
        return _zstandard.ZstdDecompressor().decompress(data)
    raise RuntimeError("zstd support requires Python 3.14+ or the zstandard package")


def _json_default(value):
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _coerce_float(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_int(value, default=0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Datetime helpers
# ---------------------------------------------------------------------------


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().isoformat()


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def _month_key(value: datetime) -> str:
    return value.strftime("%Y-%m")


def month_key_for_source_row(source: str, row: dict) -> str | None:
    raw_value = row.get("month")
    if not isinstance(raw_value, str):
        return None
    for fmt in ("%Y-%m", "%b %Y"):
        try:
            return datetime.strptime(raw_value, fmt).strftime("%Y-%m")
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Content extraction helpers
# ---------------------------------------------------------------------------


def extract_item_text(item) -> str:
    if not isinstance(item, dict):
        return ""

    content = item.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for entry in content:
            if not isinstance(entry, dict):
                continue
            if isinstance(entry.get("text"), str):
                parts.append(entry["text"])
            elif isinstance(entry.get("input_text"), str):
                parts.append(entry["input_text"])
        return "".join(parts)

    if isinstance(item.get("text"), str):
        return item["text"]
    if isinstance(item.get("input_text"), str):
        return item["input_text"]
    return ""


def _normalize_prompt_label(value, fallback: str) -> str:
    if isinstance(value, str):
        normalized = value.strip().replace("_", " ").replace("-", " ")
        if normalized:
            return normalized.upper()
    return fallback


def _extract_text_chunks(value) -> list[str]:
    if isinstance(value, str):
        normalized = value.strip()
        return [normalized] if normalized else []
    if isinstance(value, list):
        chunks: list[str] = []
        for item in value:
            chunks.extend(_extract_text_chunks(item))
        return chunks
    if not isinstance(value, dict):
        return []

    direct_chunks: list[str] = []
    for key in ("text", "input_text", "output_text"):
        text_value = value.get(key)
        if isinstance(text_value, str):
            normalized = text_value.strip()
            if normalized:
                direct_chunks.append(normalized)
    if direct_chunks:
        return direct_chunks

    nested_chunks: list[str] = []
    for key in ("content", "summary", "input", "output", "arguments"):
        nested_chunks.extend(_extract_text_chunks(value.get(key)))
    if nested_chunks:
        return nested_chunks

    for item in value.values():
        if isinstance(item, str):
            normalized = item.strip()
            if normalized:
                nested_chunks.append(normalized)
    return nested_chunks


def _render_prompt_section(label: str, text: str) -> str:
    normalized_text = str(text).strip()
    if not normalized_text:
        return ""
    return f"{label}:\n{normalized_text}"


def _extract_message_sections(messages) -> list[str]:
    if isinstance(messages, str):
        rendered = _render_prompt_section("USER", messages)
        return [rendered] if rendered else []
    if not isinstance(messages, list):
        return []

    sections: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role_label = _normalize_prompt_label(message.get("role"), "MESSAGE")
        text = "\n".join(_extract_text_chunks(message.get("content"))).strip()
        if not text:
            text = "\n".join(_extract_text_chunks(message)).strip()
        rendered = _render_prompt_section(role_label, text)
        if rendered:
            sections.append(rendered)
    return sections


def _extract_input_sections(input_value) -> list[str]:
    if isinstance(input_value, str):
        rendered = _render_prompt_section("USER", input_value)
        return [rendered] if rendered else []
    if not isinstance(input_value, list):
        return []

    sections: list[str] = []
    for item in input_value:
        if not isinstance(item, dict):
            continue

        item_type = str(item.get("type", "")).strip().lower()
        text = ""
        label = _normalize_prompt_label(
            item.get("role"), _normalize_prompt_label(item_type, "ITEM")
        )

        if item_type == "message" or item.get("role"):
            text = "\n".join(_extract_text_chunks(item.get("content"))).strip()
            label = _normalize_prompt_label(item.get("role"), "MESSAGE")
        elif item_type == "reasoning":
            text = "\n".join(
                _extract_text_chunks(item.get("summary") or item.get("content"))
            ).strip()
            label = "REASONING"
        elif item_type in {"custom_tool_call", "function_call", "tool_use"}:
            tool_name = str(item.get("name") or item.get("call_id") or "").strip()
            label = f"TOOL CALL {tool_name}".strip().upper() or "TOOL CALL"
            text = "\n".join(
                _extract_text_chunks(item.get("input") or item.get("arguments"))
            ).strip()
        elif item_type in {
            "custom_tool_call_output",
            "function_call_output",
            "computer_call_output",
            "tool_result",
        }:
            tool_name = str(item.get("name") or item.get("call_id") or "").strip()
            label = f"TOOL OUTPUT {tool_name}".strip().upper() or "TOOL OUTPUT"
            text = "\n".join(
                _extract_text_chunks(item.get("output") or item.get("content"))
            ).strip()
        else:
            text = "\n".join(
                _extract_text_chunks(item.get("content") if "content" in item else item)
            ).strip()

        rendered = _render_prompt_section(label, text)
        if rendered:
            sections.append(rendered)
    return sections


def extract_request_prompt_text(body: dict | None) -> str:
    if not isinstance(body, dict):
        return ""

    sections: list[str] = []
    for key, label in (
        ("instructions", "INSTRUCTIONS"),
        ("system", "SYSTEM"),
        ("developer", "DEVELOPER"),
        ("prompt", "PROMPT"),
    ):
        text = "\n".join(_extract_text_chunks(body.get(key))).strip()
        rendered = _render_prompt_section(label, text)
        if rendered:
            sections.append(rendered)

    sections.extend(_extract_input_sections(body.get("input")))
    sections.extend(_extract_message_sections(body.get("messages")))

    if not sections:
        text = "\n".join(_extract_text_chunks(body)).strip()
        rendered = _render_prompt_section("REQUEST", text)
        if rendered:
            sections.append(rendered)

    return "\n\n".join(section for section in sections if section).strip()


# ---------------------------------------------------------------------------
# Request body parsing
# ---------------------------------------------------------------------------


async def parse_json_request(request: Request, error_callback=None) -> dict:
    raw_body = await request.body()
    try:
        if not raw_body:
            return {}
        content_encoding = (
            str(request.headers.get("content-encoding", "")).strip().lower()
        )

        if content_encoding == "gzip":
            raw_body = gzip.decompress(raw_body)
        elif content_encoding == "deflate":
            raw_body = zlib.decompress(raw_body)
        elif content_encoding == "zstd":
            raw_body = zstd_decompress(raw_body)
        elif content_encoding == "br":
            if brotli is None:
                raise HTTPException(
                    status_code=400,
                    detail="Invalid JSON body: unsupported brotli request encoding",
                )
            raw_body = brotli.decompress(raw_body)
        elif raw_body.startswith(b"\x1f\x8b"):
            raw_body = gzip.decompress(raw_body)
        elif raw_body.startswith(b"\x28\xb5\x2f\xfd"):
            raw_body = zstd_decompress(raw_body)

        payload = json.loads(raw_body)
        if not isinstance(payload, dict):
            raise HTTPException(
                status_code=400, detail="Request body must be a JSON object"
            )
        return payload
    except HTTPException:
        raise
    except Exception:
        path = getattr(getattr(request, "url", None), "path", "?")
        content_type = str(request.headers.get("content-type", "")).strip()
        content_encoding = (
            str(request.headers.get("content-encoding", "")).strip().lower()
        )
        if error_callback is not None:
            error_callback(
                {
                    "at": utc_now_iso(),
                    "path": path,
                    "content_type": content_type,
                    "content_encoding": content_encoding,
                    "body_len": len(raw_body),
                }
            )
        print(
            f"WARN: Invalid JSON body path={path} content_type={content_type!r} "
            f"content_encoding={content_encoding!r} body_len={len(raw_body)}",
            flush=True,
        )
        raise HTTPException(status_code=400, detail="Invalid JSON body")


def extract_response_output_text(payload: dict) -> str | None:
    if not isinstance(payload, dict):
        return None

    output_text = payload.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text

    output = payload.get("output")
    if not isinstance(output, list):
        return None

    parts = []
    for item in output:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "message":
            continue
        if str(item.get("role", "")).lower() != "assistant":
            continue
        text = extract_item_text(item).strip()
        if text:
            parts.append(text)

    if not parts:
        return None
    return "\n\n".join(parts)
