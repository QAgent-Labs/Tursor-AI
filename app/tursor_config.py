import json
from dataclasses import dataclass
from pathlib import Path

TURSOR_DIR = ".tursor"
CONFIG_FILE = "config.json"
EMBEDDINGS_DIR = "embeddings"


@dataclass(frozen=True)
class TursorWorkspaceConfig:
    workspace_root: Path
    excluded: frozenset[str]

    @property
    def tursor_dir(self) -> Path:
        return self.workspace_root / TURSOR_DIR

    @property
    def config_path(self) -> Path:
        return self.tursor_dir / CONFIG_FILE

    @property
    def embeddings_path(self) -> Path:
        return self.tursor_dir / EMBEDDINGS_DIR


class TursorConfigError(Exception):
    pass


def load_tursor_config(workspace_root: Path) -> TursorWorkspaceConfig:
    root = workspace_root.resolve()
    if not root.is_dir():
        raise TursorConfigError(f"Not a directory: {root}")

    config_path = root / TURSOR_DIR / CONFIG_FILE
    if not config_path.is_file():
        raise TursorConfigError(
            f"Missing {TURSOR_DIR}/{CONFIG_FILE} under {root}",
        )

    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TursorConfigError(f"Invalid JSON in {config_path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise TursorConfigError(f"{config_path} must be a JSON object")

    excluded_raw = raw.get("excluded")
    if excluded_raw is None:
        excluded_raw = []
    if not isinstance(excluded_raw, list):
        raise TursorConfigError('"excluded" must be an array of strings')

    excluded: set[str] = set()
    for item in excluded_raw:
        if not isinstance(item, str) or not item.strip():
            raise TursorConfigError(
                '"excluded" entries must be non-empty strings',
            )
        excluded.add(item.strip())

    return TursorWorkspaceConfig(workspace_root=root, excluded=frozenset(excluded))
