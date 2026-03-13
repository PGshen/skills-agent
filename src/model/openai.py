"""OpenAIAdapter: OpenAI API integration (Phase C)."""
import json
import logging

from openai import OpenAI

logger = logging.getLogger(__name__)

from agent.plan import Action
from output.sink import NullSink, OutputSink

from .base import ModelAdapter, parse_action_response
from .streaming import StreamingJSONParser

# Strict JSON Schema for the Action response.
# Forces the model to always return {"type": "<enum>", "params": {...}}.
# All params fields are nullable so strict mode can require them all.
_ACTION_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "agent_action",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "type": {
                    "type": "string",
                    "enum": [
                        "load_skill",
                        "load_resource",
                        "run_script",
                        "update_plan",
                        "final_answer",
                    ],
                },
                "params": {
                    "type": "object",
                    "properties": {
                        "skill_name":   {"type": ["string", "null"]},
                        "resource":     {"type": ["string", "null"]},
                        "section_hint": {"type": ["string", "null"]},
                        "script":       {"type": ["string", "null"]},
                        "args": {
                            "anyOf": [
                                {"type": "null"},
                                {"type": "array", "items": {"type": "string"}},
                            ]
                        },
                        "content": {"type": ["string", "null"]},
                        "plan": {
                            "anyOf": [
                                {"type": "null"},
                                {
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
                                },
                            ]
                        },
                    },
                    "required": [
                        "skill_name", "resource", "section_hint",
                        "script", "args", "content", "plan",
                    ],
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
    """

    def __init__(
        self,
        api_key: str = None,          # None: reads from OPENAI_API_KEY env var
        model: str = "gpt-4o",
        max_tokens: int = 4096,
        sink: OutputSink = None,      # streaming answer chunk callback
    ):
        self._client = OpenAI(api_key=api_key)
        self._model = model
        self._max_tokens = max_tokens
        self._sink = sink or NullSink()

    def next_action(self, messages: list[dict]) -> Action:
        """Non-streaming call. AgentCore._execute_action() emits the text chunk."""
        logger.debug(
            "request model=%s messages=%s",
            self._model,
            json.dumps(messages, ensure_ascii=False),
        )
        response = self._client.chat.completions.create(
            model=self._model,
            max_tokens=self._max_tokens,
            messages=messages,
            response_format=_ACTION_RESPONSE_FORMAT,
        )
        raw = response.choices[0].message.content
        logger.debug("response model=%s raw=%s", self._model, raw)
        return parse_action_response(raw)

    def next_action_streaming(self, messages: list[dict]) -> Action:
        """
        Streaming call (Phase C full version).
        Parses tokens in real time via StreamingJSONParser,
        calling sink.on_text_chunk() character-by-character on $.params.content.
        """
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
        stream = self._client.chat.completions.create(
            model=self._model,
            max_tokens=self._max_tokens,
            messages=messages,
            stream=True,
            response_format=_ACTION_RESPONSE_FORMAT,
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
