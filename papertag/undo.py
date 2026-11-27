"""Undo a logged papertag run: restore metadata and filenames (--undo LOG)."""

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

from papertag.core.metadata_updater import MetadataUpdater

log = logging.getLogger(__name__)


class UndoLogError(Exception):
    """The log can't be read or isn't a papertag session log."""


@dataclass
class UndoResult:
    """What happened to one logged file."""
    path: str      # where the file is now (the original path if restored)
    restored: bool
    message: str


def read_log(log_path: str) -> List[Dict[str, Any]]:
    """The per-file entries of a session log written with --log.

    Raises:
        UndoLogError: Missing, unreadable or not a session log.
    """
    try:
        data = json.loads(Path(log_path).read_text(encoding='utf-8'))
    except (OSError, ValueError) as e:
        raise UndoLogError(f"Cannot read log {log_path}: {e}") from e
    results = data.get('results') if isinstance(data, dict) else None
    if not isinstance(results, list):
        raise UndoLogError(f"{log_path} is not a papertag session log")
    return results


def undo_entry(entry: Dict[str, Any], updater: MetadataUpdater) -> UndoResult:
    """Restore one file a run changed, if it is still as that run left it.

    Checks before touching anything: the file is where the run put it, its
    title and authors are what the run wrote (else someone changed it since),
    and nothing else has taken its old name.

    Args:
        entry: A `success` entry of the session log.
        updater: Does the restore.

    Returns:
        The outcome; nothing is changed unless `restored`.
    """
    original = entry.get('original_path', '')
    current = entry.get('new_path') or str(Path(original).with_name(entry.get('new_filename', '')))
    previous, written = entry.get('previous'), entry.get('written')
    if not previous or not written:
        return UndoResult(current, False,
                          f"{Path(original).name}: the log has no record of the previous "
                          "metadata (written by an older papertag?)")
    if not os.path.exists(current):
        return UndoResult(current, False, f"{current}: not found (moved, or already undone?)")
    moved = os.path.abspath(current) != os.path.abspath(original)
    if moved and os.path.exists(original):
        return UndoResult(current, False,
                          f"{Path(current).name}: can't rename back, {original} already exists")
    now = updater.read_metadata(current)
    if (now.title, now.authors) != (written.get('title'), written.get('authors')):
        return UndoResult(current, False,
                          f"{Path(current).name}: metadata changed since the run; left as is")

    updater.restore(current, previous)
    if moved:
        updater.rename_file(current, Path(original).name)
    return UndoResult(original, True, f"Restored {Path(original).name}")


def undo_log(log_path: str, updater: MetadataUpdater) -> List[UndoResult]:
    """Undo every file a logged run changed, last first.

    Last first, so that if one run renamed two files in turn to the same
    name (the second got " (1)"), each finds its old name free again.
    Dry-run, skipped and failed entries changed nothing and are ignored.

    Raises:
        UndoLogError: The log can't be read.
    """
    results = []
    for entry in reversed(read_log(log_path)):
        if not isinstance(entry, dict) or entry.get('status') != 'success':
            continue
        try:
            results.append(undo_entry(entry, updater))
        except Exception as e:  # one bad file shouldn't stop the rest
            results.append(UndoResult(entry.get('new_path', ''), False,
                                      f"{entry.get('new_filename', '?')}: {e}"))
    return results
