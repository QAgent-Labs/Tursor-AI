from collections.abc import Iterator
from pathlib import Path

from app.settings import settings
from app.tursor_config import (
    TURSOR_DIR,
    TursorWorkspaceConfig,
    path_matches_include,
)


def _is_excluded_name(name: str, excluded: frozenset[str]) -> bool:
    return name in excluded


def iter_indexable_files(cfg: TursorWorkspaceConfig) -> Iterator[Path]:
    """Yield files under workspace, honoring excluded and include patterns."""
    root = cfg.workspace_root
    excluded = cfg.excluded
    include_patterns = cfg.include_patterns

    for path in root.rglob("*"):
        if not path.is_file():
            continue

        try:
            rel = path.relative_to(root)
        except ValueError:
            continue

        rel_str = str(rel).replace("\\", "/")
        parts = rel.parts
        if parts and parts[0] == TURSOR_DIR:
            continue

        skip = False
        for part in parts[:-1]:
            if _is_excluded_name(part, excluded):
                skip = True
                break
        if skip:
            continue

        if _is_excluded_name(path.name, excluded):
            continue

        suffix = path.suffix.lower()
        if suffix not in settings.text_extensions:
            continue

        if not path_matches_include(rel_str, include_patterns):
            continue

        yield path
