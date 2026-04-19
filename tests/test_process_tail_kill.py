"""
Unit tests for Phase 4B — /api/process/tail and /api/process/kill.

Same in-process simulation pattern as test_process_list.py: we exercise the
endpoint logic inline without a live HTTP server. Covers:
  • tail: since-cursor slicing, session_key gating, buffer-rollback handling
  • kill: session_key gating, already_exited vs running dispatch
"""
from __future__ import annotations

import os
import sys
import threading
import time
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _find_agent_dir():
    home = Path.home()
    cands = [
        os.getenv('HERMES_WEBUI_AGENT_DIR', ''),
        str(home / '.hermes' / 'hermes-agent'),
        str(Path(__file__).parent.parent.parent / 'hermes-agent-src'),
    ]
    for c in cands:
        if c and (Path(c) / 'tools' / 'process_registry.py').exists():
            return str(Path(c).resolve())
    return None


_AGENT_DIR = _find_agent_dir()
if _AGENT_DIR and _AGENT_DIR not in sys.path:
    sys.path.insert(0, _AGENT_DIR)

requires_agent = pytest.mark.skipif(
    _AGENT_DIR is None,
    reason="hermes-agent source not found",
)


@pytest.fixture
def clean_registry():
    from tools.process_registry import process_registry
    orig_running = dict(process_registry._running)
    orig_finished = dict(getattr(process_registry, "_finished", {}) or {})
    process_registry._running.clear()
    if hasattr(process_registry, "_finished"):
        process_registry._finished.clear()
    yield process_registry
    process_registry._running.clear()
    process_registry._running.update(orig_running)
    if hasattr(process_registry, "_finished"):
        process_registry._finished.clear()
        process_registry._finished.update(orig_finished)


def _fake_session(*, proc_id, session_key, command="sleep 1",
                  output="", exited=False, exit_code=None, pid=1234):
    # include a _lock attribute so the endpoint logic can acquire it
    s = types.SimpleNamespace(
        id=proc_id,
        session_key=session_key,
        command=command,
        pid=pid,
        started_at=time.time() - 5,
        output_buffer=output,
        exited=exited,
        exit_code=exit_code,
        _lock=threading.Lock(),
    )
    return s


# ── simulate /api/process/tail logic ────────────────────────────────────────
def _simulate_tail(session_id: str, proc_id: str, since: int):
    from tools.process_registry import process_registry as _pr
    running = dict(getattr(_pr, "_running", {}) or {})
    finished = dict(getattr(_pr, "_finished", {}) or {})
    sess = running.get(proc_id) or finished.get(proc_id)
    # Uniform 404 for both "unknown" and "wrong session" — matches the
    # endpoint's post-Codex behavior (no existence leak via status code).
    if sess is None or getattr(sess, "session_key", "") != session_id:
        return {"error": "not_found", "status": 404}
    lock = getattr(sess, "_lock", None)
    if lock is not None:
        with lock:
            buf = getattr(sess, "output_buffer", "") or ""
    else:
        buf = getattr(sess, "output_buffer", "") or ""
    length = len(buf)
    if since < 0 or since > length:
        since = 0
    return {
        "proc_id": proc_id,
        "since": since,
        "length": length,
        "output": buf[since:],
        "exited": bool(getattr(sess, "exited", False)),
        "exit_code": getattr(sess, "exit_code", None),
    }


# ── simulate /api/process/kill logic ────────────────────────────────────────
def _simulate_kill(session_id: str, proc_id: str, kill_fn=None):
    from tools.process_registry import process_registry as _pr
    running = dict(getattr(_pr, "_running", {}) or {})
    sess = running.get(proc_id)
    if sess is None:
        finished = dict(getattr(_pr, "_finished", {}) or {})
        ds = finished.get(proc_id)
        if ds is not None and getattr(ds, "session_key", "") == session_id:
            return {
                "status": "already_exited",
                "exit_code": getattr(ds, "exit_code", None),
                "session_id": proc_id,
            }
        return {"error": "not_found", "status": 404}
    # Uniform 404 for cross-session access (matches endpoint's post-Codex fix).
    if getattr(sess, "session_key", "") != session_id:
        return {"error": "not_found", "status": 404}
    fn = kill_fn or _pr.kill_process
    return fn(proc_id)


# ── Tail tests ──────────────────────────────────────────────────────────────

@requires_agent
def test_tail_empty_session_returns_empty_output(clean_registry):
    clean_registry._running["p1"] = _fake_session(
        proc_id="p1", session_key="sid-A", output="")
    r = _simulate_tail("sid-A", "p1", 0)
    assert r["length"] == 0
    assert r["output"] == ""
    assert r["since"] == 0
    assert r["exited"] is False


