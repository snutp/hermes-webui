"""
ContextVar isolation test for Phase 1 auto-resume.

When two webui sessions run concurrently, each in its own daemon thread, the
gateway.session_context ContextVars set inside each thread must NOT leak into
the other. terminal_tool reads those ContextVars (via get_session_env) to
decide which session_key to stamp on pending_watchers — so correctness of
notify_on_complete depends on strict thread-local scoping.

This test calls set_session_vars in two threads and verifies each thread's
get_session_env returns its own value.
"""
import os
import sys
import threading
from pathlib import Path

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


@requires_agent
def test_contextvars_isolate_across_threads():
    """Two threads call set_session_vars with different session_keys; each
    sees only its own value."""
    from gateway.session_context import set_session_vars, clear_session_vars, get_session_env

    barrier = threading.Barrier(2)
    observed = {"A": None, "B": None}
    errors = []

    def run(label, key):
        try:
            tokens = set_session_vars(platform="webui", chat_id=key, session_key=key)
            try:
                # Let both threads set their values before either reads —
                # this is the moment where naive os.environ-only would bleed.
                barrier.wait(timeout=5.0)
                observed[label] = get_session_env("HERMES_SESSION_KEY", "")
            finally:
                clear_session_vars(tokens)
        except Exception as e:
            errors.append((label, e))

    t_a = threading.Thread(target=run, args=("A", "sid-A"))
    t_b = threading.Thread(target=run, args=("B", "sid-B"))
    t_a.start()
    t_b.start()
    t_a.join(timeout=10.0)
    t_b.join(timeout=10.0)

    assert errors == [], f"Thread errors: {errors}"
    assert observed["A"] == "sid-A", (
        f"Thread A saw '{observed['A']}' instead of its own session_key"
    )
    assert observed["B"] == "sid-B", (
        f"Thread B saw '{observed['B']}' instead of its own session_key"
    )
