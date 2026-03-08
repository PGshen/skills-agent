## Phase C: 真实 LLM 适配器 + 流式 JSON 解析器

### 任务 C1.1: 流式 JSON 解析器（FSM）

**目标**: 实现基于有限状态机的流式 JSON 解析器，支持 JSONPath 路径匹配与增量回调

**输出文件**: `src/model/streaming.py`

**背景说明**:

调研结论：无合适三方库同时满足以下需求：
- 流式（逐字符/逐 chunk 输入）
- JSONPath 风格的路径匹配
- 匹配到目标路径时触发增量回调（不等待整个 JSON 完成）

因此自行实现 FSM 解析器。

**模型输出 JSON 格式**（与 Action 结构对应）：
```json
{"type": "final_answer", "params": {"content": "..."}}
```

**目标路径（Phase C MVP）**:
- `$.type` → 解析到 action type 时立即触发（提前决定后续处理逻辑）
- `$.params.content` → 流式输出最终答案 content（逐字回调，实时显示）
- `$.params.skill_name` → 解析到 skill_name 时回调

**接口**:

```python
# src/model/streaming.py
from typing import Callable

# 回调签名：(path: str, value: str, done: bool) -> None
# done=False 时 value 是部分字符串（delta）；done=True 时 value 是完整值
PathCallback = Callable[[str, str, bool], None]

class StreamingJSONParser:
    """
    FSM 流式 JSON 解析器。
    path_callbacks: 路径 → 回调函数映射。

    使用方：
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

    def __init__(
        self,
        path_callbacks: dict[str, PathCallback] = None,
    ):
        self._callbacks = path_callbacks or {}
        # FSM 内部状态...

    def feed(self, chunk: str) -> None:
        """接收一个字符串 chunk，更新状态机，触发命中的回调。"""
        ...

    def get_result(self) -> dict:
        """流结束后返回完整的 JSON 对象（用于最终校验）。"""
        ...

    def reset(self) -> None:
        """重置状态机（新一轮调用前使用）。"""
        ...
```

**FSM 状态设计**（基本骨架）:
- `START`：等待 `{`
- `OBJECT_KEY`：读取 key 字符串
- `OBJECT_COLON`：等待 `:`
- `OBJECT_VALUE`：根据首字符分发（string/number/boolean/null/object/array）
- `STRING`：读取字符串直到非转义 `"`，对已注册路径触发 delta 回调
- `END`：解析完成

**验收标准**:
- [ ] 分多个 chunk 输入完整 JSON，`get_result()` 返回正确 Python dict
- [ ] `$.type` 回调在解析到该路径时触发，不等整个 JSON 完成
- [ ] `$.params.content` 在字符串流式输入时逐字触发（`done=False`），完成时触发 `done=True`
- [ ] 处理嵌套对象、转义字符
- [ ] 输入完整 JSON（非流式）时行为与标准 `json.loads()` 相同
- [ ] delta 回调拼接结果与最终 `get_result()["params"]["content"]` 一致

**测试用例** (`tests/unit/test_streaming.py`):
```python
def test_full_json_equals_json_loads():
    data = '{"type": "load_skill", "params": {"skill_name": "test"}}'
    parser = StreamingJSONParser()
    parser.feed(data)
    assert parser.get_result() == json.loads(data)

def test_chunked_input():
    chunks = ['{"type": "load_s', 'kill", "params": {', '"skill_name": "test"}}']
    parser = StreamingJSONParser()
    for chunk in chunks:
        parser.feed(chunk)
    assert parser.get_result()["type"] == "load_skill"

def test_path_callback_fires_on_type():
    fired = []
    def on_type(path, val, done):
        if done:
            fired.append(val)

    parser = StreamingJSONParser(path_callbacks={"$.type": on_type})
    parser.feed('{"type": "final_answer", "params": {"content": "done"}}')
    assert "final_answer" in fired

def test_streaming_content_delta():
    deltas = []
    def on_content(path, val, done):
        deltas.append((val, done))

    parser = StreamingJSONParser(path_callbacks={"$.params.content": on_content})
    parser.feed('{"type": "final_answer", "params": {"content": "hello world"}}')
    # 最后一次 done=True，拼接所有 val 等于完整内容
    full = "".join(v for v, _ in deltas)
    assert "hello world" in full
    assert any(done for _, done in deltas)
```

---

### 任务 C2.1: Anthropic Model Adapter

**目标**: 实现基于 Anthropic API 的真实模型适配器，支持 tool use 和 JSON mode

**输入**: A3.2（ModelAdapter 接口）、C1.1（StreamingJSONParser）

**输出文件**: `src/model/anthropic.py`

**背景说明**:

结构化输出优先级（从最可靠到最脆弱）：
1. **Tool use / Function calling**（首选）：将 `Action` 的结构定义注册为工具，模型通过函数调用返回结构化动作，最可靠。
2. **Prompt-only**（兜底）：系统 prompt 中要求输出特定格式 JSON，用 `parse_action_response()` 提取。

