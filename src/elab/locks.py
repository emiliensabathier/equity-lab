"""Cross-process locks: the web server and the timer jobs share one data directory."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows, for local runs only: the deploy target is Linux.
    fcntl = None


@contextmanager
def file_lock(path: Path) -> Iterator[None]:
    """Hold an exclusive flock on ``path`` (created) for the block; a no-op without fcntl."""
    if fcntl is None:
        yield
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
