"""Unit tests for StreamingJSONParser (FSM-based streaming JSON parser)."""

import json

import pytest

from model.streaming import StreamingJSONParser


class TestFullParse:
    def test_full_json_equals_json_loads(self):
        data = '{"type": "load_skill", "params": {"skill_name": "test"}}'
        parser = StreamingJSONParser()
        parser.feed(data)
        assert parser.get_result() == json.loads(data)

    def test_chunked_input(self):
        chunks = ['{"type": "load_s', 'kill", "params": {', '"skill_name": "test"}}']
        parser = StreamingJSONParser()
        for chunk in chunks:
            parser.feed(chunk)
        assert parser.get_result()["type"] == "load_skill"

    def test_nested_object(self):
        data = '{"type": "final_answer", "params": {"content": "hello"}}'
        parser = StreamingJSONParser()
        parser.feed(data)
        result = parser.get_result()
        assert result == json.loads(data)

    def test_number_value(self):
        data = '{"count": 42, "ratio": 3.14}'
        parser = StreamingJSONParser()
        parser.feed(data)
        result = parser.get_result()
        assert result["count"] == 42
        assert abs(result["ratio"] - 3.14) < 1e-9

    def test_boolean_values(self):
        data = '{"ok": true, "fail": false}'
        parser = StreamingJSONParser()
        parser.feed(data)
        result = parser.get_result()
        assert result["ok"] is True
        assert result["fail"] is False

    def test_null_value(self):
        data = '{"x": null}'
        parser = StreamingJSONParser()
        parser.feed(data)
        assert parser.get_result()["x"] is None

    def test_escape_sequences(self):
        data = r'{"msg": "line1\nline2\ttab"}'
        parser = StreamingJSONParser()
        parser.feed(data)
        assert parser.get_result()["msg"] == "line1\nline2\ttab"

    def test_escaped_quote(self):
        data = r'{"msg": "say \"hello\""}'
        parser = StreamingJSONParser()
        parser.feed(data)
        assert parser.get_result()["msg"] == 'say "hello"'

    def test_empty_object(self):
        parser = StreamingJSONParser()
        parser.feed("{}")
        assert parser.get_result() == {}

    def test_empty_string_value(self):
        parser = StreamingJSONParser()
        parser.feed('{"key": ""}')
        assert parser.get_result()["key"] == ""

    def test_array_value(self):
        data = '{"items": ["a", "b", "c"]}'
        parser = StreamingJSONParser()
        parser.feed(data)
        assert parser.get_result() == json.loads(data)

    def test_single_char_chunks(self):
        data = '{"type": "foo", "params": {"content": "bar"}}'
        parser = StreamingJSONParser()
        for ch in data:
            parser.feed(ch)
        assert parser.get_result() == json.loads(data)


class TestCallbacks:
    def test_path_callback_fires_on_type(self):
        fired = []

        def on_type(path, val, done):
            if done:
                fired.append(val)

        parser = StreamingJSONParser(path_callbacks={"$.type": on_type})
        parser.feed('{"type": "final_answer", "params": {"content": "done"}}')
        assert "final_answer" in fired

    def test_streaming_content_delta(self):
        deltas = []

        def on_content(path, val, done):
            deltas.append((val, done))

        parser = StreamingJSONParser(path_callbacks={"$.params.content": on_content})
        parser.feed('{"type": "final_answer", "params": {"content": "hello world"}}')
        # Concatenating done=False deltas should equal the full content
        partial = "".join(v for v, done in deltas if not done)
        assert partial == "hello world"
        # At least one done=True callback
        assert any(done for _, done in deltas)
        # done=True carries the complete value
        complete_vals = [v for v, done in deltas if done]
        assert complete_vals[-1] == "hello world"

    def test_delta_concat_equals_get_result(self):
        """Concatenated done=False deltas == get_result()["params"]["content"]."""
        deltas = []

        def on_content(path, val, done):
            if not done:
                deltas.append(val)

        parser = StreamingJSONParser(path_callbacks={"$.params.content": on_content})
        content = "streaming content delta test"
        parser.feed(f'{{"type": "final_answer", "params": {{"content": "{content}"}}}}')
        assert "".join(deltas) == content
        assert parser.get_result()["params"]["content"] == content

    def test_type_callback_fires_before_content(self):
        order = []

        def on_type(path, val, done):
            if done:
                order.append("type")

        def on_content(path, val, done):
            if done:
                order.append("content")

        parser = StreamingJSONParser(path_callbacks={
            "$.type": on_type,
            "$.params.content": on_content,
        })
        parser.feed('{"type": "final_answer", "params": {"content": "done"}}')
        assert order.index("type") < order.index("content")

    def test_skill_name_callback(self):
        fired = []

        def on_skill(path, val, done):
            if done:
                fired.append(val)

        parser = StreamingJSONParser(path_callbacks={"$.params.skill_name": on_skill})
        parser.feed('{"type": "load_skill", "params": {"skill_name": "my-skill"}}')
        assert fired == ["my-skill"]

    def test_no_callbacks_registered(self):
        """Parser works correctly with no callbacks."""
        parser = StreamingJSONParser()
        data = '{"type": "foo", "params": {"x": 1}}'
        parser.feed(data)
        assert parser.get_result() == json.loads(data)

    def test_unregistered_path_no_callback(self):
        fired = []
        parser = StreamingJSONParser(path_callbacks={"$.other": lambda p, v, d: fired.append(v)})
        parser.feed('{"type": "final_answer", "params": {"content": "x"}}')
        assert fired == []

    def test_chunked_callback_content(self):
        """Callback fires correctly even when chunks split mid-value."""
        deltas = []

        def on_content(path, val, done):
            deltas.append((val, done))

        parser = StreamingJSONParser(path_callbacks={"$.params.content": on_content})
        chunks = [
            '{"type": "final_answer", ',
            '"params": {"content": "hel',
            'lo wor',
            'ld"}}',
        ]
        for chunk in chunks:
            parser.feed(chunk)

        partial = "".join(v for v, done in deltas if not done)
        assert partial == "hello world"
        assert any(done for _, done in deltas)


class TestReset:
    def test_reset_clears_state(self):
        parser = StreamingJSONParser()
        parser.feed('{"type": "foo"}')
        assert parser.get_result()["type"] == "foo"
        parser.reset()
        assert parser.get_result() == {}

    def test_reset_allows_reuse(self):
        fired = []

        def on_type(path, val, done):
            if done:
                fired.append(val)

        parser = StreamingJSONParser(path_callbacks={"$.type": on_type})
        parser.feed('{"type": "first"}')
        parser.reset()
        parser.feed('{"type": "second"}')
        assert fired == ["first", "second"]
