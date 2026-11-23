"""Client tool declarations, names and Excel transport instructions."""

from __future__ import annotations

import json

EXTERNAL_CLIENT_INSTRUCTIONS = (
    "This request comes from an external Responses client. Return assistant text. "
    "Do not call Excel, Office, workbook or connector tools."
)
TOOL_CALL_MARKER_OPEN = "<codex_tool_call>"
TOOL_CALL_MARKER_CLOSE = "</codex_tool_call>"
# Kept only to replay calls produced by proxy versions that used the old text
# marker protocol. New calls travel through Basispoints' declared
# ``run_officejs`` function and are intercepted before any Office code runs.
CLIENT_TOOL_RELAY_PREFIX = "codex_client__"
CLIENT_MARKER_CALL_ID_PREFIX = "call_ghcp_excel_marker_"
NATIVE_FALLBACK_CALL_ID_PREFIX = "call_ghcp_excel_native_"
CLIENT_TOOL_TRANSPORT_NAME = "run_officejs"
CLIENT_TOOL_TRANSPORT_ALIASES = frozenset(
    {CLIENT_TOOL_TRANSPORT_NAME, f"functions.{CLIENT_TOOL_TRANSPORT_NAME}"}
)

_RAW_TRANSPORT_GUIDANCE = (
    "For FUNCTION tools, serialize one JSON object with name and object arguments into code. "
    "For CUSTOM tools, use summary=codex2api.custom/CATALOG_NAME and put the exact raw input in code. "
)
_TRANSPORT_RETRY_GUIDANCE = (
    "The previous run_officejs relay was rejected because its transport envelope was malformed. "
    "Retry once with exactly one outer run_officejs call. "
    + _RAW_TRANSPORT_GUIDANCE
    + "Do not put another run_officejs wrapper inside it. Preserve the intended tool input exactly "
    "and do not repeat the identical malformed payload."
)

_RAW_TRANSPORT_PREFIX = "excel-proxy.raw/"
_CUSTOM_TRANSPORT_PREFIX = "codex2api.custom/"

_STRIPPED_HOSTED_TOOLS = frozenset({
    "image_generation", "web_search", "web_search_preview",
    "web_search_preview_2025_03_11", "web_search_2025_08_26",
    "code_interpreter", "file_search", "computer_use_preview", "mcp",
    "web_search_2025_08_26_preview",
})
_UNSUPPORTED_HOSTED_TOOLS = (_STRIPPED_HOSTED_TOOLS - {
    "web_search_2025_08_26_preview",
}) | {"tool_search", "computer"}


def _sanitize_hosted_tools(source: dict) -> dict:
    tools = source.get("tools")
    if not isinstance(tools, list):
        return source
    result = dict(source)
    kept = []
    removed = set()
    for tool in tools:
        kind = tool.get("type") if isinstance(tool, dict) else None
        kind = kind.strip().lower() if isinstance(kind, str) else ""
        if kind in _STRIPPED_HOSTED_TOOLS:
            removed.add(kind)
        else:
            kept.append(tool)
    if removed:
        if kept:
            result["tools"] = kept
        else:
            result.pop("tools", None)
    choice = result.get("tool_choice")
    if isinstance(choice, dict):
        for field in ("name", "type"):
            value = choice.get(field)
            value = value.strip().lower() if isinstance(value, str) else ""
            if (
                value in removed
                or value in {
                    "image_generation", "web_search", "web_search_preview",
                    "code_interpreter", "file_search", "computer_use_preview",
                }
                or "web_search" in value
                or "image_generation" in value
            ):
                result["tool_choice"] = "auto"
                break
    return result


def _quoted(value) -> str:
    """Match Go's compact JSON encoding for protocol text and cache identity."""
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    for character, escaped in (
        ("<", "\\u003c"), (">", "\\u003e"), ("&", "\\u0026"),
        ("\u2028", "\\u2028"), ("\u2029", "\\u2029"),
    ):
        encoded = encoded.replace(character, escaped)
    return encoded


def _client_tool_key(name: str, namespace: str | None = None) -> str:
    return f"{namespace}.{name}" if namespace else name


def _tool_call_name(item: dict) -> str | None:
    name, namespace = item.get("name"), item.get("namespace")
    if not isinstance(name, str):
        return None
    if namespace in (None, ""):
        return name
    if not isinstance(namespace, str):
        return None
    return (
        name if name.startswith(namespace + ".") else _client_tool_key(name, namespace)
    )


