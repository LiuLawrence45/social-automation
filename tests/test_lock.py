import json
import os
import threading
import time

import pytest

from pipeline import lock


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    monkeypatch.delenv(lock.ENV_TOKEN, raising=False)
    monkeypatch.setenv("STUDIO_LOCK", str(tmp_path / "studio.lock"))


def test_lock_records_its_holder_and_is_removed_on_release(tmp_path):
    held = lock.StudioLock(holder="one").acquire()
    owner = json.loads((tmp_path / "studio.lock" / "owner.json").read_text())
    assert owner["holder"] == "one" and owner["pid"] == os.getpid() and owner["started"]
    held.release()
    assert not (tmp_path / "studio.lock").exists()


def test_second_caller_times_out_and_names_the_holder(monkeypatch):
    first = lock.StudioLock(holder="one").acquire()
    monkeypatch.delenv(lock.ENV_TOKEN)
    with pytest.raises(lock.LockTimeout, match="one"):
        lock.StudioLock(holder="two", wait=0.3, poll=0.05).acquire()
    monkeypatch.setenv(lock.ENV_TOKEN, first.token)
    first.release()


def test_second_caller_gets_the_lock_once_the_first_lets_go(monkeypatch):
    first = lock.StudioLock(holder="one").acquire()
    token = first.token
    monkeypatch.delenv(lock.ENV_TOKEN)
    order = []

    def let_go():
        time.sleep(0.2)
        order.append("released")
        first.release()

    thread = threading.Thread(target=let_go)
    thread.start()
    second = lock.StudioLock(holder="two", wait=5, poll=0.05).acquire()
    order.append("acquired")
    thread.join()
    assert order == ["released", "acquired"]
    assert second.owner()["holder"] == "two" and second.token != token
    second.release()


def test_a_child_of_the_holder_passes_through_and_does_not_release(tmp_path):
    outer = lock.StudioLock(holder="outer").acquire()
    inner = lock.StudioLock(holder="inner", wait=0.1).acquire()
    assert inner.inherited
    inner.release()
    assert (tmp_path / "studio.lock").exists()
    outer.release()
    assert not (tmp_path / "studio.lock").exists()


def test_a_lock_left_by_a_dead_process_is_taken_over(tmp_path, monkeypatch):
    path = tmp_path / "studio.lock"
    path.mkdir()
    (path / "owner.json").write_text(json.dumps({"holder": "gone", "pid": 2 ** 22 + 12345, "host": lock.socket.gethostname(), "token": "x"}))
    monkeypatch.setattr(lock, "_alive", lambda pid: False)
    taken = lock.StudioLock(holder="next", wait=0.5, poll=0.05).acquire()
    assert taken.owner()["holder"] == "next"
    taken.release()


def test_an_untouched_lock_goes_stale_and_touch_keeps_it_fresh(tmp_path, monkeypatch):
    first = lock.StudioLock(holder="slow", stale=0.3).acquire()
    monkeypatch.delenv(lock.ENV_TOKEN)
    waiter = lock.StudioLock(holder="two", stale=0.3)
    assert not waiter.is_stale(waiter.owner())
    old = time.time() - 5
    os.utime(tmp_path / "studio.lock" / "owner.json", (old, old))
    assert waiter.is_stale(waiter.owner())
    first.touch()
    assert not waiter.is_stale(waiter.owner())
    monkeypatch.setenv(lock.ENV_TOKEN, first.token)
    first.release()


def test_status_and_break_commands(capsys, monkeypatch):
    assert lock.main(["status"]) == 0
    assert capsys.readouterr().out.strip() == "free"
    held = lock.StudioLock(holder="one").acquire()
    lock.main(["status"])
    assert json.loads(capsys.readouterr().out)["holder"] == "one"
    lock.main(["break"])
    assert "one" in capsys.readouterr().out
    assert not held.path.exists()
