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
_EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}


class ExcelImageUploads:
    def __init__(self) -> None:
        self._file_ids: OrderedDict[tuple[str, str], str] = OrderedDict()
        self._lock = asyncio.Lock()

    def forget(self, keys: set[tuple[str, str]]) -> None:
        for key in keys:
            self._file_ids.pop(key, None)

    async def rewrite(
        self, body: dict, client: httpx.AsyncClient, headers: dict,
    ) -> tuple[dict, set[tuple[str, str]]]:
        """Return an upstream body and cache keys eligible for one stale-file retry."""
        items = body.get("input")
        if not isinstance(items, list):
            return body, set()
        account = headers.get("chatgpt-account-id") or headers.get("x-openai-account-id", "")
        reused: set[tuple[str, str]] = set()
        uploaded: set[tuple[str, str]] = set()
        rewritten = []
        for item in items:
            if (not isinstance(item, dict) or item.get("type") not in (None, "message")
                    or not isinstance(item.get("content"), list)):
                rewritten.append(item)
                continue
            parts = []
            for part in item["content"]:
                url = part.get("image_url") if isinstance(part, dict) else None
                if (not isinstance(part, dict) or part.get("type") != "input_image"
                        or not isinstance(url, str) or not url.startswith("data:")):
                    parts.append(part)
                    continue
                header, separator, encoded = url.partition(",")
                if not separator or not header.startswith("data:image/") or not header.endswith(";base64"):
                    raise ValueError("Excel image input must contain a base64 image data URL.")
                media_type = header[5:].split(";", 1)[0]
                try:
                    data = base64.b64decode(encoded, validate=True)
                except (binascii.Error, ValueError) as exc:
                    raise ValueError("Excel image input contains invalid base64 data.") from exc
                if not data:
                    raise ValueError("Excel image input is empty.")
                digest = hashlib.sha256(data).hexdigest()
                key = (account, digest)
                # Only image uploads take this lock. Text requests continue
                # immediately; concurrent requests for one image share a file.
                async with self._lock:
                    file_id = self._file_ids.get(key)
                    if file_id is None:
                        file_id = await _upload(client, headers, media_type, data, digest)
                        self._file_ids[key] = file_id
                        uploaded.add(key)
                        while len(self._file_ids) > _CACHE_SIZE:
                            self._file_ids.popitem(last=False)
                    else:
                        self._file_ids.move_to_end(key)
                        if key not in uploaded:
                            reused.add(key)
                picture = {name: value for name, value in part.items() if name != "image_url"}
                picture.setdefault("detail", "auto")
                parts.append({**picture, "file_id": file_id})
            rewritten.append({**item, "content": parts})
        return {**body, "input": rewritten}, reused


async def _upload(client, headers, media_type, data, digest) -> str:
    upload_headers = {
        key: value for key, value in headers.items()
        if key.lower() not in {"accept", "content-type", "content-length"}
    }
    upload_headers["accept"] = "application/json"
    filename = f"picture-{digest[:12]}.{_EXTENSIONS.get(media_type, 'png')}"
    response = await client.post(
        ATTACHMENTS_URL, headers=upload_headers,
        files={"file": (filename, data, media_type)},
        timeout=httpx.Timeout(120.0, connect=30.0), follow_redirects=False,
    )
    response.raise_for_status()
    try:
        payload = response.json()
    except ValueError as exc:
        raise httpx.RemoteProtocolError("Excel attachment upload returned invalid JSON.") from exc
    file_id = payload.get("openai_file_id") if isinstance(payload, dict) else None
    if not isinstance(file_id, str) or not file_id.strip():
        raise httpx.RemoteProtocolError("Excel attachment upload returned no file ID.")
    return file_id.strip()


image_uploads = ExcelImageUploads()
