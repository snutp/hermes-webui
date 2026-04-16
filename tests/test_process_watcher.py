"""
Unit tests for api/process_watcher.py — Phase 1 of webui auto-resume.

Scope:
  • drain_pending_watchers filters by platform="webui" AND session_key match
  • _format_synthetic_completion produces the expected [SYSTEM: ...] text
  • _run_process_watcher_webui honors notify_on_complete + is_completion_consumed

These are pure unit tests: process_registry is stubbed so no real processes
are spawned. The integration tests (test_process_watcher_integration.py) use
the real registry + real AIAgent with an LLM stub.
"""
import os
import sys
import time
import threading
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ── sys.path for hermes-agent modules ──────────────────────────────────────
# process_watcher.py imports `tools.process_registry` at call time; make sure
# the module resolves. Mirror conftest._discover_agent_dir's candidate list
# and fall back to the sibling submodule path used in this monorepo.
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

# Also ensure the webui package itself is importable (api.process_watcher)
_WEBUI_ROOT = str(Path(__file__).parent.parent.resolve())
if _WEBUI_ROOT not in sys.path:
    sys.path.insert(0, _WEBUI_ROOT)

requires_agent = pytest.mark.skipif(
    _AGENT_DIR is None,
    reason="hermes-agent source not found — skipping process_watcher unit tests",
)


# ── Fixtures ───────────────────────────────────────────────────────────────

@pytest.fixture
def clean_registry():
    """Reset process_registry.pending_watchers between tests."""
    from tools.process_registry import process_registry
    process_registry.pending_watchers.clear()
    yield process_registry
    process_registry.pending_watchers.clear()


def _make_watcher(session_id="proc_abc", platform="webui",
                  session_key="websid123", notify=True):
    return {
        "session_id": session_id,
        "check_interval": 5,
        "session_key": session_key,
        "platform": platform,
        "chat_id": session_key,
        "user_id": "",
        "user_name": "",
        "thread_id": "",
        "notify_on_complete": notify,
    }


# ── drain_pending_watchers ─────────────────────────────────────────────────

@requires_agent
def test_drain_claims_only_webui_watchers(clean_registry):
    """Watchers with platform != 'webui' must be left in the queue."""
    from api.process_watcher import drain_pending_watchers

    clean_registry.pending_watchers.extend([
        _make_watcher(platform="webui", session_key="sid-1"),
        _make_watcher(platform="slack", session_key="sid-1"),
        _make_watcher(platform="telegram", session_key="sid-1"),
    ])

    # Stub the spawn side so we don't start real threads.
    with patch("api.process_watcher._run_process_watcher_webui"):
        with patch("threading.Thread") as mock_thread:
            count = drain_pending_watchers(
                session_id="sid-1", model="m", workspace="/tmp", chain_depth=0,
            )

    assert count == 1
    assert mock_thread.call_count == 1
    # Non-webui watchers preserved in original order
    remaining = clean_registry.pending_watchers
    assert len(remaining) == 2
    assert remaining[0]["platform"] == "slack"
    assert remaining[1]["platform"] == "telegram"


@requires_agent
def test_drain_claims_only_own_session(clean_registry):
    """Webui watchers belonging to a different session_key must stay."""
    from api.process_watcher import drain_pending_watchers

    clean_registry.pending_watchers.extend([
        _make_watcher(session_id="p1", platform="webui", session_key="sid-A"),
        _make_watcher(session_id="p2", platform="webui", session_key="sid-B"),
        _make_watcher(session_id="p3", platform="webui", session_key="sid-A"),
    ])

    with patch("api.process_watcher._run_process_watcher_webui"):
        with patch("threading.Thread") as mock_thread:
            count = drain_pending_watchers(
                session_id="sid-A", model="m", workspace="/tmp", chain_depth=0,
            )

    assert count == 2, "Should claim the two sid-A watchers"
    assert mock_thread.call_count == 2
    # sid-B watcher remains
    remaining = clean_registry.pending_watchers
    assert len(remaining) == 1
    assert remaining[0]["session_id"] == "p2"
    assert remaining[0]["session_key"] == "sid-B"


@requires_agent
def test_drain_noop_when_empty(clean_registry):
    from api.process_watcher import drain_pending_watchers
    count = drain_pending_watchers(
        session_id="sid-X", model="m", workspace="/tmp", chain_depth=0,
    )
    assert count == 0


# ── _format_synthetic_completion ───────────────────────────────────────────

