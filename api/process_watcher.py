"""
Hermes Web UI — Background process auto-resume watcher.

When the agent invokes terminal(background=true, notify_on_complete=true), the
process_registry (hermes-agent-src/tools/process_registry.py) enqueues a
watcher descriptor into pending_watchers. After each agent turn in the webui,
drain_pending_watchers() pulls webui-owned watchers out and spawns one daemon
thread per watcher. The thread polls the process; when it exits, it builds a
synthetic [SYSTEM: ...] user message and re-invokes _run_agent_streaming so
the agent can continue its work without user interaction.

This mirrors gateway/run.py:_run_process_watcher but without a gateway adapter —
the resume turn runs through the webui's normal streaming entry point, which
persists to SessionDB and leaves a stream queue for Phase 2 (UI live-push).

Phase 1 — completion status:
  • Watcher spawn + resume invocation for notify_on_complete: implemented.
  • watch_patterns support: intentionally deferred. terminal_tool only stamps
    watcher_platform when notify_on_complete=True (see
    hermes-agent-src/tools/terminal_tool.py:1415-1445), so a background process
    started with watch_patterns=[...] alone has no ownership metadata. Until
    terminal_tool is updated to tag those too, webui cannot safely claim
    watch_match / watch_disabled events without risking cross-session drain.
    Users on webui who need mid-process alerts should either pair
    watch_patterns with notify_on_complete=True (completion delivers the tail
    via the same path) or poll via process(action='log').
  • SSE push to reconnecting browsers: not yet. The resume stream_id lives in
    STREAMS but is never consumed; _run_agent_streaming's finally block reaps
    it. Users see the resume turn only after reloading the session.

Concurrency caveat (existing webui behavior, not introduced by Phase 1):
  _run_agent_streaming does NOT hold `_agent_lock` around its agent run. Two
  turns for the same session can therefore overlap in principle. Phase 1 does
  not regress this — it simply inherits it. We filter watchers by session_key
  so that drains do not steal watchers from another session, but we cannot
  guarantee a turn in flight will see only its own watchers if another turn
  for the *same* session is mid-run. Tightening this is tracked for a later
  refactor (see ARCHITECTURE.md:1509 which documents the intended invariant).

Platform filter: only watchers with platform='webui' AND session_key matching
the originating webui session are claimed. Other platforms (slack, telegram,
ACP) stay in pending_watchers so their own runtimes can handle them.

Resume chain depth:
  Each resume turn is allowed to spawn further notify_on_complete tasks, which
  can in turn trigger more resumes. We cap the chain at
  _MAX_RESUME_CHAIN_DEPTH to avoid a runaway loop if the agent keeps re-
  triggering itself. The depth is carried through the watcher descriptor
  via `_webui_chain_depth` (a private field we inject on drain).
"""
import logging
import os
import queue as _queue
import threading
import time
import uuid

logger = logging.getLogger(__name__)

_WEBUI_PLATFORM = "webui"
_DEFAULT_POLL_INTERVAL = 5.0

# Hard cap so a runaway process does not leak a thread forever. Configurable
# via HERMES_WEBUI_WATCHER_MAX_LIFETIME_SEC for long-running CI/build jobs.
_DEFAULT_WATCHER_MAX_LIFETIME_SEC = 6 * 60 * 60


def _watcher_max_lifetime_sec() -> float:
    raw = os.environ.get("HERMES_WEBUI_WATCHER_MAX_LIFETIME_SEC")
    if raw:
        try:
            v = float(raw)
            if v > 0:
                return v
        except ValueError:
            logger.warning(
                "Invalid HERMES_WEBUI_WATCHER_MAX_LIFETIME_SEC=%r; using default", raw,
            )
    return _DEFAULT_WATCHER_MAX_LIFETIME_SEC


# Resume chain depth: hard cap on self-triggered resume turns. If the agent
# keeps starting background processes with notify_on_complete inside resume
# turns, we stop after this many hops.
_DEFAULT_MAX_RESUME_CHAIN_DEPTH = 10


def _max_resume_chain_depth() -> int:
    raw = os.environ.get("HERMES_WEBUI_MAX_RESUME_CHAIN_DEPTH")
    if raw:
        try:
            v = int(raw)
            if v >= 0:
                return v
        except ValueError:
            logger.warning(
                "Invalid HERMES_WEBUI_MAX_RESUME_CHAIN_DEPTH=%r; using default", raw,
            )
    return _DEFAULT_MAX_RESUME_CHAIN_DEPTH


