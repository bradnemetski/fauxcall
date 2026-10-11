"""Text-file storage: atomic JSON / JSONL reads and writes.

Everything FauxCall persists is diffable text committed to git. Writes are atomic (temp file +
rename) so an interrupted multi-hour build never leaves a half-written file behind, and appends to
shared JSONL files take an advisory lock so concurrent writers don't interleave lines.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Final

from pydantic import BaseModel

from fauxcall import jsonfmt
from fauxcall.jsonfmt import JsonStyle

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from fauxcall.types import JsonValue

try:  # POSIX only; FauxCall supports macOS and Linux, Windows is best-effort.
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

PRETTY: Final = JsonStyle(item_separator=",", key_separator=": ", indent=2, trailing_newline=True)
"""Style for files humans review in diffs."""


def atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically, creating parent directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        Path(temp_name).replace(path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            Path(temp_name).unlink()
        raise


def write_json(path: Path, value: JsonValue, style: JsonStyle = PRETTY) -> None:
    """Atomically write a JSON value (float spellings are preserved)."""
    atomic_write_text(path, jsonfmt.dumps(value, style))


def read_json(path: Path) -> JsonValue:
    """Read a JSON file, preserving key order and float spellings."""
    return jsonfmt.loads(path.read_text(encoding="utf-8"))


def _merge_lexemes(json_node: object, python_node: object) -> object:
    """Put :class:`JsonFloat` spellings from a python-mode dump back into a json-mode dump."""
    if isinstance(python_node, jsonfmt.JsonFloat):
        return python_node
    if isinstance(json_node, dict) and isinstance(python_node, dict):
        return {
            key: _merge_lexemes(value, python_node.get(key)) for key, value in json_node.items()
        }
    parallel_lists = isinstance(json_node, list) and isinstance(python_node, (list, tuple))
    if parallel_lists and len(json_node) == len(python_node):  # type: ignore[arg-type]
        return [_merge_lexemes(a, b) for a, b in zip(json_node, python_node, strict=True)]  # type: ignore[call-overload]
    return json_node


def model_to_json(model: BaseModel, *, exclude_defaults: bool = True) -> JsonValue:
    """Dump a model to JSON-compatible data, keeping float spellings inside raw JSON fields."""
    as_json = model.model_dump(mode="json", by_alias=True, exclude_defaults=exclude_defaults)
    as_python = model.model_dump(mode="python", by_alias=True, exclude_defaults=exclude_defaults)
    merged: JsonValue = _merge_lexemes(as_json, as_python)  # type: ignore[assignment]
    return merged


def write_model(path: Path, model: BaseModel) -> None:
    """Atomically write a pydantic model as pretty JSON, omitting default values for lean diffs."""
    write_json(path, model_to_json(model))


def read_model[ModelT: BaseModel](path: Path, model_type: type[ModelT]) -> ModelT:
    """Read and validate a pydantic model from a JSON file (float spellings preserved)."""
    return model_type.model_validate(read_json(path))


def write_jsonl(path: Path, values: Iterable[JsonValue]) -> None:
    """Atomically replace a JSONL file (one compact JSON value per line)."""
    lines = [jsonfmt.dumps(value) + "\n" for value in values]
    atomic_write_text(path, "".join(lines))


def read_jsonl(path: Path) -> Iterator[JsonValue]:
    """Yield the values of a JSONL file; a missing file yields nothing. Blank lines are skipped."""
    if not path.exists():
        return
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                yield jsonfmt.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{number}: invalid JSON line: {exc.msg}") from exc


def append_jsonl(path: Path, value: JsonValue) -> None:
    """Append one value to a JSONL file under an advisory lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = jsonfmt.dumps(value) + "\n"
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.write(line)
            handle.flush()
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
