"""Client tool declarations, names and Excel transport instructions."""

from __future__ import annotations

import json

EXTERNAL_CLIENT_INSTRUCTIONS = (
    "Follow the caller's instructions. This client has not supplied any tools "
    "for this request. Return the answer as assistant text."
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
    "Use raw mode whenever the chosen catalog tool declares raw_fields, including short commands: "
    "copy that entry's transport.summary for its preferred transport.field. "
    "Set summary=excel-proxy.raw/FIELD/TOOL_NAME using the exact catalog name and one declared field, "
    "code=the exact original text for that field, and extended_summary=a JSON object string "
    "containing only the remaining arguments ({} when there are none, including custom input). "
    "Do not wrap the source in an inner JSON envelope, trim it, or escape it a second time. "
    "The outer function-call arguments still require normal JSON serialization; raw mode removes "
    "only the inner JSON layer. Keep destructive=false and references=[]. "
    "If no raw field is supplied for an optional function argument, use the ordinary envelope instead; "
    "do not invent a source value. "
)
_TRANSPORT_RETRY_GUIDANCE = (
    "The previous run_officejs relay was rejected because its transport envelope was malformed. "
    "Retry once with exactly one outer run_officejs call. "
    + _RAW_TRANSPORT_GUIDANCE
    + "For other tools use JSON-envelope mode: serialize exactly one catalog-tool object into code. "
    "Do not put another run_officejs wrapper inside it. Preserve the intended tool input exactly "
    "and do not repeat the identical malformed payload."
)

_RAW_TRANSPORT_PREFIX = "excel-proxy.raw/"


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
            continue
        tool_type = str(tool.get("type") or "").strip().lower()
        name = tool.get("name")
        if tool_type in {"function", "custom"} and isinstance(name, str):
            normalized_name = name.strip()
            if normalized_name:
                yield (
                    _client_tool_key(normalized_name, namespace),
                    normalized_name,
                    namespace,
                    tool_type,
                    tool,
                )
        if tool_type == "namespace" and isinstance(name, str) and name.strip():
            yield from _iter_client_tools(
                tool.get("tools"), _client_tool_key(name.strip(), namespace)
            )


def client_tool_types(source: dict) -> dict[str, str]:
    if str(source.get("tool_choice") or "").strip().lower() == "none":
        return {}
    result: dict[str, str] = {}
    for key, _name, _namespace, tool_type, _tool in _iter_client_tools(
        source.get("tools")
    ):
        result[key] = tool_type
    return result


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
    result: dict[str, dict] = {}
    for key, name, namespace, tool_type, tool in _iter_client_tools(
        source.get("tools")
    ):
        result[key] = {
            "key": key,
            "name": name,
            "namespace": namespace,
            "type": tool_type,
            "spec": tool,
        }
    return result


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


