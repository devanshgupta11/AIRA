"""The Gemini API keys, and failing over between them when one is rate-limited.

Every agent, in both frameworks, reaches Gemini through this pool. One key is in use at a time. When
Gemini refuses a call because that key hit its rate limit (HTTP 429 / RESOURCE_EXHAUSTED), the key
goes into a cooldown and the same call is re-sent on the next key. A key whose API key is rejected
outright (invalid, revoked, wrong project) is set aside for the life of the process, so one bad
paste cannot stall every investigation.

**Quotas are per Google Cloud project, not per key.** Two keys created in the same project share one
quota, so failing over between them gains nothing. Each key in .env must come from a different
project (in AI Studio: "Create API key" -> "Create API key in new project").

**The cooldown.** A 429 response usually says how long to wait ("retryDelay": "23s"). That figure is
used when it is present, and AIRA_LLM_COOLDOWN_S (default 60 s) otherwise. A refusal on a per-day
quota means the key is finished until Google resets the quota, so it rests for AIRA_LLM_DAY_COOLDOWN_S
(default one hour) before it is tried again.

**When every key is cooling down,** the call waits for the soonest one to come back, up to
AIRA_LLM_MAX_WAIT_S (default 60 s), and otherwise fails with an error saying exactly that. It fails
rather than waiting indefinitely, because each agent runs under the incident's own timeout and the
incident should end with an honest error, not a hang.

Each process has its own pool: the backend, which runs the CrewAI agents, and the RCA agent's
process, which runs ADK behind A2A. A key that is rate-limited in one process is detected
separately by the other the first time it is used there.

Every failover is recorded through the event recorder, the same callback trace that holds each
agent's model calls, so it appears in the incident's execution trace. Only the last four characters
of a key are ever written anywhere.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
import time
from typing import Callable, TypeVar

from . import config  # noqa: F401  (loads backend/.env before the keys are read)
from . import events

log = logging.getLogger("aira.llm_keys")

T = TypeVar("T")

COOLDOWN_S = float(os.getenv("AIRA_LLM_COOLDOWN_S", "60"))
DAY_COOLDOWN_S = float(os.getenv("AIRA_LLM_DAY_COOLDOWN_S", "3600"))
MAX_WAIT_S = float(os.getenv("AIRA_LLM_MAX_WAIT_S", "60"))


class NoKeyAvailable(RuntimeError):
    """No configured key can take a call: none set, all rejected, or all cooling for too long."""


def load_keys() -> list[tuple[str, str]]:
    """GEMINI_API_KEY_1, GEMINI_API_KEY_2, ... in numeric order, blanks and duplicates dropped.

    Any number of keys works. A plain GEMINI_API_KEY is accepted too, after the numbered ones, so a
    single-key setup needs no renaming. Returns (variable name, key) pairs.
    """
    numbered = []
    for name, value in os.environ.items():
        m = re.fullmatch(r"GEMINI_API_KEY_(\d+)", name)
        if m and value.strip():
            numbered.append((int(m.group(1)), name, value.strip()))
    keys = [(name, v) for _, name, v in sorted(numbered)]
    single = os.getenv("GEMINI_API_KEY", "").strip()
    if single:
        keys.append(("GEMINI_API_KEY", single))
    seen, out = set(), []
    for name, k in keys:
        if k not in seen:
            seen.add(k)
            out.append((name, k))
    return out


# ------------------------------------------------------------------ classifying a failure

def _chain(exc: BaseException):
    """The exception and everything it wraps: CrewAI and LiteLLM both re-raise provider errors."""
    seen, stack = set(), [exc]
    while stack:
        e = stack.pop()
        if e is None or id(e) in seen:
            continue
        seen.add(id(e))
        yield e
        stack.extend([e.__cause__, e.__context__])
        stack.extend(getattr(e, "exceptions", None) or [])   # ExceptionGroup


def classify(exc: BaseException) -> str | None:
    """'rate_limit', 'invalid_key', or None for a failure that has nothing to do with the key.

    A failure that is not the key's fault (a timeout, a malformed answer, the model overloaded)
    returns None and is re-raised unchanged. Switching keys would not help it, and doing so would
    hide the real error.
    """
    for e in _chain(exc):
        code = getattr(e, "code", None)
        status = getattr(e, "status_code", None)
        text = f"{type(e).__name__} {e}".lower()
        if 429 in (code, status) or "resource_exhausted" in text or type(e).__name__ == "RateLimitError":
            return "rate_limit"
        if "429" in text and ("quota" in text or "rate" in text or "exhausted" in text):
            return "rate_limit"
        if ("api_key_invalid" in text or "api key not valid" in text or "api key expired" in text
                or "permission_denied" in text or 401 in (code, status) or 403 in (code, status)):
            return "invalid_key"
    return None


def _cooldown_for(exc: BaseException) -> tuple[float, str]:
    text = " ".join(str(e) for e in _chain(exc))
    if re.search(r"per ?day", text, re.I):
        return DAY_COOLDOWN_S, "daily quota exhausted"
    m = (re.search(r"retry_?delay['\"]?\s*[:=]\s*['\"]?(\d+(?:\.\d+)?)s", text, re.I)
         or re.search(r"retry in (\d+(?:\.\d+)?)\s*s", text, re.I))
    if m:
        # One second of margin: Google's figure is when the window reopens, not after it has.
        return float(m.group(1)) + 1.0, "per-minute limit (wait taken from Gemini's reply)"
    return COOLDOWN_S, "rate limit"


# ------------------------------------------------------------------ the pool

class KeyPool:
    def __init__(self, keys: list[tuple[str, str]]):
        self._names = [name for name, _ in keys]
        self._keys = [k for _, k in keys]
        self._cool_until = [0.0] * len(keys)
        self._invalid = [False] * len(keys)
        self._current = 0
        self._lock = threading.Lock()   # the CrewAI investigators run in parallel threads

    def __len__(self) -> int:
        return len(self._keys)

    def label(self, idx: int) -> str:
        """How a key is named in logs and traces: its .env name and last four characters only."""
        return f"{self._names[idx]} (...{self._keys[idx][-4:]})"

    def _take(self) -> tuple[int, str] | float:
        """The key to use now, or the seconds until one will be free."""
        with self._lock:
            if not self._keys:
                raise NoKeyAvailable(
                    "no Gemini API key is configured: set GEMINI_API_KEY_1 (and more) in backend/.env")
            usable = [i for i in range(len(self._keys)) if not self._invalid[i]]
            if not usable:
                raise NoKeyAvailable("every configured Gemini API key was rejected as invalid")
            now = time.monotonic()
            n = len(self._keys)
            # Stay on the current key while it works; move forward from it when it does not.
            for step in range(n):
                i = (self._current + step) % n
                if not self._invalid[i] and self._cool_until[i] <= now:
                    self._current = i
                    return i, self._keys[i]
            return min(self._cool_until[i] for i in usable) - now

    def acquire(self, deadline: float) -> tuple[int, str]:
        """A key for one call, waiting (blocking) for a cooldown to end if every key is cooling."""
        while True:
            got = self._take()
            if isinstance(got, tuple):
                return got
            self._wait_or_give_up(got, deadline)
            time.sleep(got)

    async def acquire_async(self, deadline: float) -> tuple[int, str]:
        """The same, for the ADK agent's event loop: waits without blocking it."""
        while True:
            got = self._take()
            if isinstance(got, tuple):
                return got
            self._wait_or_give_up(got, deadline)
            await asyncio.sleep(got)

    def _wait_or_give_up(self, wait: float, deadline: float) -> None:
        if time.monotonic() + wait > deadline:
            raise NoKeyAvailable(
                f"all {len(self._keys)} Gemini API keys are rate-limited; the soonest is free again "
                f"in {wait:.0f}s, past the {MAX_WAIT_S:.0f}s a call may wait for one (AIRA_LLM_MAX_WAIT_S)")
        log.warning("all Gemini keys cooling down; waiting %.1fs for the next one", wait)
        events.record("aira", "llm_keys_all_cooling", ok=False,
                      detail={"wait_s": round(wait, 1), "keys": len(self._keys)})

    def report(self, idx: int, kind: str, exc: BaseException) -> None:
        """Take a key out of rotation after Gemini refused it, and move on to the next."""
        with self._lock:
            if kind == "invalid_key":
                self._invalid[idx] = True
                why, cooldown = "key rejected as invalid; not used again until restart", None
            else:
                cooldown, why = _cooldown_for(exc)
                self._cool_until[idx] = time.monotonic() + cooldown
            self._current = (idx + 1) % len(self._keys)
            nxt = self.label(self._current)
        log.warning("Gemini %s: %s; failing over to %s", self.label(idx), why, nxt)
        events.record("aira", "llm_key_failover", ok=False, detail={
            "key": self.label(idx), "reason": why, "next": nxt,
            "cooldown_s": None if cooldown is None else round(cooldown, 1),
            "error": events.clip(str(exc), 300)})

    def status(self) -> dict:
        """For /health. Reads local state only: it makes no call to Gemini and uses no quota."""
        with self._lock:
            now = time.monotonic()
            cooling = {self.label(i): round(t - now) for i, t in enumerate(self._cool_until)
                       if t > now and not self._invalid[i]}
            invalid = [self.label(i) for i, bad in enumerate(self._invalid) if bad]
            ready = len(self._keys) - len(cooling) - len(invalid)
            current = self.label(self._current) if self._keys else None
        return {"keys": len(self._keys), "ready": ready, "current": current,
                "cooling_s": cooling, "invalid": invalid}


POOL = KeyPool(load_keys())


def call_with_failover(fn: Callable[[str], T]) -> T:
    """Run fn(api_key), moving to the next key whenever Gemini refuses the current one.

    Used by the CrewAI agents. Anything that is not a key problem propagates unchanged.
    """
    deadline = time.monotonic() + MAX_WAIT_S
    while True:
        idx, key = POOL.acquire(deadline)
        try:
            return fn(key)
        except Exception as e:  # noqa: BLE001 - re-raised unless it is the key's fault
            kind = classify(e)
            if kind is None:
                raise
            POOL.report(idx, kind, e)