Phase C MVP 实现 1（tool use）和 2（prompt-only 兜底），通过 `RetryAdapter` 包装增强鲁棒性。

**接口**:

```python
# src/model/anthropic.py
import anthropic
from .base import ModelAdapter, parse_action_response, ModelResponseError
from ..agent.plan import Action, ActionType
from ..output.sink import OutputSink, NullSink
from .streaming import StreamingJSONParser

class AnthropicAdapter(ModelAdapter):
    """
    Anthropic Claude API 适配器（Phase C）。
    支持非流式与流式两种调用模式。
    """

    def __init__(
        self,
        api_key: str = None,             # None 时从 ANTHROPIC_API_KEY 读取
        model: str = "claude-sonnet-4-6",
        max_tokens: int = 4096,
        sink: OutputSink = None,         # 流式答案时逐字回调
    ):
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model
        self._max_tokens = max_tokens
        self._sink = sink or NullSink()

    def next_action(self, messages: list[dict]) -> Action:
        """非流式调用（Phase C 初期）。"""
        system_msg = None
        user_messages = []
        for m in messages:
            if m["role"] == "system":
                system_msg = m["content"]
            else:
                user_messages.append(m)

        response = self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            system=system_msg or "",
            messages=user_messages,
        )
        raw = response.content[0].text
        action = parse_action_response(raw)

        # FINAL_ANSWER 时调用 on_text_chunk（Phase C 流式版中由 StreamingJSONParser 逐字触发）
        if action.type == ActionType.FINAL_ANSWER:
            self._sink.on_text_chunk(action.params.get("content", ""), done=True)

        return action

    def next_action_streaming(self, messages: list[dict]) -> Action:
        """
        流式调用（Phase C 完整版）。
        通过 StreamingJSONParser 实时解析 token，
        在解析到 $.params.content 时逐字调用 sink.on_text_chunk()。
        """
        def on_answer_chunk(path: str, value: str, done: bool) -> None:
            self._sink.on_text_chunk(value, done)

        parser = StreamingJSONParser(path_callbacks={
            "$.params.content": on_answer_chunk,
        })

        with self._client.messages.stream(
            model=self._model,
            max_tokens=self._max_tokens,
            messages=messages,
        ) as stream:
            for chunk in stream.text_stream:
                parser.feed(chunk)

        return _validate_action(parser.get_result())
```

**验收标准**:
- [ ] 设置 `ANTHROPIC_API_KEY` 后，`AnthropicAdapter.next_action()` 可真实调用 API（集成测试）
- [ ] 非流式：API 返回合法 JSON 时正确解析为 `Action`
- [ ] 流式：`$.params.content` 回调触发 `sink.on_text_chunk()` 多次（`done=False`），结束时 `done=True`
- [ ] 与 `RetryAdapter` 组合时，解析失败后自动重试并追加纠错消息
- [ ] API key 未设置时抛出清晰错误（`anthropic.AuthenticationError`）

---

### 任务 C2.2: 流式输出接入（StreamingJSONParser → CLISink）

**目标**: 将流式 JSON 解析器的 `$.final_answer.content` 回调接入 CLISink，实现最终答案逐字打印

**输入**: C1.1（StreamingJSONParser）、A4.2（CLISink）、C2.1（AnthropicAdapter）

**修改文件**: `src/model/anthropic.py`

**实现逻辑**:

`AnthropicAdapter.next_action_streaming()` 中向 `StreamingJSONParser` 注册 `$.params.content` 路径回调，每次收到 delta 时调用 `sink.on_text_chunk()`：

```python
def next_action_streaming(self, messages: list[dict]) -> Action:
    def on_answer_chunk(path: str, value: str, done: bool) -> None:
        self._sink.on_text_chunk(value, done)

    parser = StreamingJSONParser(path_callbacks={
        "$.params.content": on_answer_chunk,   # 仅在 final_answer 时有内容
    })

    with self._client.messages.stream(
        model=self._model, max_tokens=self._max_tokens, messages=messages
    ) as stream:
        for chunk in stream.text_stream:
            parser.feed(chunk)

    return parse_action_response(json.dumps(parser.get_result()))
```

Phase A/B 中 MockModel 不支持流式，`on_text_chunk` 在 FINAL_ANSWER 动作完成后被一次性调用（`done=True`）。Phase C 起改为逐字触发。

**验收标准**:
- [ ] 真实 API 流式调用时，最终答案逐字打印到终端（非等待完整响应后一次性输出）
- [ ] `on_text_chunk(chunk, done=False)` 在生成过程中多次触发
- [ ] `on_text_chunk("", done=True)` 在答案完整后触发一次
- [ ] `CLISink` 和 `SSESink` 均能正确处理流式 chunk（无需修改 sink 代码）

---

### 任务 C2.3: SSESink（API 流式输出预留）

**目标**: 实现面向 HTTP SSE 的 OutputSink，供未来 API 服务接入，无需修改 Agent Core

**输出文件**: `src/output/sse_sink.py`

**背景说明**:

