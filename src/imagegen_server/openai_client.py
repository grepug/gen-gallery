from __future__ import annotations

import base64
import json
import mimetypes
import socket
import tempfile
import urllib.error
import urllib.request
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Union

from openai import OpenAI

from .errors import ImageGenerationError
from .sdk_edit_prep import prepare_sdk_edit_assets


@dataclass
class OpenAIImageResult:
    image_bytes: bytes
    seen_events: list[str]


def summarize_stream_error(event: dict) -> str:
    error = event.get("error")
    if isinstance(error, dict):
        code = error.get("code")
        message = error.get("message")
        if code and message:
            return f"{code}: {message}"
        if code:
            return str(code)
        if message:
            return str(message)

    response = event.get("response")
    if isinstance(response, dict):
        status = response.get("status")
        reason = response.get("status_details")
        if status and reason:
            return f"{status}: {reason}"
        if status:
            return str(status)
        if reason:
            return str(reason)

    return json.dumps(event, ensure_ascii=False, sort_keys=True)


def sniff_image_mime_type(image_bytes: bytes) -> str | None:
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if image_bytes.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    if image_bytes.startswith(b"BM"):
        return "image/bmp"
    return None


def make_data_url(image_path: Path) -> str:
    image_bytes = image_path.read_bytes()
    mime_type = sniff_image_mime_type(image_bytes)
    if not mime_type:
        mime_type, _ = mimetypes.guess_type(str(image_path))
    if not mime_type or not mime_type.startswith("image/"):
        raise ImageGenerationError(
            f"Could not determine an image MIME type for '{image_path}'.",
            retryable=False,
        )
    image_b64 = base64.b64encode(image_bytes).decode("ascii")
    return f"data:{mime_type};base64,{image_b64}"


def build_input(prompt: str, reference_images: list[Path]) -> Union[str, list[dict]]:
    if not reference_images:
        return prompt

    content: list[dict[str, str]] = [{"type": "input_text", "text": prompt}]
    for image_path in reference_images:
        if not image_path.exists():
            raise ImageGenerationError(
                f"Reference image not found: {image_path}",
                retryable=False,
            )
        if not image_path.is_file():
            raise ImageGenerationError(
                f"Reference image is not a file: {image_path}",
                retryable=False,
            )
        content.append(
            {
                "type": "input_image",
                "image_url": make_data_url(image_path),
            }
        )
    return [{"role": "user", "content": content}]


def build_responses_payload(
    *,
    model: str,
    tool_model: str,
    image_action: str,
    prompt: str,
    reference_images: list[Path],
    stream: bool,
) -> dict[str, Any]:
    return {
        "model": model,
        "stream": stream,
        "input": build_input(prompt, reference_images),
        "tools": [
            {
                "type": "image_generation",
                "model": tool_model,
                "action": image_action,
            }
        ],
    }


def _extract_image_result_from_output_items(
    output_items: list[Any],
) -> tuple[str | None, list[str], str | None]:
    result_b64 = None
    seen_events: list[str] = []
    stream_error = None

    for item in output_items:
        item_type = getattr(item, "type", None)
        if item_type:
            seen_events.append(str(item_type))
        if item_type == "image_generation_call":
            item_result = getattr(item, "result", None)
            if item_result:
                result_b64 = item_result
        if item_type == "message":
            for content_item in getattr(item, "content", []) or []:
                content_type = getattr(content_item, "type", None)
                if content_type:
                    seen_events.append(str(content_type))
        item_error = getattr(item, "error", None)
        if item_error and not stream_error:
            stream_error = str(item_error)

    return result_b64, seen_events, stream_error


def _extract_image_bytes_from_sdk_response(response: Any) -> bytes:
    if isinstance(response, dict):
        data_items = list(response.get("data", []) or [])
    else:
        data_items = list(getattr(response, "data", []) or [])
    if not data_items:
        raise ImageGenerationError(
            "No image payload found in SDK response.",
            retryable=True,
        )

    first_item = data_items[0]
    if isinstance(first_item, dict):
        b64_json = first_item.get("b64_json")
        image_url = first_item.get("url")
    else:
        b64_json = getattr(first_item, "b64_json", None)
        image_url = getattr(first_item, "url", None)
    if b64_json:
        return base64.b64decode(b64_json)

    if image_url:
        with urllib.request.urlopen(image_url) as response_handle:
            return response_handle.read()

    raise ImageGenerationError(
        "No image payload found in SDK response.",
        retryable=True,
    )


def _map_sdk_exception(exc: Exception) -> ImageGenerationError:
    status_code = getattr(exc, "status_code", None)
    if not isinstance(status_code, int):
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
    if isinstance(status_code, int):
        immediate_retry_on_other_key = status_code in {401, 403}
        retryable = status_code >= 500 or status_code in {401, 403, 429}
        return ImageGenerationError(
            f"Responses SDK request failed with HTTP {status_code}: {exc}",
            retryable=retryable,
            immediate_retry_on_other_key=immediate_retry_on_other_key,
        )

    if isinstance(exc, (TimeoutError, socket.timeout)):
        return ImageGenerationError(
            f"Network error during image generation: {exc}",
            retryable=True,
        )

    exc_name = exc.__class__.__name__
    if exc_name in {
        "APIConnectionError",
        "APITimeoutError",
        "ConnectError",
        "ReadError",
        "ReadTimeout",
        "TimeoutException",
        "WriteError",
    }:
        return ImageGenerationError(
            f"Network error during image generation: {exc}",
            retryable=True,
        )

    return ImageGenerationError(
        f"SDK error during image generation: {exc}",
        retryable=False,
    )


