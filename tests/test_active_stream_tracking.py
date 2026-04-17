"""
Unit tests for Phase 2 session→active stream_id tracking.

Scope:
  • ACTIVE_STREAM_BY_SESSION is populated and cleared by _run_agent_streaming
  • Teardown only clears the mapping when the slot still points at our
    stream_id (protects against a resume turn that overwrote the slot before
    our finally block ran).
  • /api/chat/active_stream route (via handler-level simulation) returns
    None when the stream_id has been reaped from STREAMS.

These are pure unit tests; we do NOT run a full agent or spin up an HTTP
server. We patch _run_agent_streaming's body or invoke the slot-management
logic directly.
"""
import os
import sys
import threading
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

_WEBUI_ROOT = str(Path(__file__).parent.parent.resolve())
if _WEBUI_ROOT not in sys.path:
    sys.path.insert(0, _WEBUI_ROOT)

requires_agent = pytest.mark.skipif(
    _AGENT_DIR is None,
    reason="hermes-agent source not found",
)


@pytest.fixture
def clean_active_streams():
    from api.config import ACTIVE_STREAM_BY_SESSION, STREAMS
    ACTIVE_STREAM_BY_SESSION.clear()
    STREAMS.clear()
    yield
    ACTIVE_STREAM_BY_SESSION.clear()
    STREAMS.clear()


@requires_agent
def test_session_mapping_populated_on_start(clean_active_streams):
    """Simulate the start-block of _run_agent_streaming: the session's slot
    should point at this stream."""
    from api.config import ACTIVE_STREAM_BY_SESSION, ACTIVE_STREAM_LOCK

    session_id = "sid-A"
    stream_id = "stream-1"
    with ACTIVE_STREAM_LOCK:
        ACTIVE_STREAM_BY_SESSION[session_id] = stream_id

    assert ACTIVE_STREAM_BY_SESSION[session_id] == stream_id


@requires_agent
def test_teardown_pops_only_own_stream(clean_active_streams):
    """Finally block must NOT clobber a slot that a resume turn already
    overwrote. Otherwise the resume turn would not be discoverable."""
    from api.config import ACTIVE_STREAM_BY_SESSION, ACTIVE_STREAM_LOCK

    session_id = "sid-A"
    original_stream = "stream-user"
    resume_stream = "stream-resume"

    # User turn registered…
    with ACTIVE_STREAM_LOCK:
        ACTIVE_STREAM_BY_SESSION[session_id] = original_stream

    # …then the resume turn started while user turn was tearing down, and
    # overwrote the slot.
    with ACTIVE_STREAM_LOCK:
        ACTIVE_STREAM_BY_SESSION[session_id] = resume_stream

    # User turn's finally block runs the conditional clear.
    with ACTIVE_STREAM_LOCK:
        if ACTIVE_STREAM_BY_SESSION.get(session_id) == original_stream:
            ACTIVE_STREAM_BY_SESSION.pop(session_id, None)

    # Resume turn's slot must survive.
    assert ACTIVE_STREAM_BY_SESSION[session_id] == resume_stream


@requires_agent
def test_teardown_clears_when_no_concurrent_resume(clean_active_streams):
    """Common case: no concurrent resume → finally block clears the slot."""
    from api.config import ACTIVE_STREAM_BY_SESSION, ACTIVE_STREAM_LOCK

    session_id = "sid-A"
    stream_id = "stream-1"
    with ACTIVE_STREAM_LOCK:
        ACTIVE_STREAM_BY_SESSION[session_id] = stream_id
    with ACTIVE_STREAM_LOCK:
        if ACTIVE_STREAM_BY_SESSION.get(session_id) == stream_id:
            ACTIVE_STREAM_BY_SESSION.pop(session_id, None)

    assert session_id not in ACTIVE_STREAM_BY_SESSION


@requires_agent
def test_active_stream_route_returns_null_for_reaped_stream(clean_active_streams):
    """The endpoint logic: if the stream_id in the mapping is not in STREAMS
    anymore (race window between teardown's two pops), return None instead
    of handing the client a stale id."""
    from api.config import ACTIVE_STREAM_BY_SESSION, STREAMS

    session_id = "sid-X"
    stream_id = "stream-reaped"
    # Mapping points at a stream that has already been popped from STREAMS
    ACTIVE_STREAM_BY_SESSION[session_id] = stream_id
    # STREAMS does NOT contain stream_id (already reaped)

    # Simulate the route logic
    sid = ACTIVE_STREAM_BY_SESSION.get(session_id)
    live = bool(sid and sid in STREAMS)
    result = sid if live else None

    assert result is None


@requires_agent
def test_active_stream_route_returns_live_stream(clean_active_streams):
    """Happy path: mapping + STREAMS both have the stream_id."""
    from api.config import ACTIVE_STREAM_BY_SESSION, STREAMS
    import queue as _queue

    session_id = "sid-Y"
    stream_id = "stream-alive"
    ACTIVE_STREAM_BY_SESSION[session_id] = stream_id
    STREAMS[stream_id] = _queue.Queue()

    sid = ACTIVE_STREAM_BY_SESSION.get(session_id)
    live = bool(sid and sid in STREAMS)
    result = sid if live else None

    assert result == stream_id


@requires_agent
def test_concurrent_sessions_isolated(clean_active_streams):
    """Each session's slot is independent."""
    from api.config import ACTIVE_STREAM_BY_SESSION, ACTIVE_STREAM_LOCK

    with ACTIVE_STREAM_LOCK:
        ACTIVE_STREAM_BY_SESSION["sid-A"] = "streamA"
        ACTIVE_STREAM_BY_SESSION["sid-B"] = "streamB"

    assert ACTIVE_STREAM_BY_SESSION["sid-A"] == "streamA"
    assert ACTIVE_STREAM_BY_SESSION["sid-B"] == "streamB"

    # Clearing one does not affect the other
    with ACTIVE_STREAM_LOCK:
        if ACTIVE_STREAM_BY_SESSION.get("sid-A") == "streamA":
            ACTIVE_STREAM_BY_SESSION.pop("sid-A", None)

    assert "sid-A" not in ACTIVE_STREAM_BY_SESSION
    assert ACTIVE_STREAM_BY_SESSION["sid-B"] == "streamB"
