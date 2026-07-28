"""Manual smoke test (no pytest required)."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ["TURSOR_AI_HASH_EMBEDDINGS"] = "1"

from app.embed_service import build_embeddings, validate_workspace  # noqa: E402
from app.tursor_config import TursorConfigError  # noqa: E402


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        tursor = root / ".tursor"
        tursor.mkdir()
        (tursor / "config.json").write_text(
            '{"excluded": ["node_modules"]}',
            encoding="utf-8",
        )
        (root / "sample.ts").write_text("export const ok = true;\n", encoding="utf-8")
        (root / "node_modules").mkdir()
        (root / "node_modules" / "skip.js").write_text("x", encoding="utf-8")

        try:
            validate_workspace(str(root / "missing"))
        except TursorConfigError:
            pass
        else:
            print("expected TursorConfigError for missing config")
            return 1

        cfg = validate_workspace(str(root))
        assert "node_modules" in cfg.excluded

        result = build_embeddings(str(root))
        assert result.files_indexed == 1
        assert result.chunks_indexed >= 1
        assert (tursor / "embeddings" / "manifest.json").is_file()
        assert (tursor / "embeddings" / "chunks.jsonl").is_file()

    print("verify ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
