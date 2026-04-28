from __future__ import annotations

from pathlib import Path

from PIL import Image

from .errors import ImageGenerationError

TARGET_EDIT_SIZE = 1024


def prepare_sdk_edit_assets(
    reference_images: list[Path],
    temp_dir: Path,
) -> tuple[Path, Path]:
    if len(reference_images) != 1:
        raise ImageGenerationError(
            "SDK-backed reference-image edits currently support exactly one reference image.",
            retryable=False,
        )

    source_path = reference_images[0]
    try:
        with Image.open(source_path) as source_image:
            rgba_image = source_image.convert("RGBA")
    except Exception as exc:  # noqa: BLE001
        raise ImageGenerationError(
            f"Could not open reference image for SDK edit preprocessing: {source_path}",
            retryable=False,
        ) from exc

    image_path = temp_dir / "edit-image.png"
    mask_path = temp_dir / "edit-mask.png"

    prepared_image = _fit_into_square_canvas(rgba_image)
    prepared_image.save(image_path, format="PNG")

    mask_image = Image.new(
        "RGBA",
        (TARGET_EDIT_SIZE, TARGET_EDIT_SIZE),
        (0, 0, 0, 0),
    )
    mask_image.save(mask_path, format="PNG")

    return image_path, mask_path


def _fit_into_square_canvas(source_image: Image.Image) -> Image.Image:
    source_width, source_height = source_image.size
    if source_width <= 0 or source_height <= 0:
        raise ImageGenerationError(
            "Reference image must have non-zero dimensions for SDK edit preprocessing.",
            retryable=False,
        )

    scale = min(
        TARGET_EDIT_SIZE / source_width,
        TARGET_EDIT_SIZE / source_height,
    )
    resized_width = max(1, round(source_width * scale))
    resized_height = max(1, round(source_height * scale))
    resized_image = source_image.resize(
        (resized_width, resized_height),
        Image.Resampling.LANCZOS,
    )

    canvas = Image.new(
        "RGBA",
        (TARGET_EDIT_SIZE, TARGET_EDIT_SIZE),
        (0, 0, 0, 0),
    )
    offset_x = (TARGET_EDIT_SIZE - resized_width) // 2
    offset_y = (TARGET_EDIT_SIZE - resized_height) // 2
    canvas.alpha_composite(resized_image, (offset_x, offset_y))
    return canvas