@requires_agent
def test_format_synthetic_completion_shape():
    from api.process_watcher import _format_synthetic_completion

    proc = types.SimpleNamespace(
        id="proc_xyz",
        command="bash -c 'echo hi'",
        exit_code=0,
        output_buffer="hi\n",
    )
    text = _format_synthetic_completion(proc)
    assert text.startswith("[SYSTEM: Background process proc_xyz completed")
    assert "(exit code 0)" in text
    assert "Command: bash -c 'echo hi'" in text
    assert "Output:\nhi" in text
    assert text.endswith("]")


@requires_agent
def test_format_synthetic_completion_truncates_long_output():
    from api.process_watcher import _format_synthetic_completion

    proc = types.SimpleNamespace(
        id="p",
        command="x",
        exit_code=1,
        output_buffer="A" * 5000,
    )
    text = _format_synthetic_completion(proc)
    # Output tail is 2000 chars of 'A'; the header/footer add fixed bytes.
    # Upper-bound sanity: never emit more than ~3KB for a 5KB buffer.
    assert len(text) < 3000
    assert text.count("A") == 2000


# ── _run_process_watcher_webui ─────────────────────────────────────────────

def _stub_registry_with_process(proc, consumed=False):
    """Build a namespace that mimics process_registry for the watcher."""
    return types.SimpleNamespace(
        get=lambda sid: proc if sid == proc.id else None,
        is_completion_consumed=lambda sid: consumed,
    )


@requires_agent
def test_watcher_triggers_resume_on_exit(clean_registry):
    from api.process_watcher import _run_process_watcher_webui

    proc = types.SimpleNamespace(
        id="proc_done",
        command="sleep 0",
        exit_code=0,
        output_buffer="finished\n",
        exited=True,  # immediately exited
    )
    fake_pr = _stub_registry_with_process(proc)

    resume_calls = []

    def fake_resume(**kwargs):
        resume_calls.append(kwargs)

    watcher = _make_watcher(session_id="proc_done", session_key="sid-1",
                            notify=True)
    watcher["check_interval"] = 0.01  # fast poll for test

    with patch("tools.process_registry.process_registry", fake_pr):
        with patch("api.process_watcher._run_agent_resume_turn", fake_resume):
            _run_process_watcher_webui(
                watcher,
                session_id="sid-1", model="m", workspace="/tmp", chain_depth=0,
            )

    assert len(resume_calls) == 1
    call = resume_calls[0]
    assert call["session_id"] == "sid-1"
    assert "proc_done" in call["synth_user_text"]
    assert "exit code 0" in call["synth_user_text"]


@requires_agent
def test_watcher_skips_when_notify_false(clean_registry):
    from api.process_watcher import _run_process_watcher_webui

    proc = types.SimpleNamespace(
        id="proc_silent", command="x", exit_code=0,
        output_buffer="", exited=True,
    )
    fake_pr = _stub_registry_with_process(proc)

    resume_calls = []
    watcher = _make_watcher(session_id="proc_silent", session_key="sid-1",
                            notify=False)
    watcher["check_interval"] = 0.01

    with patch("tools.process_registry.process_registry", fake_pr):
        with patch("api.process_watcher._run_agent_resume_turn",
                   lambda **kw: resume_calls.append(kw)):
            _run_process_watcher_webui(
                watcher,
                session_id="sid-1", model="m", workspace="/tmp", chain_depth=0,
            )

    assert resume_calls == [], "Must not resume when notify_on_complete=False"


@requires_agent
def test_watcher_skips_when_already_consumed(clean_registry):
    from api.process_watcher import _run_process_watcher_webui

    proc = types.SimpleNamespace(
        id="proc_consumed", command="x", exit_code=0,
        output_buffer="out", exited=True,
    )
    fake_pr = _stub_registry_with_process(proc, consumed=True)

    resume_calls = []
    watcher = _make_watcher(session_id="proc_consumed", session_key="sid-1",
                            notify=True)
    watcher["check_interval"] = 0.01

    with patch("tools.process_registry.process_registry", fake_pr):
        with patch("api.process_watcher._run_agent_resume_turn",
                   lambda **kw: resume_calls.append(kw)):
            _run_process_watcher_webui(
                watcher,
                session_id="sid-1", model="m", workspace="/tmp", chain_depth=0,
            )

    assert resume_calls == [], (
        "Must skip resume when agent already consumed via wait/poll/log"
    )


@requires_agent
def test_watcher_exits_when_process_vanishes(clean_registry):
    from api.process_watcher import _run_process_watcher_webui

    fake_pr = types.SimpleNamespace(
        get=lambda sid: None,  # process gone
        is_completion_consumed=lambda sid: False,
    )

    resume_calls = []
    watcher = _make_watcher(session_id="proc_gone", session_key="sid-1")
    watcher["check_interval"] = 0.01

    with patch("tools.process_registry.process_registry", fake_pr):
        with patch("api.process_watcher._run_agent_resume_turn",
                   lambda **kw: resume_calls.append(kw)):
            _run_process_watcher_webui(
                watcher,
                session_id="sid-1", model="m", workspace="/tmp", chain_depth=0,
            )

    assert resume_calls == []


