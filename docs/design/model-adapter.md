# Model Adapter 设计

**状态**：Phase A（MockModel）→ Phase C（AnthropicAdapter + StreamingJSONParser）分阶段实现

---

## 1. 设计目标

Model Adapter 是模型 I/O 可靠性层，将上层固定格式的消息（messages）转换为结构化 Action 输出。

| 要求 | 说明 |
|------|------|
| 供应商无关 | 可切换 MockModel / Anthropic / OpenAI-compatible |
| 强制结构化输出 | 每轮 `next_action()` 必须返回一个合法 Action |
| 鲁棒解析 | 容忍模型输出噪声（code fence、解释文本），提供重试 |
| 流式支持 | Phase C 支持 token 级流式输出（StreamingJSONParser + OutputSink）|
| 评估支持 | MockModel 支持用例驱动的动作序列，支持流式模拟 |

**不在此处理**：
- 技能发现与加载（Skill Registry / Skill Loader 负责）
- 工具执行与权限（Tools Runtime 负责）
- Plan 更新语义（Agent Core 决定）

---

## 2. 统一接口

```python
# src/model/base.py
from abc import ABC, abstractmethod
from ..agent.plan import Action

class ModelAdapter(ABC):
    """
    模型适配器抽象基类。
    Agent Core 仅依赖此接口，不依赖具体实现。
    """

    @abstractmethod
    def next_action(self, messages: list[dict]) -> Action:
        """
        向模型发送 messages，解析并返回单个 Action。

        messages 格式：OpenAI 风格 [{"role": str, "content": str}, ...]
        返回：Action（type + params）

        失败时抛出 ModelResponseError（Agent Core 捕获并记录为 observation）。
        """

    def close(self) -> None:
        """释放资源（连接池等），默认空实现。"""
```

---

## 3. MockModel（Phase A/B）

### 3.1 设计目标

- 用于 Phase A/B 端到端测试，无需真实 API
- 按用例预设动作序列，按轮次依次返回
- 支持模拟流式输出（Phase A 测试流式 JSON 解析链路）

### 3.2 完整实现

```python
# src/model/mock.py
from .base import ModelAdapter
from ..agent.plan import Action, ActionType

class MockModel(ModelAdapter):
    """
    按顺序返回预设 Action 序列。
    用于单元测试与 Phase A/B 端到端验证。
    当序列耗尽时返回 FINAL_ANSWER（避免死循环）。
    """

    def __init__(self, actions: list[Action | dict]):
        """
        actions: 预设动作列表。
        每项可以是 Action 对象或 dict（自动转换）。
        示例：
            MockModel([
                Action(type=ActionType.UPDATE_PLAN, params={"plan": {...}}),
                Action(type=ActionType.LOAD_SKILL, params={"skill_name": "data-analysis"}),
                Action(type=ActionType.FINAL_ANSWER, params={"content": "done"}),
            ])
        """
        self._actions: list[Action] = []
        for a in actions:
            if isinstance(a, dict):
                self._actions.append(Action.model_validate(a))
            else:
                self._actions.append(a)
        self._index = 0
        self._call_history: list[list[dict]] = []

    def next_action(self, messages: list[dict]) -> Action:
        self._call_history.append(messages)

        if self._index >= len(self._actions):
            # 序列耗尽，返回 final_answer 防止死循环
            return Action(
                type=ActionType.FINAL_ANSWER,
                params={"content": "[MockModel: action sequence exhausted]"},
            )

        action = self._actions[self._index]
        self._index += 1
        return action

    @property
    def call_count(self) -> int:
        return len(self._call_history)

    def reset(self) -> None:
        self._index = 0
        self._call_history.clear()
```

### 3.3 流式模拟（Phase C 测试用）

```python
class StreamingMockModel(MockModel):
    """
    继承 MockModel，在 next_action 中模拟流式 chunk 输出。
    用于验证 StreamingJSONParser + CLISink 的流式链路。
    """

    def __init__(self, actions: list[Action | dict], chunk_size: int = 10):
        super().__init__(actions)
        self._chunk_size = chunk_size

    def next_action_streaming(
        self,
        messages: list[dict],
        on_chunk,  # Callable[[str], None]
    ) -> Action:
        """
        模拟 token 级流式输出：将 JSON 字符串按 chunk_size 分块调用 on_chunk。
        """
        import json
        action = super().next_action(messages)
        json_str = json.dumps({"type": action.type, "params": action.params})

        for i in range(0, len(json_str), self._chunk_size):
            on_chunk(json_str[i:i + self._chunk_size])

        return action
```

