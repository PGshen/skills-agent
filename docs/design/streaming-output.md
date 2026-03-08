# 流式输出架构 设计

**状态**：Phase A（CLISink 基础版）→ Phase C（流式文本 + SSESink）分阶段实现

---

## 1. 设计目标

输出系统需满足以下要求，且**不要求修改 Agent Core 代码**即可扩展：

| 场景 | 要求 |
|------|------|
| CLI（当前阶段） | 执行过程中实时打印进度，最终答案流式逐字打印 |
| API 服务（未来） | HTTP SSE 流式推送，前端可实时消费 |
| 测试 | 零副作用，不需要 mock/patch |

**核心手段**：`OutputSink` 接口将 Agent Core 与具体输出方式完全解耦。Agent Core 只调用接口方法，不感知是 CLI、HTTP 还是测试环境。

---

## 2. 整体架构

```
┌──────────────────────────────────────────────────────────┐
│                      Agent Core                           │
│                                                           │
│   执行动作时调用 self._sink.on_XXX()                       │
│                            │                              │
└────────────────────────────┼──────────────────────────────┘
                             │
                    OutputSink（接口）
                        ├── NullSink      → 不输出（测试 / 默认）
                        ├── CLISink       → 终端输出（当前阶段）
                        └── SSESink       → HTTP SSE（Phase C 预留）
```

---

## 3. OutputSink 接口

```python
# src/output/sink.py
from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..agent.plan import Plan


class OutputSink:
    """
    输出接收器基类。
    所有方法提供空实现（pass），子类只需覆盖关心的方法。
    直接用作 NullSink（测试时不需要子类或 mock）。

    设计原则：
    - 方法调用总是幂等且无副作用（除了输出）
    - 方法调用不应抛出异常（实现者负责内部异常处理）
    - 方法调用是同步的（Phase C 若需要异步，由实现者自行处理）
    """

    def on_progress(self, action: str, detail: str = "") -> None:
        """
        Agent 开始执行某个动作。
        action: 动作类型名称，如 "load_skill"、"run_script"
        detail: 补充信息，如技能名称、脚本路径
        """

    def on_plan_updated(self, plan: "Plan") -> None:
        """
        Plan 被更新（创建或全量替换）。
        plan: 新的 Plan 对象，包含当前所有步骤及其状态
        """

    def on_text_chunk(self, chunk: str, done: bool) -> None:
        """
        最终答案的流式文本片段。
        chunk: 本次新增的文本内容（Phase A 为完整答案，Phase C 为逐字片段）
        done: True 表示答案已完整生成
        """

    def on_observation(self, source: str, content: str) -> None:
        """
        工具/脚本执行结果摘要。
        source: 来源标识，如 "skill"、"script"、"resource"
        content: 结果摘要（已截断到合理长度）
        """

    def on_error(self, message: str, recoverable: bool = True) -> None:
        """
        错误发生。
        recoverable: True 表示 Agent 将继续尝试（如重试），False 表示致命错误
        """

    def on_session_end(self, turn_count: int, status: str) -> None:
        """
        会话/任务结束通知。
        status: "completed" / "failed" / "dead_loop" / "max_turns"
        """


# NullSink = OutputSink 基类本身（空实现即可）
NullSink = OutputSink
```

---

## 4. CLISink（终端实现）

### 4.1 设计原则

- **进度信息 → stderr**：不污染 stdout 管道，`skills-agent run "q" > out.txt` 时进度仍显示在终端
- **最终答案 → stdout**：可被管道或重定向捕获
- **流式打印**：`on_text_chunk` 使用 `sys.stdout.write + flush`，不缓冲
- **verbose 模式**：`--verbose` 时显示 observation；默认不显示（减少噪音）

### 4.2 完整实现

