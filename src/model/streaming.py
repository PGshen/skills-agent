"""StreamingJSONParser: FSM-based streaming JSON parser (Phase C)."""

from __future__ import annotations

from enum import Enum, auto
from typing import Any, Callable

# Callback: (path: str, value: str, done: bool) -> None
# done=False: value is a delta chunk; done=True: value is the complete string
PathCallback = Callable[[str, str, bool], None]


class _State(Enum):
    VALUE = auto()        # Expecting a value
    KEY_OR_END = auto()   # Expecting a key string or '}'
    KEY = auto()          # Reading a key string
    COLON = auto()        # Expecting ':'
    COMMA = auto()        # Expecting ',', '}', or ']'
    VALUE_OR_END = auto() # Expecting a value or ']' (empty array)
    NUMBER = auto()       # Reading a number
    TRUE1 = auto()
    TRUE2 = auto()
    TRUE3 = auto()
    FALSE1 = auto()
    FALSE2 = auto()
    FALSE3 = auto()
    FALSE4 = auto()
    NULL1 = auto()
    NULL2 = auto()
    NULL3 = auto()


def _path_to_jsonpath(path: list[str | int]) -> str:
    """Convert internal path list to JSONPath string like $.type or $.params.content."""
    if not path:
        return "$"
    parts = []
    for seg in path:
        if isinstance(seg, int):
            parts.append(f"[{seg}]")
        else:
            parts.append(f".{seg}")
    return "$" + "".join(parts)