详见 [流式输出设计](../design/streaming-output.md)。SSESink 将 OutputSink 方法调用转换为 SSE 格式事件，写入一个可写对象（`write_fn`）。使用者只需提供 `write_fn`（如 FastAPI 的 StreamingResponse generator），即可将 Agent 输出转为 HTTP 流，无需修改 Agent Core。

**数据结构**:

```python
import json
from typing import Callable
from .sink import OutputSink
from ..agent.plan import Plan

class SSESink(OutputSink):
    """
    Server-Sent Events 输出接收器。
    将每次方法调用转为 SSE 格式事件写入 write_fn。

    使用方（FastAPI 示例）：
        async def stream_endpoint():
            events = []
            sink = SSESink(write_fn=events.append)
            core = AgentCore(sink=sink)
            core.run(user_input)
            for event in events:
                yield event
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

**SSE 事件清单**:

| 事件类型 | 触发时机 | 关键字段 |
|----------|----------|----------|
| `progress` | 开始执行动作 | `action`, `detail` |
| `plan_updated` | Plan 创建/更新 | `goal`, `steps[]` |
| `text_chunk` | 答案流式片段（done=False） | `content` |
| `text_done` | 答案完整（done=True） | — |
| `observation` | 工具/脚本结果（verbose） | `source`, `content` |
| `error` | 错误 | `message`, `recoverable` |
| `session_end` | 任务/会话结束 | `turn_count`, `status` |

**验收标准**:
- [ ] `SSESink` 每次方法调用产生格式正确的 SSE 行（`data: {...}\n\n`）
- [ ] 可通过 `write_fn` 收集所有输出事件（无 HTTP 依赖，纯单元测试）
- [ ] `on_text_chunk(chunk, done=False)` 产生 `text_chunk` 事件，`done=True` 产生 `text_done` 事件
- [ ] `on_plan_updated()` 事件包含 `goal` 字段

**测试用例** (`tests/unit/test_sse_sink.py`):
```python
def test_sse_sink_format():
    events = []
    sink = SSESink(write_fn=events.append)
    sink.on_progress("load_skill", "data-analysis")
    sink.on_text_chunk("hello", False)
    sink.on_text_chunk("", True)
    sink.on_session_end(3, "completed")

    assert len(events) == 4
    for event in events:
        assert event.startswith("data: ")
        assert event.endswith("\n\n")

    import json
    data = json.loads(events[0][6:])
    assert data == {"type": "progress", "action": "load_skill", "detail": "data-analysis"}
    assert json.loads(events[1][6:])["type"] == "text_chunk"
    assert json.loads(events[2][6:])["type"] == "text_done"
    assert json.loads(events[3][6:])["type"] == "session_end"

def test_sse_sink_plan_updated_has_goal():
    from ..agent.plan import Plan, Step, StepStatus
    events = []
    sink = SSESink(write_fn=events.append)
    plan = Plan(goal="test goal", steps=[
        Step(id="s1", description="step one", status=StepStatus.PENDING),
    ])
    sink.on_plan_updated(plan)
    data = json.loads(events[0][6:])
    assert data["type"] == "plan_updated"
    assert data["goal"] == "test goal"
    assert data["steps"][0]["id"] == "s1"
```

---

### 任务 C3.1: 端到端集成测试

**目标**: 用真实 Anthropic API 跑通完整端到端流程

**输出文件**: `tests/integration/test_e2e.py`

**测试场景**:
1. **基础问答**：无技能，直接回答简单问题
2. **技能触发**：包含相关技能时，Agent 正确加载并使用
3. **多步规划**：复杂任务触发 UPDATE_PLAN，分步执行
4. **死循环安全网**：Agent 不会无限循环（max_turns 保底）

**验收标准**:
- [ ] 标记为 `@pytest.mark.integration`，默认跳过（需 `--integration` flag 运行）
- [ ] 真实 API 调用场景 1-3 全部通过
- [ ] 场景 4：max_turns 内必定退出

---

## Phase C 验收标准（整体）

```bash
# 单元测试（含流式解析器 + SSESink）
uv run pytest tests/unit/ -v

# 流式解析器专项测试
uv run pytest tests/unit/test_streaming.py -v

# SSESink 专项测试
uv run pytest tests/unit/test_sse_sink.py -v

# 集成测试（需设置 ANTHROPIC_API_KEY）
ANTHROPIC_API_KEY=xxx uv run pytest tests/integration/ -v -m integration

# 端到端 CLI 测试（真实模型 + 流式输出）
ANTHROPIC_API_KEY=xxx uv run skills-agent run --skill-root tests/fixtures/skills \
    --model anthropic "请列出可用技能并描述第一个技能的用途"
# 预期：stderr 可见进度行，答案逐字打印到 stdout

# Chat 模式 + 真实模型测试
ANTHROPIC_API_KEY=xxx uv run skills-agent chat --skill-root tests/fixtures/skills \
    --model anthropic
# 输入多轮消息，验证历史上下文正确传递，流式答案逐字显示

# 安全检查（同 Phase A/B）
grep -r "yaml.load(" src/
grep -r "allowed_tools" src/agent/core.py
```