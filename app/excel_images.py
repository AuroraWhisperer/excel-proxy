"""Upload message images using the Excel add-in's authenticated attachment API.

The wire format follows Kaixxrua/excel-codex-bridge's images.py. User messages
reference uploaded files; tool results can retain their inline image data.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
from collections import OrderedDict

import httpx

import excel_upstream


ATTACHMENTS_URL = excel_upstream.RESPONSES_URL.rsplit("/", 1)[0] + "/attachments"
_CACHE_SIZE = 256
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 32 * 1024 * 1024
MAX_IMAGES = 20
_EXTENSIONS = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
}


def _validated_images(items: list) -> dict[tuple[int, int], tuple[str, bytes]]:
    """Check the whole image request before making any attachment uploads."""
    decoded = {}
    count = total_bytes = 0
    for item_index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        if item.get("type") in (None, "message", "agent_message"):
            parts = item.get("content")
        elif item.get("type") in {"function_call_output", "custom_tool_call_output"}:
            parts = item.get("output")
        else:
            continue
        if not isinstance(parts, list):
            continue
        for part_index, part in enumerate(parts):
            if not isinstance(part, dict) or part.get("type") != "input_image":
                continue
            count += 1
            if count > MAX_IMAGES:
                raise ValueError(f"Excel requests support at most {MAX_IMAGES} images.")
            url = part.get("image_url")
            if not isinstance(url, str) or not url.startswith("data:"):
                continue
            header, separator, encoded = url.partition(",")
            media_type = header[5:].removesuffix(";base64")
            if (
                not separator
                or not header.endswith(";base64")
                or media_type not in _EXTENSIONS
            ):
                raise ValueError(
                    "Excel inline images must be PNG, JPEG, GIF, or WebP base64 data URLs."
                )
            if len(encoded) > ((MAX_IMAGE_BYTES + 2) // 3) * 4:
                raise ValueError("Each Excel image must be at most 20 MiB.")
            try:
                data = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ValueError(
                    "Excel image input contains invalid base64 data."
                ) from exc
            if not data or len(data) > MAX_IMAGE_BYTES:
                raise ValueError("Each Excel image must contain 1 byte to 20 MiB.")
            total_bytes += len(data)
            if total_bytes > MAX_TOTAL_IMAGE_BYTES:
                raise ValueError(
                    "Inline Excel images must total at most 32 MiB per request."
                )
            decoded[item_index, part_index] = (media_type, data)
    return decoded


class ExcelImageUploads:
    def __init__(self) -> None:
        self._file_ids: OrderedDict[tuple[str, str], str] = OrderedDict()
        self._locks: dict[tuple[str, str], tuple[asyncio.Lock, int]] = {}

    def forget(self, keys: set[tuple[str, str]]) -> None:
        for key in keys:
            self._file_ids.pop(key, None)

    async def _file_id(
        self, key, client, headers, media_type, data
    ) -> tuple[str, bool]:
        lock, users = self._locks.get(key, (asyncio.Lock(), 0))
        self._locks[key] = (lock, users + 1)
        try:
            async with lock:
                file_id = self._file_ids.get(key)
                if file_id is not None:
                    self._file_ids.move_to_end(key)
                    return file_id, True
                file_id = await _upload(client, headers, media_type, data, key[1])
                self._file_ids[key] = file_id
                while len(self._file_ids) > _CACHE_SIZE:
                    self._file_ids.popitem(last=False)
                return file_id, False
        finally:
            remaining = self._locks[key][1] - 1
            if remaining:
                self._locks[key] = (lock, remaining)
            else:
                del self._locks[key]

    async def rewrite(
        self,
        body: dict,
        client: httpx.AsyncClient,
        headers: dict,
    ) -> tuple[dict, set[tuple[str, str]]]:
        """Return an upstream body and cache keys eligible for one stale-file retry."""
        items = body.get("input")
        if not isinstance(items, list):
            return body, set()
        decoded = _validated_images(items)
        account = headers.get("chatgpt-account-id") or headers.get(
            "x-openai-account-id", ""
        )
        reused: set[tuple[str, str]] = set()
        uploaded: set[tuple[str, str]] = set()
        rewritten = []
        for item_index, item in enumerate(items):
            if (
                not isinstance(item, dict)
                or item.get("type") not in (None, "message", "agent_message")
                or not isinstance(item.get("content"), list)
            ):
                rewritten.append(item)
                continue
            parts = []
            for part_index, part in enumerate(item["content"]):
                picture_data = decoded.get((item_index, part_index))
                if picture_data is None:
                    parts.append(part)
                    continue
                media_type, data = picture_data
                digest = hashlib.sha256(data).hexdigest()
                key = (account, digest)
                # Same-account copies of one image share an upload; unrelated
                # pictures and text requests do not wait for it.
                file_id, cached = await self._file_id(
                    key, client, headers, media_type, data
                )
                if not cached:
                    uploaded.add(key)
                elif key not in uploaded:
                    reused.add(key)
                picture = {
                    name: value for name, value in part.items() if name != "image_url"
                }
                picture.setdefault("detail", "auto")
                parts.append({**picture, "file_id": file_id})
            rewritten.append({**item, "content": parts})
        return {**body, "input": rewritten}, reused


async def _upload(client, headers, media_type, data, digest) -> str:
    upload_headers = {
        key: value
        for key, value in headers.items()
        if key.lower() not in {"accept", "content-type", "content-length"}
    }
    upload_headers["accept"] = "application/json"
    filename = f"picture-{digest[:12]}.{_EXTENSIONS.get(media_type, 'png')}"
    response = await client.post(
        ATTACHMENTS_URL,
        headers=upload_headers,
        files={"file": (filename, data, media_type)},
        timeout=httpx.Timeout(120.0, connect=30.0),
        follow_redirects=False,
    )
    response.raise_for_status()
    try:
        payload = response.json()
    except ValueError as exc:
        raise httpx.RemoteProtocolError(
            "Excel attachment upload returned invalid JSON."
        ) from exc
    file_id = payload.get("openai_file_id") if isinstance(payload, dict) else None
    if not isinstance(file_id, str) or not file_id.strip():
        raise httpx.RemoteProtocolError("Excel attachment upload returned no file ID.")
    return file_id.strip()


image_uploads = ExcelImageUploads()
