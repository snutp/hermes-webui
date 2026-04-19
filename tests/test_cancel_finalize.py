"""
Unit tests for Option C — _finalize_cancelled_messages.

When the user interrupts a turn, the Anthropic API history must not contain
orphan ``tool_use`` blocks (a ``tool_use`` without a matching ``tool_result``
in the next user message). This helper synthesizes tool_result entries for
unresolved tool_use ids so the next turn's API call stays valid.

Tests cover:
  • No unresolved tool_use → no mutation
  • Single orphan → tool_result appended with is_error=True
  • Multiple orphans across assistant messages → all paired
  • Already-paired tool_use → not re-paired
  • Mixed paired + orphan → only orphans get synthesized
  • Empty / malformed inputs → safe
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest


_WEBUI_ROOT = Path(__file__).parent.parent
if str(_WEBUI_ROOT) not in sys.path:
    sys.path.insert(0, str(_WEBUI_ROOT))


def _import_finalizer():
    # Import is done lazily so this test works without spinning up the full
    # streaming stack (the streaming module itself has heavy side-effect
    # imports).
    import importlib
    mod = importlib.import_module('api.streaming')
    return mod._finalize_cancelled_messages


def test_noop_when_no_tool_use():
    f = _import_finalizer()
    msgs = [
        {'role': 'user', 'content': 'hello'},
        {'role': 'assistant', 'content': 'hi there'},
    ]
    out = f(msgs)
    assert out is msgs
    assert len(msgs) == 2


def test_empty_and_malformed_inputs():
    f = _import_finalizer()
    assert f([]) == []
    assert f(None) is None
    assert f([{'not a dict': None}, None, 42]) == [{'not a dict': None}, None, 42]


def test_single_orphan_gets_paired():
    f = _import_finalizer()
    msgs = [
        {'role': 'user', 'content': 'run pytest'},
        {'role': 'assistant', 'content': [
            {'type': 'text', 'text': 'running tests'},
            {'type': 'tool_use', 'id': 'toolu_1', 'name': 'terminal', 'input': {'cmd': 'pytest'}},
        ]},
    ]
    f(msgs)
    assert len(msgs) == 3
    synth = msgs[-1]
    assert synth['role'] == 'user'
    assert isinstance(synth['content'], list) and len(synth['content']) == 1
    block = synth['content'][0]
    assert block['type'] == 'tool_result'
    assert block['tool_use_id'] == 'toolu_1'
    assert block['is_error'] is True
    assert '`terminal`' in block['content'] or 'terminal' in block['content']
    assert 'Interrupted' in block['content']


def test_already_paired_is_untouched():
    f = _import_finalizer()
    msgs = [
        {'role': 'assistant', 'content': [
            {'type': 'tool_use', 'id': 'toolu_1', 'name': 'read_file', 'input': {}},
        ]},
        {'role': 'user', 'content': [
            {'type': 'tool_result', 'tool_use_id': 'toolu_1', 'content': 'ok'},
        ]},
    ]
    before = [dict(m) for m in msgs]
    f(msgs)
    assert len(msgs) == 2  # no synthetic append
    assert msgs == before


def test_mixed_paired_and_orphan():
    f = _import_finalizer()
    msgs = [
        {'role': 'assistant', 'content': [
            {'type': 'tool_use', 'id': 'resolved', 'name': 'read_file', 'input': {}},
        ]},
        {'role': 'user', 'content': [
            {'type': 'tool_result', 'tool_use_id': 'resolved', 'content': 'file contents'},
        ]},
        {'role': 'assistant', 'content': [
            {'type': 'tool_use', 'id': 'orphan', 'name': 'terminal', 'input': {'cmd': 'sleep 60'}},
        ]},
    ]
    f(msgs)
    assert len(msgs) == 4
    synth = msgs[-1]
    assert synth['content'][0]['tool_use_id'] == 'orphan'
    assert synth['content'][0]['is_error'] is True


def test_multiple_orphans_same_message_all_paired():
    f = _import_finalizer()
    msgs = [
        {'role': 'assistant', 'content': [
            {'type': 'tool_use', 'id': 'a', 'name': 'read_file'},
            {'type': 'tool_use', 'id': 'b', 'name': 'terminal'},
            {'type': 'tool_use', 'id': 'c', 'name': 'write_file'},
        ]},
    ]
    f(msgs)
    assert len(msgs) == 2
    synth_blocks = msgs[-1]['content']
    assert [b['tool_use_id'] for b in synth_blocks] == ['a', 'b', 'c']
    assert all(b['is_error'] is True for b in synth_blocks)


def test_tool_use_without_id_is_skipped():
    """Safety: a malformed tool_use block with no id can't be paired; skip it
    rather than emitting a tool_result with None."""
    f = _import_finalizer()
    msgs = [
        {'role': 'assistant', 'content': [
            {'type': 'tool_use', 'name': 'terminal'},  # missing id
        ]},
    ]
    f(msgs)
    assert len(msgs) == 1  # nothing appended


def test_tool_use_without_name_still_paired():
    f = _import_finalizer()
    msgs = [
        {'role': 'assistant', 'content': [
            {'type': 'tool_use', 'id': 'x'},  # missing name
        ]},
    ]
    f(msgs)
    assert len(msgs) == 2
    assert msgs[-1]['content'][0]['tool_use_id'] == 'x'
    # Content string should still mention "Interrupted"
    assert 'Interrupted' in msgs[-1]['content'][0]['content']


def test_string_content_assistant_message_ignored():
    """Assistant messages with plain-string content (the common text-only
    case) have no tool_use — finalization should no-op."""
    f = _import_finalizer()
    msgs = [
        {'role': 'assistant', 'content': 'plain text reply'},
        {'role': 'assistant', 'content': 'another plain text reply'},
    ]
    f(msgs)
    assert len(msgs) == 2