def _iter_client_tools(tools: object, namespace: str | None = None):
    """Yield callable leaves from Codex dynamic tool namespaces."""
    if not isinstance(tools, list):
        return
    for tool in tools:
        if not isinstance(tool, dict):
            raise ValueError("Invalid Excel client tool declaration.")
        tool_type = tool.get("type")
        name = tool.get("name")
        if tool_type == "namespace":
            if not isinstance(name, str) or not name:
                raise ValueError("Excel client namespaces require a name.")
            yield from _iter_client_tools(
                tool.get("tools"), _client_tool_key(name, namespace)
            )
            continue
        if isinstance(tool_type, str) and tool_type in _UNSUPPORTED_HOSTED_TOOLS:
            continue
        if tool_type not in ("function", "custom"):
            raise ValueError("Excel requires client function or custom tools.")
        if not isinstance(name, str) or not name:
            raise ValueError("Excel client tools require a name.")
        yield (_client_tool_key(name, namespace), name, namespace, tool_type, tool)


def client_tool_types(source: dict) -> dict[str, str]:
    if str(source.get("tool_choice") or "").strip().lower() == "none":
        return {}
    return {key: info["type"] for key, info in _client_tool_specs(source).items()}


def relay_tool_name(name: str) -> str:
    """Return the legacy non-colliding marker name used before run_officejs."""
    return CLIENT_TOOL_RELAY_PREFIX + name


def _original_client_tool_name(
    name: object,
    allowed_tools: dict[str, str],
) -> str | None:
    if not isinstance(name, str):
        return None
    if name.startswith(CLIENT_TOOL_RELAY_PREFIX):
        candidate = name[len(CLIENT_TOOL_RELAY_PREFIX) :]
        return candidate if candidate in allowed_tools else None
    # Prefer an exact catalog entry over the native host's display prefix.
    if name in allowed_tools:
        return name
    if name.startswith("functions."):
        candidate = name[len("functions.") :]
        return candidate if candidate in allowed_tools else None
    return None


def _client_tool_specs(source: dict) -> dict[str, dict]:
    source = _sanitize_hosted_tools(source)
    specs = _tool_batch_specs(source.get("tools"))
    history = source.get("input")
    if isinstance(history, list):
        for item in history:
            if isinstance(item, dict) and item.get("type") == "additional_tools":
                for key, info in _additional_tool_specs(item).items():
                    previous = specs.get(key)
                    if previous and (previous["name"], previous["namespace"]) != (
                        info["name"], info["namespace"],
                    ):
                        raise ValueError("Ambiguous Excel client tool namespace.")
                    specs[key] = info
    return specs


def _additional_tool_specs(item: dict) -> dict[str, dict]:
    if (
        item.keys() - {"type", "id", "tools", "internal_chat_message_metadata_passthrough"}
        or not isinstance(item.get("tools"), list)
    ):
        raise ValueError("additional_tools requires a tool declaration list.")
    return _tool_batch_specs(item["tools"])


def _tool_batch_specs(tools) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for key, name, namespace, tool_type, tool in _iter_client_tools(tools):
        if key in result:
            previous = result[key]
            same_identity = (previous["name"], previous["namespace"]) == (
                name,
                namespace,
            )
            same_contract = _tool_definition_signature(
                previous["spec"]
            ) == _tool_definition_signature(tool)
            if not same_identity or not same_contract:
                raise ValueError(
                    "Excel client tools contain conflicting duplicate declarations."
                )
            continue
        result[key] = {
            "key": key,
            "name": name,
            "namespace": namespace,
            "type": tool_type,
            "spec": tool,
        }
    return result


def _tool_definition_signature(spec: dict) -> str:
    return _quoted({
        "type": spec.get("type"),
        "parameters": (
            _tool_input_schema(spec)
            if spec.get("type") == "function" else spec.get("parameters")
        ),
        "format": spec.get("format"),
        "strict": spec.get("strict"),
    })


def _tool_input_schema(spec: dict):
    for key in ("parameters", "inputSchema", "input_schema"):
        if key in spec and spec[key] is not None:
            return spec[key]
    return None


def _raw_transport_fields(tool_info: dict) -> list[str]:
    if tool_info["type"] == "custom":
        return ["input"]
    schema = _tool_input_schema(tool_info["spec"])
    properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
    if not isinstance(properties, dict):
        return []
    fields = ["code"]
    if tool_info["name"].rsplit(".", 1)[-1] == "exec_command":
        fields.append("cmd")
    return [
        field
        for field in fields
        if isinstance(properties.get(field), dict)
        and properties[field].get("type") == "string"
    ]


def _is_transport_name(name: object) -> bool:
    return isinstance(name, str) and name in CLIENT_TOOL_TRANSPORT_ALIASES


