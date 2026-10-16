import sqlite3
import subprocess
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from elab.cache import Cache
from elab.narrative import (
    ClaudeRunner,
    NarrativeError,
    NarrativeWorker,
    build_prompt,
    check_text,
    payload_hash,
)

ANALYSIS = {
    "as_of": "2026-10-17T10:00:00+00:00",
    "ticker": "ACME",
    "benchmark": "S&P 500",
    "price": 1234.56,
    "ret": {"upside": 0.0915, "drawdown": -0.021, "history": [0.5, None, True]},
}


def test_payload_hash_ignores_as_of():
    other = {**ANALYSIS, "as_of": "later"}
    assert payload_hash(ANALYSIS) == payload_hash(other)
    assert payload_hash(ANALYSIS) != payload_hash({**ANALYSIS, "price": 1.5})


def test_build_prompt_contains_rules_and_json():
    prompt = build_prompt(ANALYSIS)
    assert "Never use the words buy, sell, undervalued, overvalued, target" in prompt
    assert '"ticker": "ACME"' in prompt


@pytest.mark.parametrize(
    "text",
    [
        "Upside 9.2%.",
        "Upside 9.15%.",
        "It fell -2.1% and 2.1%.",
        "Price 1,234.56, 50% of the range.",
        "Against the S&P 500 in 2024.",
        "Over 3 years, 7 peers.",
    ],
)
def test_check_text_accepts_rounded_percentages(text):
    assert check_text(text, ANALYSIS) == []


def test_check_text_rejects_invented_number():
    out = check_text("Upside is 12.4% today.", ANALYSIS)
    assert out and "12.4" in out[0]


@pytest.mark.parametrize("word", ["buy", "Sell", "UNDERVALUED", "overvalued", "target",
                                  "buying", "sells", "targets"])
def test_check_text_rejects_forbidden_word(word):
    assert any("forbidden" in v for v in check_text(f"This is a {word} case.", ANALYSIS))


def test_check_text_does_not_flag_substrings():
    assert check_text("The buyout, targeted and retargetable.", ANALYSIS) == []


def test_check_text_rejects_overvaluation():
    assert any("forbidden" in v for v in check_text("An overvaluation signal.", ANALYSIS))


GOOD = "Upside is 9.2% over 5 years."


def make_worker(tmp_path, runner):
    return NarrativeWorker(Cache(tmp_path / "c.db"), runner)


def test_worker_serves_ready_text(tmp_path):
    worker = make_worker(tmp_path, lambda p: GOOD)
    assert worker.get(ANALYSIS) is None
    assert worker.request(ANALYSIS) == "pending"
    worker.drain()
    assert worker.get(ANALYSIS) == {"status": "ready", "text": GOOD}
    assert worker.request(ANALYSIS) == "ready"


def test_worker_dedups_same_payload(tmp_path):
    gate, calls = threading.Event(), []

    def runner(prompt):
        gate.wait(5)
        calls.append(prompt)
        return GOOD

    worker = make_worker(tmp_path, runner)
    assert worker.request(ANALYSIS) == "pending"
    assert worker.request({**ANALYSIS, "as_of": "x"}) == "pending"
    gate.set()
    worker.drain()
    assert len(calls) == 1


def test_worker_retries_once_then_marks_unavailable(tmp_path):
    prompts = []

    def runner(prompt):
        prompts.append(prompt)
        return "You should buy it."

    worker = make_worker(tmp_path, runner)
    worker.request(ANALYSIS)
    worker.drain()
    assert len(prompts) == 2
    assert "forbidden word: buy" in prompts[1]
    assert worker.get(ANALYSIS) == {"status": "unavailable"}
    assert worker.request(ANALYSIS) == "unavailable"
    assert len(prompts) == 2


def test_worker_retry_can_succeed(tmp_path):
    texts = iter(["Invented 99.9%.", GOOD])
    worker = make_worker(tmp_path, lambda p: next(texts))
    worker.request(ANALYSIS)
    worker.drain()
    assert worker.get(ANALYSIS)["status"] == "ready"


def test_worker_narrative_error_is_unavailable(tmp_path):
    def runner(prompt):
        raise NarrativeError("down")

    worker = make_worker(tmp_path, runner)
    worker.request(ANALYSIS)
    worker.drain()
    assert worker.get(ANALYSIS) == {"status": "unavailable"}


