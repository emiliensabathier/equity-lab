import sys
import threading

import pytest

from elab import locks
from elab.locks import file_lock


@pytest.mark.skipif(sys.platform == "win32", reason="fcntl.flock exists only on POSIX")
def test_two_holders_of_one_lock_file_take_turns(tmp_path):
    path = tmp_path / "sub" / "engine.lock"
    first_in, release, second_in = threading.Event(), threading.Event(), threading.Event()

    def first():
        with file_lock(path):
            first_in.set()
            release.wait(5)

    def second():
        first_in.wait(5)
        with file_lock(path):  # its own open(), so its own file description, as across processes
            second_in.set()

    threads = [threading.Thread(target=first), threading.Thread(target=second)]
    for thread in threads:
        thread.start()
    assert first_in.wait(5)
    assert not second_in.wait(0.3)
    release.set()
    assert second_in.wait(5)
    for thread in threads:
        thread.join(5)


def test_without_fcntl_the_lock_is_a_no_op(tmp_path, monkeypatch):
    monkeypatch.setattr(locks, "fcntl", None)
    with file_lock(tmp_path / "x.lock"), file_lock(tmp_path / "x.lock"):
        pass
