"""Translate Codex native image requests to the Excel add-in endpoints."""

import base64
import binascii

import excel_upstream


GENERATIONS_URL = excel_upstream.RESPONSES_URL.rsplit("/", 1)[0] + "/images/generations"
EDITS_URL = excel_upstream.RESPONSES_URL.rsplit("/", 1)[0] + "/images/edits"
MAX_IMAGE_BYTES = 20 * 1024 * 1024
_EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}
_CHOICES = {
    "size": ("auto", "1024x1024", "1536x1024", "1024x1536", "1280x720"),
    "quality": ("auto", "low", "medium", "high"),
    "background": ("auto", "opaque"),
}


def prepare_request(body, *, edit=False):
    if not isinstance(body, dict):
        raise ValueError("Image request must be a JSON object.")
    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("A nonempty image prompt is required.")
    if body.get("model") not in (None, "gpt-image-2"):
        raise ValueError("Excel image requests support gpt-image-2 only.")
    if body.get("output_format") not in (None, "png"):
        raise ValueError("Excel image requests return PNG only.")
    if body.get("response_format") not in (None, "b64_json"):
        raise ValueError("Excel image requests return b64_json only.")
    if body.get("stream") not in (None, False):
        raise ValueError("Streaming image responses are not supported.")
    if body.get("mask") is not None:
        raise ValueError("Masked edits are not supported by the Excel image endpoint.")
    fields = {"prompt": prompt, "model": "gpt-image-2", "output_format": "png"}
    for key, choices in _CHOICES.items():
        value = "auto" if body.get(key) is None else body[key]
        if value not in choices:
            raise ValueError(f"Unsupported {key}; use {', '.join(choices)}.")
        fields[key] = value
    count = body.get("n", 1)
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 3:
        raise ValueError("n must be an integer from 1 to 3.")
    fields["n"] = count
    if not edit:
        return {"json": fields}
    images = body.get("images")
    if not isinstance(images, list) or not 1 <= len(images) <= 3:
        raise ValueError("Edits require one to three inline images.")
    files = []
    field_name = "image" if len(images) == 1 else "image[]"
    for index, image in enumerate(images):
        url = image.get("image_url") if isinstance(image, dict) else None
        if not isinstance(url, str) or not url.startswith("data:"):
            raise ValueError("Edit images must be inline base64 data URLs, not remote URLs.")
        header, separator, encoded = url.partition(",")
        media_type = header[5:].removesuffix(";base64")
        if not separator or not header.endswith(";base64") or media_type not in _EXTENSIONS:
            raise ValueError("Edit images must be PNG, JPEG, or WebP base64 data URLs.")
        if len(encoded) > ((MAX_IMAGE_BYTES + 2) // 3) * 4:
            raise ValueError("Each edit image must be at most 20 MiB.")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("Edit image contains invalid base64 data.") from exc
        if not data or len(data) > MAX_IMAGE_BYTES:
            raise ValueError("Each edit image must contain 1 byte to 20 MiB.")
        files.append((field_name, (f"image-{index + 1}.{_EXTENSIONS[media_type]}", data, media_type)))
    return {"data": {key: str(value) for key, value in fields.items()}, "files": files}


def validate_response(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list) or not payload["data"]:
        raise ValueError("Excel returned no generated images.")
    for image in payload["data"]:
        encoded = image.get("b64_json") if isinstance(image, dict) else None
        if not isinstance(encoded, str) or not encoded:
            raise ValueError("Excel returned an invalid image response.")
        try:
            if not base64.b64decode(encoded, validate=True):
                raise ValueError("Empty image")
        except (ValueError, binascii.Error) as exc:
            raise ValueError("Excel returned invalid image data.") from exc
    return payload