```python
# src/output/cli_sink.py
import sys
from .sink import OutputSink
from ..agent.plan import Plan, StepStatus

# ANSI 颜色码
_DIM   = "\033[2m"
_RED   = "\033[31m"
_YELLOW = "\033[33m"
_RESET = "\033[0m"

# 步骤状态符号
_STEP_ICON = {
    StepStatus.PENDING:     "○",
    StepStatus.IN_PROGRESS: "→",
    StepStatus.DONE:        "✓",
    StepStatus.FAILED:      "✗",
}


class CLISink(OutputSink):
    """
    终端输出实现。

    使用方：
        sink = CLISink(verbose=args.verbose)
        core = AgentCore(..., sink=sink)
        core.run("query")
    """

    def __init__(self, verbose: bool = False, color: bool = True):
        self._verbose = verbose
        self._color = color and sys.stderr.isatty()
        self._answer_started = False  # 是否已开始输出答案（用于首次换行）

    def _dim(self, text: str) -> str:
        return f"{_DIM}{text}{_RESET}" if self._color else text

    def _red(self, text: str) -> str:
        return f"{_RED}{text}{_RESET}" if self._color else text

    def _yellow(self, text: str) -> str:
        return f"{_YELLOW}{text}{_RESET}" if self._color else text

    # ── 进度（→ stderr）────────────────────────────────────────────────

    def on_progress(self, action: str, detail: str = "") -> None:
        label = f"{action}: {detail}" if detail else action
        print(self._dim(f"  → {label}"), file=sys.stderr)

    def on_plan_updated(self, plan: Plan) -> None:
        print(self._dim("[Plan]"), file=sys.stderr)
        for step in plan.steps:
            icon = _STEP_ICON.get(step.status, "?")
            line = f"  {icon} {step.description}"
            if step.status == StepStatus.IN_PROGRESS:
                line = self._dim(line)
            print(line, file=sys.stderr)

    def on_observation(self, source: str, content: str) -> None:
        if not self._verbose:
            return
        # 截断长观察，避免刷屏
        preview = content[:300].replace("\n", " ")
        if len(content) > 300:
            preview += "..."
        print(self._dim(f"  [obs/{source}] {preview}"), file=sys.stderr)

    def on_error(self, message: str, recoverable: bool = True) -> None:
        if recoverable:
            print(self._yellow(f"[WARN] {message}"), file=sys.stderr)
        else:
            print(self._red(f"[ERROR] {message}"), file=sys.stderr)

    def on_session_end(self, turn_count: int, status: str) -> None:
        print(
            self._dim(f"\nCompleted in {turn_count} turns ({status})."),
            file=sys.stderr,
        )

    # ── 最终答案（→ stdout）────────────────────────────────────────────

    def on_text_chunk(self, chunk: str, done: bool) -> None:
        if not self._answer_started and chunk:
            # 首个 chunk 前确保进度行已换行
            print(file=sys.stderr, flush=True)
            self._answer_started = True

        sys.stdout.write(chunk)
        sys.stdout.flush()

        if done:
            # 答案结束，确保末尾有换行
            if chunk and not chunk.endswith("\n"):
                sys.stdout.write("\n")
                sys.stdout.flush()
            self._answer_started = False
```

### 4.3 CLI 参数集成

在 `argparse` 中添加：

```python
parser.add_argument("--verbose", "-v", action="store_true",
                    help="Show tool observations and internal details")
parser.add_argument("--no-color", action="store_true",
                    help="Disable colored output")
```

构造 CLISink：

```python
sink = CLISink(verbose=args.verbose, color=not args.no_color)
```

---

## 5. SSESink（HTTP SSE 实现）

### 5.1 设计目标

SSESink 将每次 OutputSink 方法调用转为标准 SSE 格式字符串，写入一个 `write_fn` 回调。

调用者只需提供 `write_fn`（如将字符串推入 HTTP 响应流的函数），无需了解 OutputSink 的内部实现。

### 5.2 完整实现

```python
# src/output/sse_sink.py
import json
from typing import Callable
from .sink import OutputSink
from ..agent.plan import Plan


class SSESink(OutputSink):
    """
    Server-Sent Events 输出接收器。

    SSE 格式：每个事件为 "data: {json}\n\n"

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
```

### 5.3 SSE 事件清单

| 事件类型 | 触发时机 | 关键字段 |
|----------|----------|----------|
| `progress` | 开始执行动作 | `action`, `detail` |
| `plan_updated` | Plan 创建/更新 | `goal`, `steps[]` |
| `text_chunk` | 答案流式片段（done=False） | `content` |
| `text_done` | 答案完整（done=True） | — |
| `observation` | 工具/脚本结果 | `source`, `content` |
| `error` | 错误 | `message`, `recoverable` |
| `session_end` | 任务/会话结束 | `turn_count`, `status` |

