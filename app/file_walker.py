from collections.abc import Iterator
from pathlib import Path

from app.settings import settings
from app.tursor_config import TURSOR_DIR, TursorWorkspaceConfig


def _is_excluded_name(name: str, excluded: frozenset[str]) -> bool:
    return name in excluded


def iter_indexable_files(cfg: TursorWorkspaceConfig) -> Iterator[Path]:
    """Yield files under workspace, honoring excluded folder/file names."""
    root = cfg.workspace_root
    excluded = cfg.excluded

    for path in root.rglob("*"):
        if not path.is_file():
            continue

        try:
            rel = path.relative_to(root)
        except ValueError:
            continue

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
        if suffix not in settings.text_extensions and path.name not in excluded:
            continue

        if suffix not in settings.text_extensions:
            continue

        yield path
