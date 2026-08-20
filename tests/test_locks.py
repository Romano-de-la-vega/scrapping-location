from __future__ import annotations

import pytest

from watcher.locks import AlreadyRunningError, GlobalLock


def test_global_lock_blocks_a_second_owner(tmp_path) -> None:
    first = GlobalLock(tmp_path / "watcher.lock")
    second = GlobalLock(tmp_path / "watcher.lock")
    first.acquire()
    try:
        with pytest.raises(AlreadyRunningError):
            second.acquire()
    finally:
        first.release()

    second.acquire()
    second.release()