---

## 4. 结构化动作协议

### 4.1 模型输出格式

每次 `next_action()` 期望模型输出单个 JSON 对象（不含 Markdown code fence 或解释文本）：

```json
{
  "type": "load_skill",
  "params": {
    "skill_name": "pdf-form-filler"
  }
}
```

### 4.2 Action.type 枚举与 params 结构

| type | 必需 params | 可选 params |
|------|-------------|-------------|
| `load_skill` | `skill_name: str` | — |
| `load_resource` | `skill_name: str`, `resource: str` | `section_hint: str` |
| `run_script` | `skill_name: str`, `script: str` | `args: list[str]` |
| `update_plan` | `plan: dict`（完整 Plan JSON） | — |
| `final_answer` | `content: str` | — |

`update_plan` 中的 plan 格式：

```json
{
  "goal": "完成用户任务",
  "steps": [
    {"id": "s1", "description": "加载数据分析技能", "status": "pending"},
    {"id": "s2", "description": "执行分析脚本",     "status": "pending"},
    {"id": "s3", "description": "输出报告",          "status": "pending"}
  ]
}
```

### 4.3 System Prompt 中的输出协议约束

```
## 输出格式（严格遵守）
每次回复必须且只能是以下格式的 JSON 对象：
{
  "type": "<动作类型>",
  "params": { ... }
}

禁止：
- Markdown code fence（```json）
- 解释文字、前言、结尾说明
- 多个 JSON 对象
- 嵌套 action 数组
```

---

## 5. 解析、校验与重试

### 5.1 解析策略（Phase A，非流式）

```python
# src/model/base.py（工具函数）
import json, re

def parse_action_response(raw: str) -> Action:
    """
    从模型原始文本解析 Action。
    三步降级解析：
    1. 直接 json.loads()
    2. 提取最外层 {} 块再解析（去除 code fence 等噪声）
    3. 仍失败则抛出 ModelResponseError
    """
    raw = raw.strip()

    # 步骤 1：直接解析
    try:
        data = json.loads(raw)
        return _validate_action(data)
    except (json.JSONDecodeError, ValueError):
        pass

    # 步骤 2：提取 JSON 块
    match = re.search(r'\{.*\}', raw, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group())
            return _validate_action(data)
        except (json.JSONDecodeError, ValueError):
            pass

    raise ModelResponseError(f"Cannot parse model response as Action: {raw[:200]!r}")

def _validate_action(data: dict) -> Action:
    """最小 schema 校验：type 必须是合法枚举，params 必须是 dict。"""
    if "type" not in data:
        raise ValueError("Missing 'type' field")
    return Action(type=data["type"], params=data.get("params", {}))
```

### 5.2 重试策略

```python
class RetryAdapter(ModelAdapter):
    """
    包装任意 ModelAdapter，在解析失败时自动重试。
    重试时附加纠错消息（要求模型仅输出修正 JSON）。
    """

    def __init__(self, inner: ModelAdapter, max_retries: int = 2):
        self._inner = inner
        self._max_retries = max_retries

    def next_action(self, messages: list[dict]) -> Action:
        last_error = None
        for attempt in range(self._max_retries + 1):
            try:
                return self._inner.next_action(messages)
            except ModelResponseError as e:
                last_error = e
                if attempt < self._max_retries:
                    # 追加纠错消息
                    messages = messages + [{
                        "role": "user",
                        "content": (
                            f"你的上一次输出无法解析：{e}\n"
                            "请仅输出一个合法的 JSON 对象，格式：\n"
                            '{"type": "<动作类型>", "params": {...}}'
                        )
                    }]
        raise last_error

class ModelResponseError(Exception):
    pass
```

---

## 6. AnthropicAdapter（Phase C）

### 6.1 接口草图

```python
# src/model/anthropic.py
import anthropic
from .base import ModelAdapter
from ..agent.plan import Action
from ..output.sink import OutputSink, NullSink
from .streaming import StreamingJSONParser