def generate_image_via_responses_http(
    *,
    base_url: str,
    api_key: str,
    model: str,
    tool_model: str,
    image_action: str,
    prompt: str,
    reference_images: list[Path],
    timeout_seconds: int,
) -> OpenAIImageResult:
    payload = build_responses_payload(
        model=model,
        tool_model=tool_model,
        image_action=image_action,
        prompt=prompt,
        reference_images=reference_images,
        stream=True,
    )

    request = urllib.request.Request(
        base_url + "/responses",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "Accept": "text/event-stream",
        },
        method="POST",
    )

    result_b64 = None
    seen_events: list[str] = []
    stream_error = None

    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="ignore").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data or data == "[DONE]":
                    continue

                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    continue

                event_type = event.get("type")
                if event_type:
                    seen_events.append(str(event_type))
                if event_type in {"error", "response.failed"}:
                    stream_error = summarize_stream_error(event)

                item = event.get("item") or {}
                if item.get("type") == "image_generation_call" and item.get("result"):
                    result_b64 = item["result"]
                if (
                    event_type == "response.output_item.done"
                    and item.get("type") == "image_generation_call"
                    and item.get("result")
                ):
                    result_b64 = item["result"]
                    break
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        immediate_retry_on_other_key = exc.code in {401, 403}
        retryable = exc.code >= 500 or exc.code in {401, 403, 429}
        raise ImageGenerationError(
            f"Responses request failed with HTTP {exc.code}: {body}",
            retryable=retryable,
            immediate_retry_on_other_key=immediate_retry_on_other_key,
        ) from exc
    except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
        raise ImageGenerationError(
            f"Network error during image generation: {exc}",
            retryable=True,
        ) from exc

    if not result_b64:
        message = "No image payload found in SSE stream."
        if stream_error:
            message += f" Upstream error: {stream_error}."
        if seen_events:
            message += " Seen events: " + ", ".join(seen_events[:30])
        raise ImageGenerationError(message, retryable=True)

    return OpenAIImageResult(
        image_bytes=base64.b64decode(result_b64),
        seen_events=seen_events,
    )


def generate_image_via_openai_sdk(
    *,
    base_url: str,
    api_key: str,
    model: str,
    tool_model: str,
    image_action: str,
    prompt: str,
    reference_images: list[Path],
    timeout_seconds: int,
) -> OpenAIImageResult:
    try:
        client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout_seconds,
        )
        sdk_model = tool_model or model
        if reference_images and image_action != "edit":
            raise ImageGenerationError(
                "SDK-backed reference-image requests currently require image_action=edit.",
                retryable=False,
            )
        uses_edit_path = image_action == "edit"
        if uses_edit_path:
            if not reference_images:
                raise ImageGenerationError(
                    "SDK image edit requires at least one reference image.",
                    retryable=False,
                )
            with ExitStack() as exit_stack:
                temp_dir = Path(exit_stack.enter_context(tempfile.TemporaryDirectory()))
                prepared_image_path, prepared_mask_path = prepare_sdk_edit_assets(
                    reference_images,
                    temp_dir,
                )
                image_handle = exit_stack.enter_context(prepared_image_path.open("rb"))
                mask_handle = exit_stack.enter_context(prepared_mask_path.open("rb"))
                request = client._client.build_request(
                    "POST",
                    str(client.base_url) + "images/edits",
                    data={
                        "model": sdk_model,
                        "prompt": prompt,
                        "output_format": "png",
                        "response_format": "b64_json",
                        "size": "1024x1024",
                    },
                    files={
                        "image": ("edit-image.png", image_handle, "image/png"),
                        "mask": ("edit-mask.png", mask_handle, "image/png"),
                    },
                    headers={"Authorization": f"Bearer {api_key}"},
                )
                response = client._client.send(request)
                response.raise_for_status()
                response = response.json()
            seen_events = ["images.edit"]
        else:
            response = client.images.generate(
                model=sdk_model,
                prompt=prompt,
                size="1024x1024",
                response_format="b64_json",
            )
            seen_events = ["images.generate"]
    except Exception as exc:  # noqa: BLE001
        if isinstance(exc, ImageGenerationError):
            raise
        raise _map_sdk_exception(exc) from exc

    return OpenAIImageResult(
        image_bytes=_extract_image_bytes_from_sdk_response(response),
        seen_events=seen_events,
    )


def generate_image(
    *,
    transport: str,
    base_url: str,
    api_key: str,
    model: str,
    tool_model: str,
    image_action: str,
    prompt: str,
    reference_images: list[Path],
    timeout_seconds: int,
) -> OpenAIImageResult:
    if transport == "responses_http":
        return generate_image_via_responses_http(
            base_url=base_url,
            api_key=api_key,
            model=model,
            tool_model=tool_model,
            image_action=image_action,
            prompt=prompt,
            reference_images=reference_images,
            timeout_seconds=timeout_seconds,
        )
    if transport == "openai_sdk":
        return generate_image_via_openai_sdk(
            base_url=base_url,
            api_key=api_key,
            model=model,
            tool_model=tool_model,
            image_action=image_action,
            prompt=prompt,
            reference_images=reference_images,
            timeout_seconds=timeout_seconds,
        )
    raise ImageGenerationError(
        f"Unsupported image transport: {transport}",
        retryable=False,
    )