@requires_agent
def test_tail_returns_full_buffer_from_zero(clean_registry):
    clean_registry._running["p1"] = _fake_session(
        proc_id="p1", session_key="sid-A", output="hello world")
    r = _simulate_tail("sid-A", "p1", 0)
    assert r["length"] == 11
    assert r["output"] == "hello world"
    assert r["since"] == 0


@requires_agent
def test_tail_slices_from_since_cursor(clean_registry):
    clean_registry._running["p1"] = _fake_session(
        proc_id="p1", session_key="sid-A", output="hello world")
    r = _simulate_tail("sid-A", "p1", 6)
    assert r["output"] == "world"
    assert r["since"] == 6
    assert r["length"] == 11


@requires_agent
def test_tail_since_past_end_returns_empty(clean_registry):
    clean_registry._running["p1"] = _fake_session(
        proc_id="p1", session_key="sid-A", output="short")
    # Client cursor ahead of current buffer (e.g. buffer was truncated) →
    # server should reset to 0 and resend full content so client resyncs.
    r = _simulate_tail("sid-A", "p1", 999)
    assert r["since"] == 0
    assert r["output"] == "short"


@requires_agent
def test_tail_cross_session_returns_404_not_403(clean_registry):
    """Post-Codex fix: cross-session access returns the same 404 as unknown
    proc_id so status code doesn't leak process existence."""
    clean_registry._running["p1"] = _fake_session(
        proc_id="p1", session_key="sid-A", output="secret")
    r = _simulate_tail("sid-B", "p1", 0)
    assert r.get("status") == 404
    # And never leaks the buffer
    assert "output" not in r or r.get("output", "") != "secret"


@requires_agent
def test_tail_missing_proc_returns_404(clean_registry):
    r = _simulate_tail("sid-A", "nonexistent", 0)
    assert r.get("status") == 404


@requires_agent
def test_tail_surfaces_exit_status(clean_registry):
    clean_registry._running["p1"] = _fake_session(
        proc_id="p1", session_key="sid-A", output="done",
        exited=True, exit_code=0)
    r = _simulate_tail("sid-A", "p1", 0)
    assert r["exited"] is True
    assert r["exit_code"] == 0


@requires_agent
def test_tail_reads_finished_registry(clean_registry):
    """Once a process moves to _finished (after exit), tail must still read
    its terminal buffer so the drawer can show the final output."""
    if not hasattr(clean_registry, "_finished"):
        pytest.skip("process_registry has no _finished dict on this version")
    clean_registry._finished["p-done"] = _fake_session(
        proc_id="p-done", session_key="sid-A", output="all done\n",
        exited=True, exit_code=0)
    r = _simulate_tail("sid-A", "p-done", 0)
    assert r["output"] == "all done\n"
    assert r["exited"] is True


# ── Kill tests ──────────────────────────────────────────────────────────────

@requires_agent
def test_kill_cross_session_returns_404(clean_registry):
    """Post-Codex fix: uniform 404 instead of 403 for other-session procs."""
    clean_registry._running["p1"] = _fake_session(
        proc_id="p1", session_key="sid-A")
    r = _simulate_kill("sid-B", "p1")
    assert r.get("status") == 404


@requires_agent
def test_kill_missing_returns_404(clean_registry):
    r = _simulate_kill("sid-A", "ghost")
    assert r.get("status") == 404


@requires_agent
def test_kill_already_exited_looks_in_finished(clean_registry):
    if not hasattr(clean_registry, "_finished"):
        pytest.skip("process_registry has no _finished dict on this version")
    clean_registry._finished["p-done"] = _fake_session(
        proc_id="p-done", session_key="sid-A",
        exited=True, exit_code=0)
    r = _simulate_kill("sid-A", "p-done")
    assert r["status"] == "already_exited"
    assert r["exit_code"] == 0


@requires_agent
def test_kill_running_dispatches_kill_fn(clean_registry):
    """When gating passes, endpoint calls kill_process. We don't exercise
    real SIGTERM here — inject a stub kill_fn and assert it was invoked."""
    clean_registry._running["p1"] = _fake_session(
        proc_id="p1", session_key="sid-A")
    calls = []
    stub = lambda proc_id: (calls.append(proc_id) or
                            {"status": "killed", "session_id": proc_id})
    r = _simulate_kill("sid-A", "p1", kill_fn=stub)
    assert calls == ["p1"]
    assert r["status"] == "killed"
