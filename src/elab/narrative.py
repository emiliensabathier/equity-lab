"""Claude-written commentary on an analysis, generated off the request path.

The text is only shown if it passes check_text: no advice words and no figure that is not in
the engine JSON. Claude is reached through the `claude -p` CLI (subscription), never an API key.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from elab.cache import Cache

log = logging.getLogger(__name__)

UNAVAILABLE_TTL = timedelta(hours=1)
QUEUE_SIZE = 20
ALWAYS_ALLOWED_INTS = frozenset(range(1, 11))
ALWAYS_ALLOWED_YEARS = range(1990, 2101)

RULES = (
    "You write a short commentary on an equity analysis. Write about 250 words in four short "
    "paragraphs: what the price implies; plausibility against history and peers; balance-sheet "
    "points; what would change the reading. Quote only figures present in the JSON. "
    "Never use the words buy, sell, undervalued, overvalued, target. Do not give advice."
)

_FORBIDDEN = re.compile(r"\b(?:buy|sell|undervalued|overvalued|target)(?:s|ing|ed)?\b", re.I)
_NUMBER = re.compile(r"[-+]?\d{1,3}(?:,\d{3})+(?:\.\d+)?|[-+]?\d+(?:\.\d+)?")


class NarrativeError(Exception):
    """The Claude CLI failed, timed out or returned nothing."""


def payload_hash(analysis: dict) -> str:
    """Content key of an analysis; as_of changes on every run and is left out."""
    content = {k: v for k, v in analysis.items() if k != "as_of"}
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


def build_prompt(analysis: dict) -> str:
    return f"{RULES}\n\nJSON:\n{json.dumps(analysis, sort_keys=True)}"


def _walk(node: Any, numbers: list[float], strings: list[str]) -> None:
    if isinstance(node, bool) or node is None:
        return
    if isinstance(node, (int, float)):
        numbers.append(float(node))
    elif isinstance(node, str):
        strings.append(node)
    elif isinstance(node, dict):
        for value in node.values():
            _walk(value, numbers, strings)
    elif isinstance(node, (list, tuple)):
        for value in node:
            _walk(value, numbers, strings)


def check_text(text: str, analysis: dict) -> list[str]:
    """List violations: advice words, and figures absent from the analysis.

    A figure matches a payload value as is or times 100 (fractions are quoted as percentages),
    within half a unit of its last printed decimal; the sign is ignored since prose says
    "fell 2.1%". Numbers that sit inside payload strings (index names, dates) match literally.
    """
    violations = [f"forbidden word: {m.group(0).lower()}" for m in _FORBIDDEN.finditer(text)]
    numbers: list[float] = []
    strings: list[str] = []
    _walk(analysis, numbers, strings)
    literals = {
        m.group(0).lstrip("+-").replace(",", "") for s in strings for m in _NUMBER.finditer(s)
    }
    for m in _NUMBER.finditer(text):
        digits = m.group(0).lstrip("+-").replace(",", "")
        x = float(digits)
        decimals = len(digits.split(".")[1]) if "." in digits else 0
        tol = 0.5 * 10**-decimals + 1e-12
        if decimals == 0 and (x in ALWAYS_ALLOWED_INTS or int(x) in ALWAYS_ALLOWED_YEARS):
            continue
        if digits in literals:
            continue
        if any(abs(x - abs(v)) <= tol or abs(x - 100 * abs(v)) <= tol for v in numbers):
            continue
        violations.append(f"number not in data: {m.group(0)}")
    return violations


class ClaudeRunner:
    def __init__(self, timeout_s: int = 240, run: Callable[..., Any] = subprocess.run) -> None:
        self._timeout_s = timeout_s
        self._run = run

    def __call__(self, prompt: str) -> str:
        # The key is stripped so the CLI uses the logged-in subscription, never metered billing.
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        binary = shutil.which("claude") or "claude"  # claude.cmd on Windows
        try:
            proc = self._run(
                [binary, "-p", "--model", "sonnet"],
                input=prompt,
                text=True,
                capture_output=True,
                timeout=self._timeout_s,
                env=env,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise NarrativeError(f"claude CLI failed: {type(exc).__name__}") from exc
        if proc.returncode != 0:
            raise NarrativeError(f"claude CLI exited with code {proc.returncode}")
        text = (proc.stdout or "").strip()
        if not text:
            raise NarrativeError("claude CLI returned empty output")
        return text


class NarrativeWorker:
    """One daemon thread turning analyses into checked narratives, cached by content hash."""

    def __init__(self, cache: Cache, runner: Callable[[str], str]) -> None:
        self._cache = cache
        self._runner = runner
        self._queue: queue.Queue[tuple[str, dict]] = queue.Queue(maxsize=QUEUE_SIZE)
        self._inflight: set[str] = set()
        self._lock = threading.Lock()
        threading.Thread(target=self._loop, daemon=True, name="narrative-worker").start()

    @staticmethod
    def _key(analysis: dict) -> str:
        return f"narrative:{payload_hash(analysis)}"

    def get(self, analysis: dict) -> dict | None:
        hit = self._cache.get(self._key(analysis), None)
        if hit is None:
            return None
        value, stored_at = hit
        expired = datetime.now(UTC) - stored_at > UNAVAILABLE_TTL
        if value.get("status") == "unavailable" and expired:
            return None
        return value

    def request(self, analysis: dict) -> str:
        cached = self.get(analysis)
        if cached is not None:
            return "ready" if cached["status"] == "ready" else "unavailable"
        digest = payload_hash(analysis)
        with self._lock:
            if digest in self._inflight:
                return "pending"
            try:
                self._queue.put_nowait((digest, analysis))
            except queue.Full:
                return "unavailable"
            self._inflight.add(digest)
        return "pending"

    def drain(self) -> None:
        """Block until every queued job is finished (for tests)."""
        self._queue.join()

    def _loop(self) -> None:
        while True:
            digest, analysis = self._queue.get()
            try:
                self._cache.set(self._key(analysis), self._generate(analysis))
            except Exception:  # noqa: BLE001 - the thread must outlive any single job
                log.exception("narrative job crashed")
            finally:
                with self._lock:
                    self._inflight.discard(digest)
                self._queue.task_done()

    def _generate(self, analysis: dict) -> dict:
        prompt = build_prompt(analysis)
        try:
            text = self._runner(prompt)
            violations = check_text(text, analysis)
            if violations:
                retry = prompt + "\n\nYour previous text broke these rules, fix them:\n"
                text = self._runner(retry + "\n".join(violations))
                violations = check_text(text, analysis)
        except Exception as exc:  # noqa: BLE001 - any runner failure means "unavailable"
            log.warning("narrative runner failed: %s", type(exc).__name__)
            return {"status": "unavailable"}
        if violations:
            return {"status": "unavailable"}
        return {"status": "ready", "text": text}