@requires_agent
def test_watcher_waits_while_process_running(clean_registry):
    """Watcher must continue polling while exited is False."""
    from api.process_watcher import _run_process_watcher_webui

    state = {"exited": False, "polls": 0}

    class _FakeProc:
        id = "proc_running"
        command = "sleep 10"
        exit_code = 0
        output_buffer = ""
        @property
        def exited(self):
            state["polls"] += 1
            if state["polls"] >= 3:
                state["exited"] = True
            return state["exited"]

    proc = _FakeProc()
    fake_pr = _stub_registry_with_process(proc)

    resume_calls = []
    watcher = _make_watcher(session_id="proc_running", session_key="sid-1")
    watcher["check_interval"] = 0.01

    with patch("tools.process_registry.process_registry", fake_pr):
        with patch("api.process_watcher._run_agent_resume_turn",
                   lambda **kw: resume_calls.append(kw)):
            _run_process_watcher_webui(
                watcher,
                session_id="sid-1", model="m", workspace="/tmp", chain_depth=0,
            )

    assert state["polls"] >= 3, "Should poll multiple times before exit"
    assert len(resume_calls) == 1


# ── chain_depth cap (Phase 1 re-entrancy guard) ────────────────────────────

@requires_agent
def test_drain_skips_spawn_when_chain_depth_at_cap(clean_registry, monkeypatch):
    """When chain_depth meets the cap, drain must purge own watchers without
    starting a resume thread — prevents runaway auto-resume loops."""
    from api.process_watcher import drain_pending_watchers

    monkeypatch.setenv("HERMES_WEBUI_MAX_RESUME_CHAIN_DEPTH", "3")
    clean_registry.pending_watchers.extend([
        _make_watcher(platform="webui", session_key="sid-1"),
        _make_watcher(platform="slack", session_key="sid-1"),
    ])

    with patch("api.process_watcher._run_process_watcher_webui") as spawn_mock:
        count = drain_pending_watchers(
            session_id="sid-1", model="m", workspace="/tmp", chain_depth=3,
        )

    assert count == 0
    spawn_mock.assert_not_called()
    # Foreign watcher must still remain
    remaining = clean_registry.pending_watchers
    assert len(remaining) == 1 and remaining[0]["platform"] == "slack"


@requires_agent
def test_drain_below_cap_still_spawns(clean_registry, monkeypatch):
    """Sanity check: at depth-1 under the cap, drain still spawns."""
    from api.process_watcher import drain_pending_watchers

    monkeypatch.setenv("HERMES_WEBUI_MAX_RESUME_CHAIN_DEPTH", "3")
    clean_registry.pending_watchers.extend([
        _make_watcher(platform="webui", session_key="sid-1"),
    ])

    with patch("api.process_watcher._run_process_watcher_webui"):
        with patch("threading.Thread") as mock_thread:
            count = drain_pending_watchers(
                session_id="sid-1", model="m", workspace="/tmp", chain_depth=2,
            )

    assert count == 1
    assert mock_thread.call_count == 1


# ── Lifetime cap ───────────────────────────────────────────────────────────

@requires_agent
def test_watcher_respects_lifetime_cap(clean_registry, monkeypatch):
    """When HERMES_WEBUI_WATCHER_MAX_LIFETIME_SEC is short, watcher exits even
    if the process never finishes."""
    from api.process_watcher import _run_process_watcher_webui

    monkeypatch.setenv("HERMES_WEBUI_WATCHER_MAX_LIFETIME_SEC", "0.1")

    proc = types.SimpleNamespace(
        id="proc_forever", command="x", exit_code=None,
        output_buffer="", exited=False,
    )
    fake_pr = _stub_registry_with_process(proc)

    resume_calls = []
    watcher = _make_watcher(session_id="proc_forever", session_key="sid-1")
    watcher["check_interval"] = 0.05

    start = time.time()
    with patch("tools.process_registry.process_registry", fake_pr):
        with patch("api.process_watcher._run_agent_resume_turn",
                   lambda **kw: resume_calls.append(kw)):
            _run_process_watcher_webui(
                watcher,
                session_id="sid-1", model="m", workspace="/tmp", chain_depth=0,
            )
    elapsed = time.time() - start

    assert resume_calls == []
    # Should exit well under 2s (lifetime cap = 0.1s)
    assert elapsed < 2.0, f"Watcher took {elapsed:.2f}s; lifetime cap not honored"
