"""
Integration tests for webui auto-resume (Phase 1).

These tests use the real process_registry + real terminal_tool to spawn actual
background processes, then let the real watcher thread run and observe that
_run_agent_resume_turn is invoked (mocked here so we don't pull in AIAgent).

Why these are separate from unit tests:
  • Unit tests stub the registry and cover branch logic quickly.
  • Integration tests exercise the real spawn → pending_watchers enqueue →
    drain → watcher polling → exit detection → resume invocation pipeline.

Why AIAgent is still mocked:
  • Running a real LLM in CI is expensive and flaky. The Phase 1 contract is
    "watcher correctly invokes the resume entry point" — whether the entry
    point's LLM call succeeds is orthogonal and covered by L3 smoke tests.
"""
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

# ── sys.path setup (same as unit test file) ────────────────────────────────
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

_WEBUI_ROOT = str(Path(__file__).parent.parent.resolve())
if _WEBUI_ROOT not in sys.path:
    sys.path.insert(0, _WEBUI_ROOT)

requires_agent = pytest.mark.skipif(
    _AGENT_DIR is None,
    reason="hermes-agent source not found",
)
only_unix = pytest.mark.skipif(
    sys.platform == "win32",
    reason="terminal_tool background spawn uses bash; Windows runner skips",
)


# ── Fixtures ───────────────────────────────────────────────────────────────

@pytest.fixture
def webui_env():
    """Set the HERMES_SESSION_* env required for terminal_tool to enqueue a
    webui-platform watcher into pending_watchers. Mirrors streaming.py:124+."""
    session_id = "test-websid-" + os.urandom(4).hex()
    old = {
        "HERMES_SESSION_PLATFORM": os.environ.get("HERMES_SESSION_PLATFORM"),
        "HERMES_SESSION_KEY": os.environ.get("HERMES_SESSION_KEY"),
        "HERMES_SESSION_CHAT_ID": os.environ.get("HERMES_SESSION_CHAT_ID"),
        "HERMES_EXEC_ASK": os.environ.get("HERMES_EXEC_ASK"),
    }
    os.environ["HERMES_SESSION_PLATFORM"] = "webui"
    os.environ["HERMES_SESSION_KEY"] = session_id
    os.environ["HERMES_SESSION_CHAT_ID"] = session_id
    os.environ["HERMES_EXEC_ASK"] = "1"
    yield session_id
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


@pytest.fixture
def clean_registry():
    from tools.process_registry import process_registry
    process_registry.pending_watchers.clear()
    yield process_registry
    # Kill any stragglers so a failing test doesn't leak processes
    with process_registry._lock:
        for sid, sess in list(process_registry._running.items()):
            try:
                sess.proc.kill()
            except Exception:
                pass
    process_registry.pending_watchers.clear()


def _shrink_watcher_intervals(registry, interval=0.2):
    """Shrink check_interval on every pending watcher so integration tests
    finish quickly. Must be called AFTER terminal_tool enqueues the watcher
    but BEFORE drain_pending_watchers spawns the thread."""
    for w in registry.pending_watchers:
        w["check_interval"] = interval


# ── Scenario A: spawn → exit → resume invoked ──────────────────────────────

