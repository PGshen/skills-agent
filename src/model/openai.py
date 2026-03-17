"""OpenAIAdapter: OpenAI API integration (Phase C)."""
import json
import logging

from openai import OpenAI

logger = logging.getLogger(__name__)

from agent.plan import Action
from output.sink import NullSink, OutputSink

from .base import ModelAdapter, parse_action_response
from .streaming import StreamingJSONParser


# ── Per-action param field definitions ───────────────────────────────────────

_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "goal": {"type": "string"},
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id":          {"type": "string"},
                    "description": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": ["pending", "in_progress", "done", "failed"],
                    },
                    "notes": {"type": ["string", "null"]},
                },
                "required": ["id", "description", "status", "notes"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["goal", "steps"],
    "additionalProperties": False,
}

_PARAM_SCHEMAS: dict[str, dict] = {
    "skill_name":   {"type": ["string", "null"]},
    "resource":     {"type": ["string", "null"]},
    "section_hint": {"type": ["string", "null"]},
    "script":       {"type": ["string", "null"]},
    "args":         {"anyOf": [{"type": "null"}, {"type": "array", "items": {"type": "string"}}]},
    "content":      {"type": ["string", "null"]},
    "route":        {"type": ["string", "null"]},
    "plan":         {"anyOf": [{"type": "null"}, _PLAN_SCHEMA]},
    "path":         {"type": ["string", "null"]},
    "max_bytes":    {"type": ["integer", "null"]},
    "max_entries":  {"type": ["integer", "null"]},
    "pattern":      {"type": ["string", "null"]},
    "max_results":  {"type": ["integer", "null"]},
    "query":        {"type": ["string", "null"]},
}

# Params needed by each action type
_ACTION_PARAMS: dict[str, list[str]] = {
    "load_skill":    ["skill_name"],
    "load_resource": ["skill_name", "resource", "section_hint"],
    "run_script":    ["skill_name", "script", "args"],
    "update_plan":   ["plan"],
    "final_answer":  ["content", "route"],
    "read_file":     ["path", "max_bytes"],
    "list_dir":      ["path", "max_entries"],
    "grep":          ["pattern", "path", "max_results"],
    "write_file":    ["path", "content"],
    "delete_file":   ["path"],
    "web_search":    ["query", "max_results"],
}


def build_action_response_format(action_types: list[str]) -> dict:
    """
    Build an OpenAI structured-output response_format for the given action types.

    Only the action types listed are allowed in the 'type' enum.
    Only the param fields needed by those action types are included in the schema.
    All included param fields are nullable so OpenAI strict mode can require them all.
    """
    # Union of param fields needed by all requested action types
    needed_params: set[str] = set()
    for at in action_types:
        needed_params.update(_ACTION_PARAMS.get(at, []))

    param_properties = {k: _PARAM_SCHEMAS[k] for k in needed_params if k in _PARAM_SCHEMAS}
    param_required = sorted(param_properties)  # stable order; all nullable so always safe

    return {
        "type": "json_schema",
        "json_schema": {
            "name": "agent_action",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": action_types,
                    },
                    "params": {
                        "type": "object",
                        "properties": param_properties,
                        "required": param_required,
                        "additionalProperties": False,
                    },
                },
                "required": ["type", "params"],
                "additionalProperties": False,
            },
        },
    }


class OpenAIAdapter(ModelAdapter):
    """
    OpenAI API adapter (Phase C).
    Supports non-streaming and streaming call modes.

    response_format: default structured-output schema passed to every call.
    Callers can override per-call by passing response_format to next_action /
    next_action_streaming.  Use build_action_response_format() to build schemas
    tailored to the action types actually needed.
    """

    def __init__(
        self,
        api_key: str = None,          # None: reads from OPENAI_API_KEY env var
        model: str = "gpt-4o",
        max_tokens: int = 4096,
        sink: OutputSink = None,      # streaming answer chunk callback
        response_format: dict = None,
    ):
        self._client = OpenAI(api_key=api_key)
        self._model = model
        self._max_tokens = max_tokens
        self._sink = sink or NullSink()
        self._default_response_format = response_format

    def next_action(self, messages: list[dict], response_format: dict = None) -> Action:
        """Non-streaming call. AgentCore._execute_action() emits the text chunk."""
        fmt = response_format or self._default_response_format
        logger.debug(
            "request model=%s messages=%s",
            self._model,
            json.dumps(messages, ensure_ascii=False),
        )
        kwargs = {}
        if fmt is not None:
            kwargs["response_format"] = fmt
        response = self._client.chat.completions.create(
            model=self._model,
            max_tokens=self._max_tokens,
            messages=messages,
            **kwargs,
        )
        raw = response.choices[0].message.content
        logger.debug("response model=%s raw=%s", self._model, raw)
        return parse_action_response(raw)

    def next_action_streaming(self, messages: list[dict], response_format: dict = None) -> Action:
        """
        Streaming call (Phase C full version).
        Parses tokens in real time via StreamingJSONParser,
        calling sink.on_text_chunk() character-by-character on $.params.content.
        """
        fmt = response_format or self._default_response_format

        def on_answer_chunk(_path: str, value: str, done: bool) -> None:
            # Send "" when done=True — content was already emitted char-by-char via
            # done=False deltas; passing the full value here would cause CLISink to
            # print the entire answer a second time.
            self._sink.on_text_chunk("" if done else value, done)

        parser = StreamingJSONParser(path_callbacks={
            "$.params.content": on_answer_chunk,
        })

        logger.debug(
            "request(stream) model=%s messages=%s",
            self._model,
            json.dumps(messages, ensure_ascii=False),
        )
        kwargs = {}
        if fmt is not None:
            kwargs["response_format"] = fmt
        stream = self._client.chat.completions.create(
            model=self._model,
            max_tokens=self._max_tokens,
            messages=messages,
            stream=True,
            **kwargs,
        )
        raw_chunks: list[str] = []
        for chunk in stream:
            delta = chunk.choices[0].delta.content or ""
            if delta:
                parser.feed(delta)
                raw_chunks.append(delta)

        raw = "".join(raw_chunks)
        logger.debug("response(stream) model=%s raw=%s", self._model, raw)
        return parse_action_response(raw)