def test_worker_unavailable_expires_after_one_hour(tmp_path):
    calls = []

    def runner(prompt):
        calls.append(prompt)
        raise NarrativeError("down")

    worker = make_worker(tmp_path, runner)
    worker.request(ANALYSIS)
    worker.drain()
    old = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    with sqlite3.connect(tmp_path / "c.db") as conn:
        conn.execute("UPDATE entries SET stored_at = ?", (old,))
    assert worker.get(ANALYSIS) is None
    assert worker.request(ANALYSIS) == "pending"
    worker.drain()
    assert len(calls) == 2


def test_worker_survives_runner_crash_and_serves_next_job(tmp_path):
    def runner(prompt):
        if '"ticker": "BAD"' in prompt:
            raise RuntimeError("boom")
        return GOOD

    worker = make_worker(tmp_path, runner)
    worker.request({**ANALYSIS, "ticker": "BAD"})
    worker.request(ANALYSIS)
    worker.drain()
    assert worker.get({**ANALYSIS, "ticker": "BAD"}) == {"status": "unavailable"}
    assert worker.get(ANALYSIS)["status"] == "ready"


def test_worker_full_queue_is_unavailable_and_stores_nothing(tmp_path):
    gate = threading.Event()
    worker = make_worker(tmp_path, lambda p: gate.wait(5) and GOOD)
    results = [worker.request({**ANALYSIS, "n": i}) for i in range(25)]
    overflow = {**ANALYSIS, "n": 24}
    assert "unavailable" in results
    assert results[-1] == "unavailable"
    assert worker.get(overflow) is None
    gate.set()
    worker.drain()
    assert worker.get(overflow) is None


def test_runner_strips_api_key_from_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok-secret")
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"], seen["kwargs"] = cmd, kwargs
        return SimpleNamespace(returncode=0, stdout=" hello \n", stderr="")

    assert ClaudeRunner(timeout_s=7, run=fake_run)("the prompt") == "hello"
    kw = seen["kwargs"]
    assert "ANTHROPIC_API_KEY" not in kw["env"]
    assert "ANTHROPIC_AUTH_TOKEN" not in kw["env"]
    assert "PATH" in kw["env"]
    assert kw["input"] == "the prompt" and kw["timeout"] == 7
    assert kw["text"] and kw["capture_output"]
    assert seen["cmd"][1:] == ["-p", "--model", "sonnet"]


@pytest.mark.parametrize("proc", [
    SimpleNamespace(returncode=1, stdout="x", stderr="e"),
    SimpleNamespace(returncode=0, stdout="  \n", stderr=""),
])
def test_runner_raises_on_failure_or_empty(proc):
    with pytest.raises(NarrativeError):
        ClaudeRunner(run=lambda cmd, **kw: proc)("p")


def test_runner_raises_on_timeout():
    def fake_run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 1)

    with pytest.raises(NarrativeError):
        ClaudeRunner(run=fake_run)("p")


def test_failing_rerun_never_overwrites_ready(tmp_path):
    def runner(prompt):
        raise NarrativeError("down")

    worker = make_worker(tmp_path, runner)
    cache = Cache(tmp_path / "c.db")
    cache.set(f"narrative:{payload_hash(ANALYSIS)}", {"status": "ready", "text": GOOD})
    worker._queue.put((payload_hash(ANALYSIS), ANALYSIS))
    worker.drain()
    assert worker.get(ANALYSIS) == {"status": "ready", "text": GOOD}


def test_request_rechecks_cache_inside_lock(tmp_path, monkeypatch):
    calls = []
    worker = make_worker(tmp_path, lambda p: calls.append(p) or GOOD)
    Cache(tmp_path / "c.db").set(
        f"narrative:{payload_hash(ANALYSIS)}", {"status": "ready", "text": GOOD}
    )
    real_get = worker.get
    first = []

    def racy_get(analysis):  # the first, unlocked read misses: the job just finished
        if not first:
            first.append(1)
            return None
        return real_get(analysis)

    monkeypatch.setattr(worker, "get", racy_get)
    assert worker.request(ANALYSIS) == "ready"
    worker.drain()
    assert calls == []


def test_bad_payload_stores_unavailable(tmp_path, monkeypatch):
    def boom(analysis):
        raise TypeError("bad payload")

    monkeypatch.setattr("elab.narrative.build_prompt", boom)
    worker = make_worker(tmp_path, lambda p: GOOD)
    assert worker.request(ANALYSIS) == "pending"
    worker.drain()
    assert worker.get(ANALYSIS) == {"status": "unavailable"}
    assert worker.request(ANALYSIS) == "unavailable"
