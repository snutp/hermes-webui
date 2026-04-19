"""
Unit tests for Phase 4A — /api/process/list endpoint logic.

We don't spin up the full HTTP server; we exercise the same filter +
projection logic that routes.py runs inline when the request hits. Covers:
  • session_key filter (other session's procs not leaked)
  • empty session returns empty list
  • output_buffer tail is capped at 500 chars
  • tasks sorted by started_at desc (newest first)
"""
from __future__ import annotations

import os
import sys
import time
import types
from pathlib import Path
from unittest.mock import patch

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
    # Snapshot original state to avoid cross-test pollution
    original_running = dict(process_registry._running)
    process_registry._running.clear()
    yield process_registry
    process_registry._running.clear()
    process_registry._running.update(original_running)


def _fake_session(*, proc_id, session_key, command="sleep 1",
                  pid=1234, started_at=None, output="",
                  notify=False, watch_patterns=None):
    return types.SimpleNamespace(
        id=proc_id,
        session_key=session_key,
        command=command,
        pid=pid,
        started_at=started_at if started_at is not None else time.time() - 5,
        output_buffer=output,
        notify_on_complete=notify,
        watch_patterns=watch_patterns or [],
    )


def _simulate_endpoint(session_id: str):
    """Replicate the inline logic from routes.py /api/process/list so we can
    test it without an HTTP server."""
    from tools.process_registry import process_registry as _pr
    try:
        from tools.ansi_strip import strip_ansi
    except ImportError:
        def strip_ansi(s):
            return s
    now_ts = time.time()
    running = dict(getattr(_pr, "_running", {}) or {})
    tasks = []
    for proc_id, sess in running.items():
        if getattr(sess, "session_key", "") != session_id:
            continue
        started = getattr(sess, "started_at", 0.0) or 0.0
        runtime = max(now_ts - started, 0.0) if started else 0.0
        tail_raw = getattr(sess, "output_buffer", "") or ""
        tail = strip_ansi(tail_raw[-500:])
        tasks.append({
            "proc_id": proc_id,
            "command": getattr(sess, "command", ""),
            "pid": getattr(sess, "pid", None),
            "started_at": started,
            "runtime_sec": round(runtime, 1),
            "notify_on_complete": bool(getattr(sess, "notify_on_complete", False)),
            "watch_patterns": list(getattr(sess, "watch_patterns", []) or []),
            "tail": tail,
        })
    tasks.sort(key=lambda t: t.get("started_at") or 0.0, reverse=True)
    return {"session_id": session_id, "tasks": tasks}


# ── Tests ─────────────────────────────────────────────────────────────────

@requires_agent
def test_empty_session_returns_no_tasks(clean_registry):
    result = _simulate_endpoint("sid-empty")
    assert result == {"session_id": "sid-empty", "tasks": []}


@requires_agent
def test_only_own_session_leaks_through(clean_registry):
    """A process registered for a different session_key must not appear."""
    clean_registry._running["proc_mine"] = _fake_session(
        proc_id="proc_mine", session_key="sid-A", command="sleep 30")
    clean_registry._running["proc_other"] = _fake_session(
        proc_id="proc_other", session_key="sid-B", command="sleep 30")

    r = _simulate_endpoint("sid-A")
    assert len(r["tasks"]) == 1
    assert r["tasks"][0]["proc_id"] == "proc_mine"


@requires_agent
def test_tail_is_capped_at_500_chars(clean_registry):
    long_out = "A" * 2000 + "B" * 1500 + "ZZZ"  # 3503 chars total
    clean_registry._running["proc_big"] = _fake_session(
        proc_id="proc_big", session_key="sid-A", output=long_out)
    r = _simulate_endpoint("sid-A")
    tail = r["tasks"][0]["tail"]
    assert len(tail) == 500
    assert tail.endswith("ZZZ")


@requires_agent
def test_tasks_sorted_newest_first(clean_registry):
    now = time.time()
    clean_registry._running["proc_old"] = _fake_session(
        proc_id="proc_old", session_key="sid-A", started_at=now - 120)
    clean_registry._running["proc_mid"] = _fake_session(
        proc_id="proc_mid", session_key="sid-A", started_at=now - 30)
    clean_registry._running["proc_new"] = _fake_session(
        proc_id="proc_new", session_key="sid-A", started_at=now - 2)
    r = _simulate_endpoint("sid-A")
    assert [t["proc_id"] for t in r["tasks"]] == ["proc_new", "proc_mid", "proc_old"]


@requires_agent
def test_runtime_and_flags_projected(clean_registry):
    now = time.time()
    clean_registry._running["proc_feat"] = _fake_session(
        proc_id="proc_feat", session_key="sid-A",
        command="bun run build", pid=5555, started_at=now - 42,
        notify=True, watch_patterns=["ERROR", "DONE"], output="last line\n")
    r = _simulate_endpoint("sid-A")
    t = r["tasks"][0]
    assert t["proc_id"] == "proc_feat"
    assert t["command"] == "bun run build"
    assert t["pid"] == 5555
    assert t["notify_on_complete"] is True
    assert t["watch_patterns"] == ["ERROR", "DONE"]
    assert t["tail"].endswith("last line\n")
    # runtime is round(now - started_at, 1); allow slight drift
    assert 40.0 <= t["runtime_sec"] <= 50.0


@requires_agent
def test_ansi_stripped_from_tail(clean_registry):
    colored = "\x1b[31mRED\x1b[0m\n\x1b[32mGREEN\x1b[0m"
    clean_registry._running["proc_color"] = _fake_session(
        proc_id="proc_color", session_key="sid-A", output=colored)
    r = _simulate_endpoint("sid-A")
    tail = r["tasks"][0]["tail"]
    assert "\x1b" not in tail
    assert "RED" in tail and "GREEN" in tail