def drain_pending_watchers(
    *,
    session_id: str,
    model: str,
    workspace: str,
    chain_depth: int = 0,
) -> int:
    """Pull webui watchers owned by ``session_id`` out of pending_watchers and
    spawn one daemon thread per watcher.

    Non-matching pending_watchers entries are preserved so other runtimes
    (slack gateway, ACP) can handle their own. watch_patterns events in
    completion_queue are intentionally NOT drained here — see the module
    docstring.

    ``chain_depth`` tracks how many resume hops have occurred so far; when it
    reaches the cap, we drain the queue but skip spawning new watchers.

    Returns the number of watchers spawned.
    """
    from tools.process_registry import process_registry as _pr

    max_depth = _max_resume_chain_depth()
    if chain_depth >= max_depth:
        # Purge our watchers without spawning — avoid indefinite auto-resume.
        # Foreign-platform / foreign-session entries stay in pending_watchers.
        _drain_and_discard_own_watchers(_pr, session_id)
        logger.warning(
            "Resume chain depth cap (%d) reached for session %s — halting auto-resume",
            max_depth, session_id,
        )
        return 0

    mine: list[dict] = []
    leftover: list[dict] = []
    # pending_watchers is a plain list without its own lock. We iterate
    # destructively and rebuild in-place. This is NOT safe against a
    # concurrent terminal_tool.append() from another thread; see the module
    # docstring's concurrency caveat. Phase 1 tolerates this at the same
    # level the rest of webui already does.
    while _pr.pending_watchers:
        w = _pr.pending_watchers.pop(0)
        if w.get("platform") == _WEBUI_PLATFORM and w.get("session_key") == session_id:
            mine.append(w)
        else:
            leftover.append(w)
    _pr.pending_watchers.extend(leftover)

    for w in mine:
        thr = threading.Thread(
            target=_run_process_watcher_webui,
            args=(w,),
            kwargs={
                "session_id": session_id,
                "model": model,
                "workspace": workspace,
                "chain_depth": chain_depth,
            },
            daemon=True,
            name=f"webui-proc-watcher-{str(w.get('session_id', '?'))[:8]}",
        )
        thr.start()

    if mine:
        logger.info(
            "Spawned %d webui process watcher(s) for session %s (depth=%d)",
            len(mine), session_id, chain_depth,
        )
    return len(mine)


def _drain_and_discard_own_watchers(pr, session_id: str) -> None:
    leftover: list[dict] = []
    while pr.pending_watchers:
        w = pr.pending_watchers.pop(0)
        if not (w.get("platform") == _WEBUI_PLATFORM and
                w.get("session_key") == session_id):
            leftover.append(w)
    pr.pending_watchers.extend(leftover)


def _run_process_watcher_webui(
    watcher: dict,
    *,
    session_id: str,
    model: str,
    workspace: str,
    chain_depth: int = 0,
) -> None:
    """Poll a single background process; on exit, trigger an agent resume turn."""
    from tools.process_registry import process_registry as _pr

    proc_sid = watcher.get("session_id", "")
    interval = float(watcher.get("check_interval") or _DEFAULT_POLL_INTERVAL)
    agent_notify = bool(watcher.get("notify_on_complete", False))

    logger.debug(
        "Process watcher started: proc=%s webui_session=%s interval=%.1fs notify=%s depth=%d",
        proc_sid, session_id, interval, agent_notify, chain_depth,
    )

    lifetime_cap = _watcher_max_lifetime_sec()
    start_monotonic = time.monotonic()

    try:
        while True:
            if (time.monotonic() - start_monotonic) > lifetime_cap:
                logger.warning(
                    "Process watcher hit lifetime cap (%.0fs): proc=%s session=%s",
                    lifetime_cap, proc_sid, session_id,
                )
                return
            time.sleep(interval)

            proc = _pr.get(proc_sid)
            if proc is None:
                logger.debug("Process %s vanished — watcher exiting", proc_sid)
                return
            if not proc.exited:
                continue

            if not agent_notify:
                return
            if _pr.is_completion_consumed(proc_sid):
                logger.debug(
                    "Process %s already consumed — skipping auto-resume", proc_sid,
                )
                return

            synth_text = _format_synthetic_completion(proc)
            logger.info(
                "Process %s exited (rc=%s) — triggering resume turn for session %s",
                proc_sid, proc.exit_code, session_id,
            )
            _run_agent_resume_turn(
                session_id=session_id,
                synth_user_text=synth_text,
                model=model,
                workspace=workspace,
                chain_depth=chain_depth,
            )
            return
    except Exception:
        logger.exception(
            "Process watcher crashed: proc=%s session=%s", proc_sid, session_id,
        )


def _format_synthetic_completion(proc) -> str:
    """Build the [SYSTEM: ...] user message injected into the resume turn.

    Matches the gateway's format so prompts and tests written for one backend
    transfer to the other.
    """
    try:
        from tools.ansi_strip import strip_ansi
    except ImportError:
        def strip_ansi(s): return s

    tail = proc.output_buffer[-2000:] if proc.output_buffer else ""
    tail = strip_ansi(tail)
    return (
        f"[SYSTEM: Background process {proc.id} completed "
        f"(exit code {proc.exit_code}).\n"
        f"Command: {proc.command}\n"
        f"Output:\n{tail}]"
    )


def _run_agent_resume_turn(
    *,
    session_id: str,
    synth_user_text: str,
    model: str,
    workspace: str,
    chain_depth: int = 0,
) -> None:
    """Invoke _run_agent_streaming with a synthetic user message.

    A fresh resume stream_id is created so the existing agent machinery
    (thread-local env, session save, tool progress callbacks, and nested
    pending_watchers drain for chained background tasks) runs unchanged. The
    queue is registered in STREAMS but never consumed — Phase 1 is headless.
    _run_agent_streaming's finally block reaps the queue entry.

    The ``chain_depth + 1`` is propagated to the nested drain so that a resume
    turn that itself spawns notify_on_complete work cannot exceed the cap.
    """
    from api.config import STREAMS, STREAMS_LOCK
    from api.streaming import _run_agent_streaming

    resume_stream_id = f"resume-{uuid.uuid4().hex[:12]}"
    q = _queue.Queue()
    with STREAMS_LOCK:
        STREAMS[resume_stream_id] = q

    try:
        _run_agent_streaming(
            session_id,
            synth_user_text,
            model,
            workspace,
            resume_stream_id,
            None,  # attachments
            chain_depth=chain_depth + 1,
        )
    except Exception:
        logger.exception("Resume turn failed for session %s", session_id)