### 5.4 未来 API 服务集成

当需要对外提供 HTTP API 时，Agent Core **无需任何修改**：

```python
# 伪代码（FastAPI）
@app.post("/v1/run")
async def run_agent(body: RunRequest):
    async def generate():
        events = []
        sink = SSESink(write_fn=events.append)
        # AgentCore 是同步的，可在线程中运行
        await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: AgentCore(sink=sink).run(body.query, history_messages=...),
        )
        for event in events:
            yield event

    return StreamingResponse(generate(), media_type="text/event-stream")
```

---

## 6. Agent Core 集成

### 6.1 构造参数

```python
class AgentCore:
    def __init__(
        self,
        model: ModelAdapter,
        registry: SkillRegistry,
        loader: SkillLoader,
        event_logger: EventLogger,
        sink: OutputSink = None,   # ← 新增，默认 NullSink
        max_turns: int = 50,
        ...
    ):
        self._sink = sink or NullSink()
```

### 6.2 调用点清单

| 调用位置 | 调用方法 | 说明 |
|----------|----------|------|
| 创建初始 Plan 后 | `on_plan_updated(plan)` | 展示初始步骤列表 |
| 执行 LOAD_SKILL 前 | `on_progress("load_skill", skill_name)` | |
| 执行 LOAD_SKILL 后 | `on_observation("skill", f"loaded {N} chars")` | verbose 模式 |
| 执行 LOAD_RESOURCE 前 | `on_progress("load_resource", path)` | |
| 执行 RUN_SCRIPT 前 | `on_progress("run_script", script)` | |
| 执行 RUN_SCRIPT 后 | `on_observation("script", result.stdout[:300])` | verbose 模式 |
| 执行 UPDATE_PLAN 后 | `on_plan_updated(state.plan)` | |
| 检测到错误时 | `on_error(message, recoverable)` | |
| 检测到死循环时 | `on_error("Dead loop detected", recoverable=False)` | |
| 输出最终答案（Phase A） | `on_text_chunk(answer, done=True)` | 一次性输出 |
| 输出最终答案（Phase C） | `on_text_chunk(chunk, done)` | 流式逐字 |
| 任务结束 | `on_session_end(turn_count, status)` | |

### 6.3 Phase C：流式答案接入

Phase C 中，`AnthropicAdapter` 通过 `StreamingJSONParser` 的回调将答案流式传给 sink：

```python
class AnthropicAdapter(ModelAdapter):
    def __init__(self, ..., sink: OutputSink = None):
        self._sink = sink or NullSink()

    def next_action(self, messages: list[dict]) -> Action:
        def on_answer_chunk(path: str, value: str, done: bool) -> None:
            self._sink.on_text_chunk(value, done)

        parser = StreamingJSONParser(callbacks={
            "$.final_answer.content": on_answer_chunk,
        })

        with anthropic_client.stream(...) as stream:
            for chunk in stream.text_stream:
                parser.feed(chunk)

        return self._build_action(parser.get_result())
```

---

## 7. 各阶段实现范围

| 功能 | Phase A | Phase B | Phase C |
|------|---------|---------|---------|
| OutputSink 接口 + NullSink | ✅ | — | — |
| CLISink（进度输出） | ✅ | — | — |
| CLISink（one-shot 答案） | ✅ | — | — |
| CLISink（流式逐字答案） | — | — | ✅ |
| SSESink | — | — | ✅ |
| StreamingJSONParser 接入 | — | — | ✅ |

Phase A 的 `on_text_chunk` 行为：`FINAL_ANSWER` 动作执行完成后，一次性调用 `on_text_chunk(full_answer, done=True)`，非流式（MockModel 不支持流式，且 Phase A 尚无流式 JSON 解析器）。

Phase C 起，`AnthropicAdapter` 通过流式 JSON 解析器回调逐字触发 `on_text_chunk`，实现真正的流式输出。

---

## 8. 测试策略

### 8.1 NullSink（无需测试）

`NullSink` 是空实现，无副作用，无需专门测试。

### 8.2 CLISink 单元测试

通过 `capsys`（pytest）捕获 stdout/stderr：

