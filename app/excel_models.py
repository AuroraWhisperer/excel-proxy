"""Shared Excel model catalog; independent of sessions and request translation."""

import os


EXCEL_MODEL_UPSTREAMS = {
    "gpt-6-astra-excel": "gpt-6-astra",
    "gpt-5.6-luna-excel": "gpt-5.6-luna",
    "gpt-5.6-terra-excel": "gpt-5.6-terra",
    "gpt-5.6-sol-excel": "gpt-5.6-sol",
}
MODEL_IDS = tuple(EXCEL_MODEL_UPSTREAMS)
MODEL_ID = "gpt-5.6-sol-excel"
ASTRA_COMPACTION_TOKEN_LIMIT = 258_000
_UPSTREAM_MODEL_OVERRIDE = os.environ.get("GHCP_EXCEL_UPSTREAM_MODEL", "").strip()
UPSTREAM_MODEL = _UPSTREAM_MODEL_OVERRIDE or EXCEL_MODEL_UPSTREAMS[MODEL_ID]
EXCEL_REASONING_EFFORTS = ("low", "medium", "high", "xhigh")
EXCEL_MODEL_REASONING_EFFORTS = {
    "gpt-6-astra-excel": ("medium", "high", "xhigh"),
}
_REASONING_EFFORT_ALIASES = {
    "x-high": "xhigh",
    "extra-high": "xhigh",
    "extra_high": "xhigh",
}


LOCAL_MODEL_CAPABILITIES = {
    model_id: {
        "auto_compact_token_limit": {
            "gpt-6-astra-excel": ASTRA_COMPACTION_TOKEN_LIMIT * 9 // 10,
            "gpt-5.6-luna-excel": 180_000,
        }.get(model_id, 240_000),
        "context_window": 200_000 if "luna" in model_id else 272_000,
        "display_name": {
            "gpt-6-astra-excel": "6-Astra Excel",
            "gpt-5.6-luna-excel": "5.6-Luna Excel",
            "gpt-5.6-terra-excel": "5.6-Terra Excel",
            "gpt-5.6-sol-excel": "5.6-Sol Excel",
        }.get(
            model_id, model_id.removeprefix("gpt-").removesuffix("-excel").upper()
        ),
        "input_modalities": ["text", "image"],
        "max_context_window": 200_000 if "luna" in model_id else 272_000,
        "messages_endpoint_supported": False,
        "model_picker_enabled": True,
        "parallel_tool_calls": True,
        "provider": "OpenAI Excel",
        "reasoning_efforts": list(
            EXCEL_MODEL_REASONING_EFFORTS.get(model_id, EXCEL_REASONING_EFFORTS)
        ),
        "supported_endpoints": ["/responses"],
        "vision": True,
    }
    for model_id in MODEL_IDS
}


def model_display_name(model_id: str | None) -> str:
    if not model_id:
        return "—"
    return LOCAL_MODEL_CAPABILITIES.get(model_id, {}).get(
        "display_name", model_id.removeprefix("gpt-")
    )


def is_excel_model(model: object) -> bool:
    return excel_model_id(model) is not None


def excel_model_id(model: object) -> str | None:
    if not isinstance(model, str):
        return None
    normalized = model.strip().lower()
    return normalized if normalized in EXCEL_MODEL_UPSTREAMS else None


def upstream_model_for(model: object) -> str:
    model_id = excel_model_id(model) or MODEL_ID
    return _UPSTREAM_MODEL_OVERRIDE or EXCEL_MODEL_UPSTREAMS[model_id]


def normalize_reasoning_effort(value: object, model: object = None) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    normalized = _REASONING_EFFORT_ALIASES.get(normalized, normalized)
    allowed = EXCEL_MODEL_REASONING_EFFORTS.get(
        excel_model_id(model), EXCEL_REASONING_EFFORTS
    )
    return normalized if normalized in allowed else None


def local_model_payload(model_id: str) -> dict[str, object]:
    return {
        "id": model_id,
        "object": "model",
        "created": 0,
        "owned_by": "openai-excel",
    }


def merge_local_model_capabilities(
    capabilities: dict[str, dict] | None,
) -> dict[str, dict]:
    merged = dict(capabilities or {})
    merged.update({key: dict(value) for key, value in LOCAL_MODEL_CAPABILITIES.items()})
    return merged


def merge_local_models_payload(payload: dict | None) -> dict:
    result = dict(payload or {})
    raw_data = result.get("data")
    data = (
        [dict(item) for item in raw_data if isinstance(item, dict)]
        if isinstance(raw_data, list)
        else []
    )
    data = [item for item in data if item.get("id") != "gpt-excel"]
    existing_ids = {item.get("id") for item in data}
    data.extend(
        local_model_payload(model_id)
        for model_id in MODEL_IDS
        if model_id not in existing_ids
    )
    result["object"] = result.get("object") or "list"
    result["data"] = data
    return result