@requires_agent
@only_unix
def test_background_completion_triggers_resume(webui_env, clean_registry):
    """Real terminal_tool spawn + real watcher thread + mocked resume."""
    from tools.terminal_tool import terminal_tool
    from api.process_watcher import drain_pending_watchers

    session_id = webui_env

    # Spawn a background process that exits quickly. We use `sleep` with a
    # short delay so the watcher gets at least one "still running" poll before
    # the exit. bash -c / shell pipelines trigger terminal_tool's approval
    # guard which is an orthogonal concern here.
    result_json = terminal_tool(
        command="sleep 1",
        background=True,
        notify_on_complete=True,
    )
    result = json.loads(result_json)
    proc_sid = result["session_id"]
    assert proc_sid.startswith("proc_")

    # Watcher descriptor is in the queue, tagged as webui platform.
    assert len(clean_registry.pending_watchers) == 1
    w = clean_registry.pending_watchers[0]
    assert w["platform"] == "webui"
    assert w["session_key"] == session_id
    assert w["notify_on_complete"] is True

    _shrink_watcher_intervals(clean_registry, interval=0.2)

    resume_calls = []

    def fake_resume(**kwargs):
        resume_calls.append(kwargs)

    with patch("api.process_watcher._run_agent_resume_turn", fake_resume):
        drain_pending_watchers(
            session_id=session_id, model="stub", workspace="/tmp", chain_depth=0,
        )
        # Wait for the process to finish + watcher to poll + resume to fire.
        deadline = time.time() + 15.0
        while time.time() < deadline and not resume_calls:
            time.sleep(0.2)

    assert len(resume_calls) == 1, (
        f"Expected one resume, got {len(resume_calls)}. "
        f"Watcher thread likely failed to detect exit. Proc sid={proc_sid}"
    )
    synth = resume_calls[0]["synth_user_text"]
    assert proc_sid in synth
    assert "exit code 0" in synth
    assert "Command: sleep 1" in synth


# ── Scenario D: agent consumed completion via wait() — resume skipped ──────

@requires_agent
@only_unix
def test_consumed_completion_skips_resume(webui_env, clean_registry):
    """When the agent calls process(action=wait) during the same turn, the
    completion is marked consumed and the watcher must NOT trigger a resume."""
    from tools.terminal_tool import terminal_tool
    from api.process_watcher import drain_pending_watchers
    from tools.process_registry import process_registry

    session_id = webui_env

    result = json.loads(terminal_tool(
        command="sleep 0.5",
        background=True,
        notify_on_complete=True,
    ))
    proc_sid = result["session_id"]

    # Simulate the agent draining the completion via process(action=wait).
    # process_registry's consumed-tracking uses a private set; we poke it
    # directly instead of going through the process tool (which would require
    # spinning up the full tool dispatch machinery).
    time.sleep(1.2)
    process_registry._completion_consumed.add(proc_sid)
    assert process_registry.is_completion_consumed(proc_sid)

    _shrink_watcher_intervals(clean_registry, interval=0.2)

    resume_calls = []
    with patch("api.process_watcher._run_agent_resume_turn",
               lambda **kw: resume_calls.append(kw)):
        drain_pending_watchers(
            session_id=session_id, model="stub", workspace="/tmp", chain_depth=0,
        )
        # Give the watcher enough time to poll at least once after exit.
        time.sleep(1.5)

    assert resume_calls == [], (
        "Watcher fired a resume even though the completion was already consumed"
    )


# ── Scenario E: platform filter isolates concurrent runtimes ───────────────

@requires_agent
@only_unix
def test_drain_leaves_foreign_platform_watchers(webui_env, clean_registry):
    """A slack-platform watcher injected concurrently must remain in the queue
    after the webui drain runs — another runtime is responsible for it."""
    from tools.terminal_tool import terminal_tool
    from api.process_watcher import drain_pending_watchers

    session_id = webui_env

    # Spawn a webui-owned process
    json.loads(terminal_tool(
        command="sleep 0.3",
        background=True,
        notify_on_complete=True,
    ))

    # Inject a foreign watcher directly into the queue
    clean_registry.pending_watchers.append({
        "session_id": "proc_foreign",
        "check_interval": 5,
        "session_key": "slack-session-xyz",
        "platform": "slack",
        "chat_id": "C123",
        "user_id": "U1",
        "user_name": "alice",
        "thread_id": "",
        "notify_on_complete": True,
    })

    _shrink_watcher_intervals(clean_registry, interval=0.2)

    with patch("api.process_watcher._run_agent_resume_turn"):
        drain_pending_watchers(
            session_id=session_id, model="stub", workspace="/tmp", chain_depth=0,
        )

    # The slack watcher must still be in the queue
    remaining = [w for w in clean_registry.pending_watchers if w["platform"] == "slack"]
    assert len(remaining) == 1, "Foreign platform watcher was incorrectly drained"