def _describe_schema(value, depth: int = 0) -> str:
    if not isinstance(value, dict) or depth >= 8:
        return (
            "Use the arguments described by the tool."
            if value is None else _quoted(value)
        )
    parts = []
    if value.get("type") is not None:
        parts.append("Value type: " + _quoted(value["type"]) + ".")
    description = value.get("description")
    if isinstance(description, str) and description:
        parts.append(description)
    required = value.get("required")
    required = (
        {name if isinstance(name, str) else "" for name in required}
        if isinstance(required, list) else set()
    )
    properties = value.get("properties")
    if isinstance(properties, dict):
        for name in sorted(properties):
            presence = "required" if name in required else "optional"
            parts.append(
                f"Field {_quoted(name)} ({presence}): "
                + _describe_schema(properties[name], depth + 1)
            )
    if value.get("items") is not None:
        parts.append("Each array item: " + _describe_schema(value["items"], depth + 1))
    constraints = {
        key: item for key, item in value.items()
        if key not in {"type", "description", "properties", "items"}
    }
    if constraints:
        parts.append("Additional constraints: " + _quoted(constraints) + ".")
    return " ".join(parts) if parts else "Any JSON value."


def _describe_catalog(specs: dict[str, dict]) -> str:
    lines = []
    for key, info in specs.items():
        tool = info["spec"]
        line = f'Client tool {_quoted(key)} ({info["type"]}).'
        description = tool.get("description")
        if isinstance(description, str) and description:
            line += " " + description
        if info["type"] == "custom":
            line += (
                " Set run_officejs summary to " + _quoted(_CUSTOM_TRANSPORT_PREFIX + key)
                + " and pass its exact raw text directly in code."
            )
            if tool.get("format") is not None:
                line += " Input format: " + _quoted(tool["format"]) + "."
        else:
            line += (
                " Pass a JSON object in the envelope's arguments field. Argument contract: "
                + _describe_schema(_tool_input_schema(tool))
            )
        lines.append(line)
    return "\n\n".join(lines)


def _unsupported_hosted_tools(tools) -> set[str]:
    result = set()
    if isinstance(tools, list):
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            kind = tool.get("type")
            if kind == "namespace":
                result.update(_unsupported_hosted_tools(tool.get("tools")))
            elif isinstance(kind, str) and kind in _UNSUPPORTED_HOSTED_TOOLS:
                result.add(kind)
    return result


def _client_tool_protocol_instructions(source: dict) -> str:
    if source.get("tool_choice") == "none":
        return EXTERNAL_CLIENT_INSTRUCTIONS
    source = _sanitize_hosted_tools(source)
    specs = _client_tool_specs(source)
    protocol = EXTERNAL_CLIENT_INSTRUCTIONS
    if specs:
        protocol = (
            "This request comes from an external Responses client. Use only the client tools in the catalog below. "
            "There is no live Excel workbook for this request. The proxy intercepts run_officejs as a transport and never executes Office code. "
            "To call one client tool, call native run_officejs using the transport matching its catalog type. "
            'FUNCTION: code must contain one serialized JSON object {"name":"CATALOG_NAME","arguments":{...}}. Arguments is an object, not an extra JSON string. '
            "CUSTOM: set summary to exactly codex2api.custom/CATALOG_NAME and put the exact raw tool input directly in code. Do not wrap custom input in another JSON object or add Markdown fences. "
            "For example, custom functions.exec uses summary=codex2api.custom/functions.exec and code containing its raw JavaScript; custom functions.apply_patch uses its exact patch text. The marker is mandatory for raw input. "
            "CATALOG_NAME includes its exact namespace. Outer arguments also include extended_summary, destructive=false and references=[]. For FUNCTION transport, use an ordinary descriptive summary. "
            "Never nest run_officejs inside code. Serialize outer native arguments with proper JSON escaping. For FUNCTION envelopes also escape all quotes, backslashes, newline, carriage return and tab characters within JSON string values. "
            "Call one client tool at a time, including update_plan through this transport. After receiving its result continue the task; do not repeat completed calls. "
            "Tool results replayed under run_officejs are the named client tool's results. When a tool is needed, emit its call in this response instead of only announcing it. "
            "Do not call other native tools or claim that shell, filesystem or workspace access is unavailable when a suitable catalog tool exists. "
            "If no tool is needed, answer as assistant text. Client tool catalog:\n"
            + _describe_catalog(specs)
            + "\nEnd of catalog. Invoke native run_officejs once. FUNCTION uses a JSON envelope in code. CUSTOM uses the exact codex2api.custom/CATALOG_NAME summary marker and raw input in code. No Office code is executed by the proxy."
        )
    unsupported = _unsupported_hosted_tools(source.get("tools"))
    if isinstance(source.get("input"), list):
        for item in source["input"]:
            if isinstance(item, dict) and item.get("type") == "additional_tools":
                unsupported.update(_unsupported_hosted_tools(item.get("tools")))
    if unsupported:
        protocol += (
            "\nHosted tools unavailable through Basispoints: " + ", ".join(sorted(unsupported))
            + ". These declarations were omitted. Do not claim to have used them. If the task requires one, explain the limitation or use a suitable declared client tool."
        )
    return protocol


def _client_tool_protocol_reminder(source: dict) -> str:
    """Retained for compatibility; the protocol is now one developer message."""
    return ""
