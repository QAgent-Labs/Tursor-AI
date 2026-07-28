from dataclasses import dataclass
from pathlib import Path

from app.settings import settings


@dataclass(frozen=True)
class TextChunk:
    relative_path: str
    chunk_index: int
    start_line: int
    end_line: int
    text: str


def _line_number_at(text: str, char_index: int) -> int:
    return text.count("\n", 0, char_index) + 1


def chunk_file(path: Path, workspace_root: Path) -> list[TextChunk]:
    rel = str(path.relative_to(workspace_root)).replace("\\", "/")
    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return []

    if not content.strip():
        return []

    chunk_size = settings.chunk_size
    overlap = settings.chunk_overlap
    chunks: list[TextChunk] = []
    start = 0
    chunk_index = 0
    length = len(content)

    while start < length:
        end = min(start + chunk_size, length)
        if end < length:
            break_at = content.rfind("\n", start, end)
            if break_at > start + chunk_size // 2:
                end = break_at + 1

        piece = content[start:end]
        if piece.strip():
            start_line = _line_number_at(content, start)
            end_line = _line_number_at(content, max(start, end - 1))
            chunks.append(
                TextChunk(
                    relative_path=rel,
                    chunk_index=chunk_index,
                    start_line=start_line,
                    end_line=end_line,
                    text=piece,
                ),
            )
            chunk_index += 1

        if end >= length:
            break
        start = max(0, end - overlap)

    return chunks
