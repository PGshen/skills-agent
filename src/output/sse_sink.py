"""SSESink: HTTP Server-Sent Events output (Phase C)."""
import json
from typing import Callable
from .sink import OutputSink
from ..agent.plan import Plan


class SSESink(OutputSink):
    """
    Server-Sent Events 输出接收器。

    SSE 格式：每个事件为 "data: {json}\\n\\n"

    使用方（FastAPI StreamingResponse 示例）：
        async def stream():
            buffer = []
            sink = SSESink(write_fn=buffer.append)
            AgentCore(sink=sink).run(user_input)
            for event in buffer:
                yield event

        return StreamingResponse(stream(), media_type="text/event-stream")
    """

    def __init__(self, write_fn: Callable[[str], None]):
        self._write = write_fn

    def _emit(self, event_type: str, **data) -> None:
        payload = json.dumps({"type": event_type, **data}, ensure_ascii=False)
        self._write(f"data: {payload}\n\n")

    def on_progress(self, action: str, detail: str = "") -> None:
        self._emit("progress", action=action, detail=detail)

    def on_plan_updated(self, plan: Plan) -> None:
        steps = [
            {
                "id": s.id,
                "description": s.description,
                "status": s.status.value,
                "notes": s.notes,
            }
            for s in plan.steps
        ]
        self._emit("plan_updated", goal=plan.goal, steps=steps)

    def on_text_chunk(self, chunk: str, done: bool) -> None:
        if done:
            self._emit("text_done")
        else:
            self._emit("text_chunk", content=chunk)

    def on_observation(self, source: str, content: str) -> None:
        self._emit("observation", source=source, content=content[:500])

    def on_error(self, message: str, recoverable: bool = True) -> None:
        self._emit("error", message=message, recoverable=recoverable)

    def on_session_end(self, turn_count: int, status: str) -> None:
        self._emit("session_end", turn_count=turn_count, status=status)