class StreamingJSONParser:
    """
    FSM streaming JSON parser.
    path_callbacks: path → callback function mapping.

    Usage:
        def on_content(path, val, done):
            sink.on_text_chunk(val, done)

        parser = StreamingJSONParser(path_callbacks={
            "$.type": lambda p, v, d: None,
            "$.params.content": on_content,
        })
        for chunk in model_stream:
            parser.feed(chunk)
        result = parser.get_result()
    """

    def __init__(self, path_callbacks: dict[str, PathCallback] | None = None):
        self._callbacks: dict[str, PathCallback] = path_callbacks or {}
        self._state = _State.VALUE
        self._stack: list[Any] = []
        self._path: list[str | int] = []
        self._buffer = ""
        self._is_escaped = False
        self._is_in_string = False
        self._current_key: str | None = None
        self._array_indexes: list[int] = []
        # Tracks how many buffer chars have been sent as deltas per path
        self._last_sent_pos: dict[str, int] = {}

    def feed(self, chunk: str) -> None:
        """Feed a string chunk, update the state machine, trigger callbacks."""
        for ch in chunk:
            self._process_char(ch)

    def get_result(self) -> dict:
        """Return the complete parsed JSON object after stream ends."""
        if self._stack:
            return self._stack[0]
        return {}

    def reset(self) -> None:
        """Reset the state machine for a new invocation."""
        self._state = _State.VALUE
        self._stack = []
        self._path = []
        self._buffer = ""
        self._is_escaped = False
        self._is_in_string = False
        self._current_key = None
        self._array_indexes = []
        self._last_sent_pos = {}

    # ── helpers ───────────────────────────────────────────────────────────

    def _jsonpath(self) -> str:
        return _path_to_jsonpath(self._path)

    def _fire_cb(self, path_key: str, value: str, done: bool) -> None:
        cb = self._callbacks.get(path_key)
        if cb is not None:
            cb(path_key, value, done)

    def _fire_delta(self) -> None:
        """Fire incremental (done=False) callback for current string path."""
        pk = self._jsonpath()
        if pk not in self._callbacks:
            return
        last = self._last_sent_pos.get(pk, 0)
        if len(self._buffer) > last:
            self._fire_cb(pk, self._buffer[last:], False)
            self._last_sent_pos[pk] = len(self._buffer)

    def _put_in_parent(self, value: Any) -> None:
        """Add value to the current parent container (dict or list)."""
        if not self._stack:
            return
        parent = self._stack[-1]
        if isinstance(parent, list):
            if self._array_indexes:
                idx = self._array_indexes[-1]
                while len(parent) <= idx:
                    parent.append(None)
                parent[idx] = value
        elif isinstance(parent, dict) and self._current_key is not None:
            parent[self._current_key] = value

    def _complete_scalar(self, value: Any, str_repr: str) -> None:
        """Finalize a scalar: fire done=True callback and place in tree."""
        pk = self._jsonpath()
        self._last_sent_pos.pop(pk, None)
        self._fire_cb(pk, str_repr, True)
        if self._stack:
            self._put_in_parent(value)
        else:
            self._stack.append(value)  # root scalar

    # ── FSM core ──────────────────────────────────────────────────────────

    def _process_char(self, ch: str) -> None:
        if self._is_in_string:
            self._handle_in_string(ch)
            return
        s = self._state
        if s == _State.VALUE:
            self._on_value(ch)
        elif s == _State.KEY_OR_END:
            self._on_key_or_end(ch)
        elif s == _State.KEY:
            self._on_key(ch)
        elif s == _State.COLON:
            self._on_colon(ch)
        elif s == _State.COMMA:
            self._on_comma(ch)
        elif s == _State.VALUE_OR_END:
            self._on_value_or_end(ch)
        elif s == _State.NUMBER:
            self._on_number(ch)
        elif s in (_State.TRUE1, _State.TRUE2, _State.TRUE3):
            self._on_true(ch)
        elif s in (_State.FALSE1, _State.FALSE2, _State.FALSE3, _State.FALSE4):
            self._on_false(ch)
        elif s in (_State.NULL1, _State.NULL2, _State.NULL3):
            self._on_null(ch)

    def _handle_in_string(self, ch: str) -> None:
        if self._is_escaped:
            _esc = {
                'n': '\n', 't': '\t', 'r': '\r', '\\': '\\',
                '"': '"', '/': '/', 'b': '\b', 'f': '\f',
            }
            self._buffer += _esc.get(ch, '\\' + ch)
            self._is_escaped = False
            if self._state == _State.VALUE:
                self._fire_delta()
            return

        if ch == '\\':
            self._is_escaped = True
            return

        if ch == '"':
            self._is_in_string = False
            if self._state == _State.KEY:
                self._current_key = self._buffer
                self._buffer = ""
                self._state = _State.COLON
            else:  # VALUE
                complete = self._buffer
                self._complete_scalar(complete, complete)
                self._buffer = ""
                self._state = _State.COMMA
            return

        self._buffer += ch
        if self._state == _State.VALUE:
            self._fire_delta()

    def _on_value(self, ch: str) -> None:
        if ch == '{':
            obj: dict = {}
            self._put_in_parent(obj)
            self._stack.append(obj)
            self._state = _State.KEY_OR_END
        elif ch == '[':
            arr: list = []
            self._put_in_parent(arr)
            self._stack.append(arr)
            self._array_indexes.append(0)
            self._path.append(0)
            self._state = _State.VALUE_OR_END
        elif ch == '"':
            self._is_in_string = True
            self._buffer = ""
        elif ch == 't':
            self._buffer = 't'
            self._state = _State.TRUE1
        elif ch == 'f':
            self._buffer = 'f'
            self._state = _State.FALSE1
        elif ch == 'n':
            self._buffer = 'n'
            self._state = _State.NULL1
        elif ch in '0123456789-':
            self._buffer = ch
            self._state = _State.NUMBER
        # else: whitespace, ignore

    def _on_key_or_end(self, ch: str) -> None:
        if ch == '}':
            self._end_object()
        elif ch == '"':
            self._is_in_string = True
            self._buffer = ""
            self._state = _State.KEY
        # else: whitespace, ignore

    def _on_key(self, ch: str) -> None:
        if ch == '"':
            self._is_in_string = True
            self._buffer = ""
        # else: whitespace, ignore

    def _on_colon(self, ch: str) -> None:
        if ch == ':':
            if self._current_key is not None:
                self._path.append(self._current_key)
            self._state = _State.VALUE
        # else: whitespace, ignore

    def _on_comma(self, ch: str) -> None:
        if ch == ',':
            if self._stack and isinstance(self._stack[-1], list):
                if self._array_indexes:
                    self._array_indexes[-1] += 1
                    self._path[-1] = self._array_indexes[-1]
                self._state = _State.VALUE
            elif self._stack:
                if self._path:
                    self._path.pop()
                self._state = _State.KEY
        elif ch == '}':
            self._end_object()
        elif ch == ']':
            self._end_array()
        # else: whitespace, ignore

    def _on_value_or_end(self, ch: str) -> None:
        if ch == ']':
            self._end_array()
        elif ch not in ' \t\n\r':
            self._state = _State.VALUE
            self._process_char(ch)
        # else: whitespace, stay in VALUE_OR_END

    def _on_number(self, ch: str) -> None:
        if ch in '0123456789.eE+-':
            self._buffer += ch
        else:
            raw = self._buffer
            try:
                num: int | float = (
                    int(raw) if '.' not in raw and 'e' not in raw.lower()
                    else float(raw)
                )
            except ValueError:
                num = float(raw)
            self._complete_scalar(num, raw)
            self._buffer = ""
            self._state = _State.COMMA
            self._process_char(ch)  # reprocess the terminator char

    def _on_true(self, ch: str) -> None:
        _expected = {_State.TRUE1: 'r', _State.TRUE2: 'u', _State.TRUE3: 'e'}
        _next = {_State.TRUE1: _State.TRUE2, _State.TRUE2: _State.TRUE3}
        s = self._state
        if ch == _expected[s]:
            self._buffer += ch
            if s == _State.TRUE3:
                self._complete_scalar(True, "true")
                self._buffer = ""
                self._state = _State.COMMA
            else:
                self._state = _next[s]

    def _on_false(self, ch: str) -> None:
        _expected = {
            _State.FALSE1: 'a', _State.FALSE2: 'l',
            _State.FALSE3: 's', _State.FALSE4: 'e',
        }
        _next = {
            _State.FALSE1: _State.FALSE2, _State.FALSE2: _State.FALSE3,
            _State.FALSE3: _State.FALSE4,
        }
        s = self._state
        if ch == _expected[s]:
            self._buffer += ch
            if s == _State.FALSE4:
                self._complete_scalar(False, "false")
                self._buffer = ""
                self._state = _State.COMMA
            else:
                self._state = _next[s]

    def _on_null(self, ch: str) -> None:
        _expected = {_State.NULL1: 'u', _State.NULL2: 'l', _State.NULL3: 'l'}
        _next = {_State.NULL1: _State.NULL2, _State.NULL2: _State.NULL3}
        s = self._state
        if ch == _expected[s]:
            self._buffer += ch
            if s == _State.NULL3:
                self._complete_scalar(None, "null")
                self._buffer = ""
                self._state = _State.COMMA
            else:
                self._state = _next[s]

    def _end_object(self) -> None:
        if len(self._stack) > 1:
            self._stack.pop()
        if self._path and not isinstance(self._path[-1], int):
            self._path.pop()
        self._state = _State.COMMA

    def _end_array(self) -> None:
        if len(self._stack) > 1:
            self._stack.pop()
        if self._array_indexes:
            self._array_indexes.pop()
        if self._path:
            self._path.pop()
        self._state = _State.COMMA
