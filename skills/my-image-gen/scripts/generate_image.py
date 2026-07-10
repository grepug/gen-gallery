#!/usr/bin/env python3
"""CLI entry point for the my-image-gen skill."""

from __future__ import annotations

import sys
from pathlib import Path

# Keep the implementation importable for the repository's unit tests while
# allowing this hyphenated skill directory to be executed directly.
repo_root = Path(__file__).resolve().parents[3]
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from skills.my_image_gen.scripts.generate_image import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