class AnthropicAdapter(ModelAdapter):
    """
    Anthropic Claude API 适配器（Phase C）。
    支持非流式与流式两种调用模式。
    """

    def __init__(
        self,
        api_key: str,
        model: str = "claude-sonnet-4-6",
        max_tokens: int = 4096,
        sink: OutputSink = None,
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

        # Phase A 行为：FINAL_ANSWER 时一次性调用 on_text_chunk
        if action.type == "final_answer":
            self._sink.on_text_chunk(action.params.get("content", ""), done=True)

        return action

    def next_action_streaming(self, messages: list[dict]) -> Action:
        """
        流式调用（Phase C 完整版）。
        通过 StreamingJSONParser 实时解析 token，
        在解析到 final_answer.content 时逐字调用 sink.on_text_chunk()。
        """
        # 详见 streaming.md 设计
        ...
```

---

## 7. StreamingJSONParser（Phase C）

### 7.1 设计要点

StreamingJSONParser 是自研的 FSM（有限状态机）流式 JSON 解析器，不依赖第三方库。

```
输入：JSON 字符流（逐字符或逐 chunk）
输出：路径匹配回调（JSONPath 风格）

支持路径：
  $.type                        → 解析到 action.type 时回调
  $.params.content              → 解析到 final_answer.content 时逐字回调（delta 模式）
  $.params.skill_name           → 解析到 skill_name 时回调
```

### 7.2 接口草图

```python
# src/model/streaming.py
from typing import Callable

class StreamingJSONParser:
    """
    FSM 流式 JSON 解析器。
    path_callbacks: 路径 → 回调函数映射。
    回调签名：(path: str, value: str, done: bool) -> None
    """

    def __init__(
        self,
        path_callbacks: dict[str, Callable[[str, str, bool], None]],
    ):
        self._callbacks = path_callbacks
        # FSM 内部状态...

    def feed(self, chunk: str) -> None:
        """接收一个字符串 chunk，更新状态机，触发命中的回调。"""

    def get_result(self) -> dict:
        """流结束后返回完整的 JSON 对象（用于最终校验）。"""

    def reset(self) -> None:
        """重置状态机（新一轮调用前使用）。"""
```

### 7.3 与 CLISink 的集成（Phase C）

```python
def on_answer_chunk(path: str, value: str, done: bool) -> None:
    self._sink.on_text_chunk(value, done)

parser = StreamingJSONParser(callbacks={
    "$.params.content": on_answer_chunk,
})

with self._client.messages.stream(...) as stream:
    for chunk in stream.text_stream:
        parser.feed(chunk)

result = parser.get_result()
return _validate_action(result)
```

---

## 8. 各阶段实现范围

| 功能 | Phase A | Phase B | Phase C |
|------|---------|---------|---------|
| ModelAdapter 抽象基类 | ✅ | — | — |
| MockModel（顺序动作） | ✅ | — | — |
| parse_action_response（JSON 解析 + 提取） | ✅ | — | — |
| RetryAdapter（重试包装） | ✅ | — | — |
| AnthropicAdapter（非流式） | — | — | ✅ |
| StreamingJSONParser | — | — | ✅ |
| AnthropicAdapter（流式） | — | — | ✅ |
| StreamingMockModel（流式测试） | — | — | ✅ |

---

## 9. 验收标准

### Phase A

- [ ] `MockModel` 按序返回 3 个预设 Action，第 4 次返回 FINAL_ANSWER
- [ ] `parse_action_response()` 正确解析干净 JSON
- [ ] `parse_action_response()` 正确从带 code fence 的输出中提取 JSON
- [ ] `RetryAdapter` 在第一次失败后追加纠错消息并重试
- [ ] `MockModel.call_count` 在 AgentCore 完成 3 轮后等于 3

### Phase C

- [ ] `AnthropicAdapter.next_action()` 成功调用 API 并返回合法 Action
- [ ] `StreamingJSONParser` 对 `$.params.content` 路径触发多次 delta 回调
- [ ] delta 回调拼接结果与最终 `get_result()["params"]["content"]` 一致
- [ ] 流式中断时（网络错误）抛出 `ModelResponseError`，不挂起

---

## 10. 设计权衡说明

| 决策点 | 选择 | 替代方案 | 选择理由 |
|--------|------|----------|----------|
| 结构化输出机制 | prompt-only（要求 JSON 输出） | tool use / JSON mode | MockModel 阶段不依赖 provider 能力；Phase C 可切换为 tool use |
| 流式解析器 | 自研 FSM | 第三方库（ijson 等） | 避免依赖；场景简单（固定 schema）；可测试性强 |
| MockModel 序列耗尽处理 | 返回 FINAL_ANSWER | 抛出异常 | 避免测试因 MockModel 逻辑导致死循环 |
| 重试时追加 user 消息 | 追加到 messages 末尾 | 新开对话 | 给模型更多上下文理解失败原因 |
