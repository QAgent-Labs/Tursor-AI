import json
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

TURSOR_DIR = ".tursor"
CONFIG_FILE = "config.json"
EMBEDDINGS_DIR = "embeddings"


@dataclass(frozen=True)
class TursorAiConfig:
    generation_model: str
    api_key: str


@dataclass(frozen=True)
class TursorWorkspaceConfig:
    workspace_root: Path
    excluded: frozenset[str]
    include_patterns: frozenset[str]
    ai: TursorAiConfig | None = None

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


def _normalize_rel_path(rel: str) -> str:
    return rel.replace("\\", "/")


def path_matches_include(relative_path: str, patterns: frozenset[str]) -> bool:
    if not patterns:
        return True
    normalized = _normalize_rel_path(relative_path)
    return any(fnmatch(normalized, pattern) for pattern in patterns)


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

    include_patterns: set[str] = set()
    include_raw = raw.get("include")
    if include_raw is not None:
        if not isinstance(include_raw, dict):
            raise TursorConfigError('"include" must be an object')
        patterns_raw = include_raw.get("patterns")
        if patterns_raw is not None:
            if not isinstance(patterns_raw, list):
                raise TursorConfigError('"include.patterns" must be an array of strings')
            for item in patterns_raw:
                if not isinstance(item, str) or not item.strip():
                    raise TursorConfigError(
                        '"include.patterns" entries must be non-empty strings',
                    )
                include_patterns.add(item.strip())

    ai_config: TursorAiConfig | None = None
    ai_raw = raw.get("ai")
    if ai_raw is not None:
        if not isinstance(ai_raw, dict):
            raise TursorConfigError('"ai" must be an object')
        model_raw = ai_raw.get("generationModel")
        if not isinstance(model_raw, str) or not model_raw.strip():
            raise TursorConfigError(
                '"ai.generationModel" must be a non-empty string',
            )
        key_raw = ai_raw.get("apiKey")
        if not isinstance(key_raw, str) or not key_raw.strip():
            raise TursorConfigError('"ai.apiKey" must be a non-empty string')
        ai_config = TursorAiConfig(
            generation_model=model_raw.strip(),
            api_key=key_raw.strip(),
        )

    return TursorWorkspaceConfig(
        workspace_root=root,
        excluded=frozenset(excluded),
        include_patterns=frozenset(include_patterns),
        ai=ai_config,
    )
