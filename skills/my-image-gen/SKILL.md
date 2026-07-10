---
name: my-image-gen
description: Generate images from natural-language prompts using an OpenAI-compatible Images API. Use this skill when the user asks to create a new image and wants the result saved locally.
---

# My Image Gen

Use the bundled script for standalone image generation. It uses `POST /images/generations`, stores first-use credentials outside the repository, preserves the API-returned image format, and saves images under `~/.my-image-gen/images/`.

## Generate an image

Run:

```bash
python skills/my-image-gen/scripts/generate_image.py "小狗在喝水"
```

On first use, ask the user for an API key and base URL. The script stores them in `~/.my-image-gen/config.json` with restrictive permissions. Never print or echo the API key.

The default model is `gpt-image-2`. The script creates names such as `20260710-dog-drinking.png` or `20260710-dog-drinking.jpg`, based on the returned image bytes. Existing files are never overwritten; a numeric suffix is added instead.

## Handling failures

Report the script's error to the user. Common causes include invalid credentials, an unavailable image model, an unsupported `/images/generations` endpoint, network timeouts, and responses without `b64_json` or `url` image data.

Do not place credentials in repository files, command-line arguments, prompts, logs, or generated metadata.

## Bundled resource

`scripts/generate_image.py` is the deterministic implementation. Use it instead of rewriting the API request or image file handling in prose.