```python
def test_cli_sink_progress_to_stderr(capsys):
    sink = CLISink(color=False)
    sink.on_progress("load_skill", "data-analysis")
    captured = capsys.readouterr()
    assert "load_skill: data-analysis" in captured.err
    assert captured.out == ""  # 进度不写 stdout

def test_cli_sink_text_to_stdout(capsys):
    sink = CLISink(color=False)
    sink.on_text_chunk("hello world", done=True)
    captured = capsys.readouterr()
    assert "hello world" in captured.out
    assert captured.err == "" or "Completed" in captured.err

def test_cli_sink_observation_hidden_by_default(capsys):
    sink = CLISink(verbose=False, color=False)
    sink.on_observation("script", "some output")
    captured = capsys.readouterr()
    assert "some output" not in captured.err

def test_cli_sink_observation_visible_verbose(capsys):
    sink = CLISink(verbose=True, color=False)
    sink.on_observation("script", "some output")
    captured = capsys.readouterr()
    assert "some output" in captured.err
```

### 8.3 SSESink 单元测试

```python
def test_sse_sink_event_format():
    events = []
    sink = SSESink(write_fn=events.append)

    sink.on_progress("load_skill", "data-analysis")
    sink.on_text_chunk("hello", done=False)
    sink.on_text_chunk("", done=True)
    sink.on_session_end(3, "completed")

    assert len(events) == 4
    # 格式检查：每个事件以 "data: " 开头，以 "\n\n" 结尾
    for event in events:
        assert event.startswith("data: ")
        assert event.endswith("\n\n")

    import json
    data = json.loads(events[0][6:])  # 去掉 "data: " 前缀
    assert data == {"type": "progress", "action": "load_skill", "detail": "data-analysis"}

    data = json.loads(events[1][6:])
    assert data["type"] == "text_chunk"
    assert data["content"] == "hello"

    data = json.loads(events[2][6:])
    assert data["type"] == "text_done"

def test_sse_sink_plan_updated():
    from ..agent.plan import Plan, Step, StepStatus
    events = []
    sink = SSESink(write_fn=events.append)

    plan = Plan(goal="test goal", steps=[
        Step(id="s1", description="first step", status=StepStatus.PENDING),
    ])
    sink.on_plan_updated(plan)

    import json
    data = json.loads(events[0][6:])
    assert data["type"] == "plan_updated"
    assert data["goal"] == "test goal"
    assert data["steps"][0]["id"] == "s1"
```

### 8.4 Agent Core 集成测试（RecordSink）

用自定义 sink 验证 Agent Core 在正确时机调用了正确方法：

```python
class RecordSink(OutputSink):
    def __init__(self):
        self.calls = []

    def on_progress(self, action, detail=""):
        self.calls.append(("progress", action, detail))

    def on_plan_updated(self, plan):
        self.calls.append(("plan_updated", len(plan.steps)))

    def on_text_chunk(self, chunk, done):
        self.calls.append(("chunk", chunk, done))

    def on_session_end(self, turn_count, status):
        self.calls.append(("end", turn_count, status))

def test_agent_core_calls_sink_in_order():
    sink = RecordSink()
    mock = MockModel(responses=[
        Action(type=ActionType.UPDATE_PLAN, params={"plan": {...}}),
        Action(type=ActionType.FINAL_ANSWER, params={"content": "done"}),
    ])
    core = AgentCore(model=mock, ..., sink=sink)
    core.run("test query")

    call_types = [c[0] for c in sink.calls]
    assert "plan_updated" in call_types
    assert "chunk" in call_types
    assert call_types[-1] == "end"
```

---

## 9. 验收标准

### Phase A

- [ ] `OutputSink` + `NullSink` 单元测试通过
- [ ] `CLISink` 单元测试通过（进度→stderr，答案→stdout）
- [ ] `AgentCore` 集成测试：RecordSink 验证调用顺序正确
- [ ] `skills-agent run "test"` 时 stderr 可见进度行
- [ ] `skills-agent run "test" 2>/dev/null` 只有答案出现在 stdout

### Phase C

- [ ] `SSESink` 单元测试通过，事件格式符合 SSE 规范
- [ ] 真实 API 调用时 `on_text_chunk` 被多次触发（非 done=True 一次性）
- [ ] `CLISink` 流式答案逐字显示，非等待完整响应后一次性打印
