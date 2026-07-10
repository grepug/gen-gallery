from __future__ import annotations

import base64
import json
import mimetypes
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Union


@dataclass
class OpenAIImageResult:
    image_bytes: bytes
    seen_events: list[str]


class ImageGenerationError(RuntimeError):
    def __init__(
        self,
        message: str,
        retryable: bool,
        immediate_retry_on_other_key: bool = False,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.immediate_retry_on_other_key = immediate_retry_on_other_key


def extract_image_payload(response: dict) -> bytes | str:
    """Extract the first base64 image or URL from an images API response."""
    data = response.get("data")
    if isinstance(data, list) and data:
        first = data[0]
        if isinstance(first, dict) and first.get("b64_json"):
            try:
                return base64.b64decode(first["b64_json"], validate=True)
            except (ValueError, TypeError) as exc:
                raise RuntimeError("Invalid base64 image payload") from exc
        if isinstance(first, dict) and first.get("url"):
            return str(first["url"])
    raise RuntimeError("No image payload found in response.")


def generate_image_via_images_api(
    *, base_url: str, api_key: str, model: str, prompt: str, timeout_seconds: int
) -> bytes:
    """Use the legacy images endpoint used by some OpenAI-compatible proxies."""
    payload = {"model": model, "prompt": prompt, "response_format": "b64_json"}
    request = urllib.request.Request(
        base_url + "/images/generations",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            payload = extract_image_payload(json.loads(response.read()))
            if isinstance(payload, bytes):
                return payload
            with urllib.request.urlopen(payload, timeout=timeout_seconds) as image_response:
                return image_response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise ImageGenerationError(
            f"Images request failed with HTTP {exc.code}: {body}",
            retryable=exc.code >= 500 or exc.code in {401, 403, 429},
        ) from exc
    except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
        raise ImageGenerationError(
            f"Network error during images generation: {exc}", retryable=True
        ) from exc
    except (json.JSONDecodeError, RuntimeError) as exc:
        raise ImageGenerationError(
            f"Images response did not contain an image: {exc}", retryable=False
        ) from exc


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


def generate_image(
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
    payload = {
        "model": model,
        "stream": True,
        "input": build_input(prompt, reference_images),
        "tools": [
            {
                "type": "image_generation",
                "model": tool_model,
                "action": image_action,
            }
        ],
    }

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
        try:
            image_bytes = generate_image_via_images_api(
                base_url=base_url,
                api_key=api_key,
                model=tool_model,
                prompt=prompt,
                timeout_seconds=timeout_seconds,
            )
            return OpenAIImageResult(image_bytes=image_bytes, seen_events=seen_events)
        except ImageGenerationError as fallback_error:
            message = "No image payload found in SSE stream."
            if stream_error:
                message += f" Upstream error: {stream_error}."
            if seen_events:
                message += " Seen events: " + ", ".join(seen_events[:30])
            message += f" Images API fallback failed: {fallback_error}."
            raise ImageGenerationError(message, retryable=fallback_error.retryable) from fallback_error

    return OpenAIImageResult(
        image_bytes=base64.b64decode(result_b64),
        seen_events=seen_events,
    )