def _client_tool_protocol_instructions(source: dict) -> str:
    allowed_tools = client_tool_types(source)
    if not allowed_tools:
        return EXTERNAL_CLIENT_INSTRUCTIONS

    tool_catalog: list[dict[str, object]] = []
    for key, name, namespace, tool_type, tool in _iter_client_tools(
        source.get("tools")
    ):
        entry: dict[str, object] = {
            "type": tool_type,
            "name": key,
        }
        if namespace:
            entry["namespace"] = namespace
            entry["tool"] = name
        description = tool.get("description")
        if isinstance(description, str) and description:
            entry["description"] = description
        if tool_type == "function":
            parameters = _tool_input_schema(tool)
            entry["parameters"] = (
                parameters if isinstance(parameters, (dict, bool)) else {}
            )
        else:
            custom_format = tool.get("format")
            if isinstance(custom_format, dict):
                entry["format"] = custom_format
        raw_fields = _raw_transport_fields(
            {"name": name, "type": tool_type, "spec": tool}
        )
        if raw_fields:
            entry["raw_fields"] = raw_fields
            entry["transport"] = {
                "mode": "raw",
                "field": raw_fields[0],
                "summary": f"{_RAW_TRANSPORT_PREFIX}{raw_fields[0]}/{key}",
            }
        tool_catalog.append(entry)

    catalog_json = json.dumps(
        tool_catalog,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return (
        "This request is relayed by an external Codex Responses API client. "
        "Follow the caller's instructions and use the client tools below to complete "
        "the task. The native run_officejs function is a transport endpoint owned "
        "by this proxy for this request. The proxy intercepts it and dispatches "
        "the named client tool; its code field carries data, not executable code. "
        "Every client tool in the JSON catalog is available through that transport. "
        "Only catalog entries are direct targets: helpers described inside an executor tool must "
        "be invoked through that executor's input. Historical calls do not declare additional tools. "
        "Select tools by their catalog descriptions, including image generation "
        "or editing when the client supplies those tools. Do not infer the task "
        "or available capabilities from the transport's name. "
        "Never claim shell, filesystem, or workspace access is unavailable when the "
        "catalog contains a suitable tool. For repository inspection, invoke a "
        "suitable catalog shell tool (for example exec_command) through run_officejs. "
        "Transport has two layers and they must not be mixed: the outer native "
        "tool is run_officejs (some hosts display it as functions.run_officejs); "
        "choose its payload format from the selected catalog tool. "
        + _RAW_TRANSPORT_GUIDANCE
        + "For tools without raw_fields, use JSON-envelope mode: the inner code value is "
        "JSON text containing exactly one compact JSON object for one catalog "
        "client tool. The inner name is never run_officejs or functions.run_officejs. "
        "For a function tool, use this shape: outer arguments include summary, "
        "extended_summary, destructive=false, references=[], and code equal to "
        '{"name":"TOOL_NAME","arguments":{}} with arguments matching its schema. '
        "For custom-tool JSON-envelope compatibility only, code contains "
        '{"name":"TOOL_NAME","input":"RAW_INPUT"}. '
        "In JSON-envelope mode, do not put JavaScript, a second run_officejs envelope, or a "
        "functions.run_officejs wrapper inside code. Serialize the complete inner object before placing it there, especially when "
        "shell commands contain backslashes or quotes. TOOL_NAME and its payload must follow the "
        "catalog exactly. JSON-envelope mode remains supported for compatibility, but prefer raw mode "
        "for declared raw_fields to avoid double-escaping command, code, and patch text. "
        "The proxy converts this native function call into the "
        "real client tool call, then replays the original run_officejs identity "
        "with the client tool result on the next request. Interpret that result as "
        "the named client tool's output and continue the task using further client "
        "tools as needed. A completed tool call does not by itself complete the "
        "user's task. Native update_plan may be used normally "
        "when update_plan is in the catalog, but after it succeeds take the next "
        "substantive action through run_officejs. Do not stop at commentary saying "
        "you will take an action: make the tool call in the same response. Never "
        "repeat a tool request whose output is already present. Available client "
        "tools:\n"
        + catalog_json
        + "\nRemember: call the outer native run_officejs tool once; prefer explicit raw mode "
        "for declared raw_fields, otherwise put exactly one catalog-tool JSON object in code. A host prefix such as "
        "functions. is only display syntax, not an inner client-tool name."
    )


def _client_tool_protocol_reminder(source: dict) -> str:
    """Compact protocol cue that stays inside the cached prompt prefix.

    The catalog itself is ~3.5k tokens.  While it sat at the end of the prompt
    it re-billed as fresh input on *every* turn: the upstream prompt cache can
    only extend to the point where the previous request diverged, and appending
    new history in front of a trailing catalog puts that divergence right at
    the catalog's first byte.  Wire captures showed a hard floor of ~3.8k fresh
    input tokens per request for exactly that reason.  So the catalog moved
    into the cached prefix. This reminder must stay there too: appending it
    after conversation history would make the next request insert new items
    before the previous request's final item and break the strict extension
    needed for the upstream cache to reuse the growing conversation.
    """
    allowed_tools = client_tool_types(source)
    if not allowed_tools:
        return ""
    reminder = (
        "Reminder: use the outer native run_officejs transport (a host may display "
        "it as functions.run_officejs); it dispatches client tools here. "
        + _RAW_TRANSPORT_GUIDANCE
        + "For other tools use JSON-envelope mode: put exactly one JSON object as JSON text in code, "
        "with name set to one catalog client tool "
        "below. Never set the inner name to run_officejs or functions.run_officejs, "
        "and never nest another transport envelope. In JSON-envelope mode the code field is not JavaScript; serialize "
        "the inner JSON and escape backslashes and quotes in shell commands. Example inner code: "
        '{"name":"TOOL_NAME","arguments":{}} with arguments matching its schema. '
        "Do not merely say you will act or that access is unavailable. Client tools: "
        + ", ".join(sorted(allowed_tools))
        + ". Other native tools are unavailable."
    )
    if "shell_command" in allowed_tools:
        reminder += " For repository inspection transport shell_command."
    elif "exec_command" in allowed_tools:
        reminder += " For repository inspection transport exec_command."
    custom_tools = sorted(
        name for name, tool_type in allowed_tools.items() if tool_type == "custom"
    )
    if custom_tools:
        reminder += (
            " JSON-envelope compatibility only: Custom tools use input, not arguments: "
            '{"name":"TOOL_NAME","input":"RAW_INPUT"}. '
        )
    if allowed_tools.get("apply_patch") == "custom":
        reminder += (
            "For apply_patch, prefer summary=excel-proxy.raw/input/apply_patch with code=the complete raw patch; "
            "never use arguments.patch."
        )
    if "update_plan" in allowed_tools:
        reminder += (
            " Native update_plan is allowed for progress; after its result, "
            "take the next substantive action through run_officejs."
        )
    if source.get("parallel_tool_calls") is False:
        reminder += (
            " The client requires serial execution: emit at most one tool call per response "
            "and wait for its result before calling another tool. Do not use multi_tool_use.parallel."
        )
    return reminder
