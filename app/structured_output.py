"""Prompt and validate JSON answers for Excel's text-only output contract."""

import json
import re

from jsonschema import Draft202012Validator, exceptions, validators

import tool_schema
from upstream_errors import ExcelResponseError


def request_format(body):
    text = body.get("text")
    if text is None:
        return None
    if not isinstance(text, dict):
        raise ValueError("text must be an object.")
    fmt = text.get("format")
    if fmt is None:
        return None
    if not isinstance(fmt, dict):
        raise ValueError("text.format must be an object.")
    kind = fmt.get("type")
    if kind in (None, "text"):
        return None
    allowed = (
        {"type", "name", "schema", "strict", "description"}
        if kind == "json_schema"
        else {"type"}
    )
    if fmt.keys() - allowed:
        raise ValueError("text.format contains unsupported fields.")
    if kind == "json_object":
        return fmt
    if kind != "json_schema":
        raise ValueError("text.format.type must be text, json_object or json_schema.")
    if not isinstance(fmt.get("name"), str) or not re.fullmatch(
        r"[A-Za-z0-9_-]{1,64}", fmt["name"]
    ):
        raise ValueError(
            "text.format.name must contain 1-64 letters, digits, underscores or hyphens."
        )
    if fmt.get("strict") is not None and not isinstance(fmt["strict"], bool):
        raise ValueError("text.format.strict must be a boolean.")
    if fmt.get("description") is not None and not isinstance(fmt["description"], str):
        raise ValueError("text.format.description must be a string.")
    schema = fmt.get("schema")
    try:
        if (
            not isinstance(schema, dict)
            or len(json.dumps(schema, allow_nan=False).encode())
            > tool_schema.MAX_SCHEMA_BYTES
        ):
            raise ValueError()
        validators.validator_for(schema, default=Draft202012Validator).check_schema(
            schema
        )
        pending = [schema]
        while pending:
            value = pending.pop()
            if isinstance(value, dict):
                for key, child in value.items():
                    if (
                        key in ("$ref", "$dynamicRef")
                        and isinstance(child, str)
                        and not child.startswith("#")
                    ):
                        raise ValueError()
                    pending.append(child)
            elif isinstance(value, list):
                pending.extend(value)
    except (exceptions.SchemaError, TypeError, ValueError, RecursionError):
        raise ValueError(
            "text.format.schema must be a valid JSON schema of at most 1 MiB with local references only."
        ) from None
    return fmt


def instructions(fmt):
    return (
        "The client requires a structured final answer. Return exactly one JSON object, "
        "without Markdown fences or surrounding prose, matching this output format. "
        "The proxy validates the final JSON before delivering it. Tool calls and explicit "
        "refusals remain separate protocol items; use client tools as needed before the final answer.\n"
        + json.dumps(fmt, ensure_ascii=False, separators=(",", ":"))
    )


def validate_response(response, fmt):
    if fmt is None:
        return
    output = response.get("output", [])
    if any(
        item.get("type") in ("function_call", "custom_tool_call") for item in output
    ):
        return  # Tool continuations are not final answers.
    try:
        parts = [
            part
            for item in output
            if item.get("type") == "message"
            for part in item.get("content", [])
        ]
        if any(
            not isinstance(part, dict)
            or part.get("type") not in ("output_text", "refusal")
            or not isinstance(
                part.get("text" if part["type"] == "output_text" else "refusal"), str
            )
            for part in parts
        ):
            raise ValueError()
        text = "".join(
            part.get("text", "") for part in parts if part.get("type") == "output_text"
        )
        if not text and any(part.get("type") == "refusal" for part in parts):
            response.pop("output_text", None)
            return
        if len(text.encode("utf-8")) > 16 * 1024 * 1024:
            raise ValueError()
        value = json.loads(text)
        schema = fmt["schema"] if fmt["type"] == "json_schema" else {"type": "object"}
        if not tool_schema.arguments_match_schema(value, schema):
            raise ValueError()
        if "output_text" in response:
            response["output_text"] = text
    except (TypeError, ValueError, RecursionError):
        raise ExcelResponseError(
            "excel_invalid_structured_output",
            "Excel did not return valid JSON matching the requested output format.",
        ) from None


def is_message_event(kind, payload):
    item = payload.get("item")
    return kind.startswith(
        ("response.output_text.", "response.refusal.", "response.content_part.")
    ) or (
        kind.startswith("response.output_item.")
        and isinstance(item, dict)
        and item.get("type") == "message"
    )
