## Phase B: 脚本执行 + 权限 + 上下文裁剪

### 任务 B1.1: Tools Runtime（脚本执行器）

**目标**: 实现受控的脚本执行，支持超时、目录隔离、并发限制

**输出文件**: `src/tools/executor.py`

**背景说明**:

资源限制执行策略：
- `max_script_time_sec`：通过 `subprocess` 的 `timeout` 参数强制执行（**enforced**）
- `max_concurrent_scripts`：通过 `asyncio.Semaphore` 或线程 `Semaphore` 强制执行（**enforced**）
- `max_memory_mb`：记录日志但不强制（**best-effort**，macOS 无 `resource.RLIMIT_AS` 支持）
- `allow_network`：记录日志但不强制（**best-effort**，Python 进程级无法阻断 DNS/TCP）

**接口**:

```python
from dataclasses import dataclass
from ..skills.metadata import ResourceLimits

@dataclass
class ScriptResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False

class ScriptExecutor:
    def execute(
        self,
        script_path: str,
        args: list[str],
        cwd: str,
        limits: ResourceLimits,
        env_overrides: dict = None,
    ) -> ScriptResult:
        """
        在 cwd 目录执行 script_path，受 limits 约束。
        env_overrides 用于注入/覆盖环境变量。
        """
        ...
```

**安全要求**:
- `cwd` 必须是技能目录内的路径（防止脚本逃逸到上级目录）
- 执行前检查 `script_path` 的文件权限（确认可执行）
- 清理危险环境变量（如 `PYTHONPATH`，除非在 `env_overrides` 中明确设置）

**验收标准**:
- [ ] 正常脚本执行并返回 stdout/stderr
- [ ] 超过 `max_script_time_sec` 时 `timed_out=True`，进程被终止
- [ ] 并发超过 `max_concurrent_scripts` 时排队等待
- [ ] `ResourceLimits(max_memory_mb=100)` 仅记录 WARNING，不报错

---

### 任务 B2.1: 权限强制执行

**目标**: 在 Agent Core 中强制执行 `allowed_tools` 白名单

**输入**: A3.3（AgentCore）、B1.1（ScriptExecutor）

**修改文件**: `src/agent/core.py`

**实现逻辑**:

在 `_execute_action` 中，执行 `RUN_SCRIPT` 前验证（通过 `PermissionChecker`）：
1. 从 `state.active_skills` 中找到对应技能的 `SkillMetadata`
2. 调用 `PermissionChecker.check("run_script", meta)` 校验三方权限合并结果
3. 如果不允许，返回 `ToolNotAllowed` observation 并记录 `ACTION_FAILED` 事件

```python
# src/tools/permissions.py
class PermissionChecker:
    def __init__(self, global_allowed_tools: list[str]):
        self._global = set(global_allowed_tools)

    def check(self, tool_name: str, skill_meta: SkillMetadata) -> bool:
        if tool_name not in self._global:
            return False
        if skill_meta.allowed_tools:
            if tool_name not in skill_meta.allowed_tools:
                return False
        return True
```

**注意**：`allowed_tools` 检查发生在 Agent Core 内部，**绝不**通过 model context 传递给模型（防止模型伪造权限）。`ToolsRuntime` 在 Phase B 引入，Phase A 的 RUN_SCRIPT 直接返回 "not enabled" 错误 observation。

**验收标准**:
- [ ] `allowed_tools=[]` 的技能尝试执行脚本时，返回权限错误
- [ ] `allowed_tools=["run_script"]` 的技能可以正常执行

---

### 任务 B3.1: ContextBuilder（上下文组装与裁剪）

**目标**: 实现 ContextBuilder，负责将系统 prompt、会话历史、ReAct 历史、当前输入组装为 messages，并在接近 token 上限时按优先级裁剪

**输出文件**: `src/agent/context.py`

**背景说明**:

ContextBuilder 在 Phase A 的 AgentCore 中以内联方式实现（不独立），Phase B 将其提取为独立类并加入 token 裁剪逻辑。

**消息层次（完整顺序）**:

```
messages = [
  # ── 系统层（每轮重建）──────────────────────────────────────
  {"role": "system", "content": SYSTEM_PROMPT + skill_index + plan_summary},

  # ── 历史摘要层（chat 模式，有 compressed_summary 时）────────
  {"role": "user", "content": "<summary>历史摘要：...</summary>"},

  # ── 近期原文层（chat 模式，最近 K 轮）────────────────────────
  {"role": "user",      "content": "第 N-1 轮用户输入"},
  {"role": "assistant", "content": "第 N-1 轮助手回复"},

  # ── 任务内 ReAct 历史（本次任务的 act/observe 交替）──────────
  {"role": "assistant", "content": json.dumps(prev_action)},
  {"role": "user",      "content": "Observation: <result>"},

  # ── 当前层──────────────────────────────────────────────────
  {"role": "user", "content": user_input},
]
```

**接口**:

```python
# src/agent/context.py
from typing import Optional
from .state import AgentState
from ..skills.registry import SkillRegistry

SYSTEM_PROMPT_TEMPLATE = """
你是一个 AI Agent，通过 ReAct 循环（推理 → 行动 → 观察）完成用户任务。

## 可用技能索引
{skill_index}

## 当前计划
{plan_summary}

## 行动协议
每次回复必须是一个 JSON 对象，格式：
{{
  "type": "<load_skill|load_resource|run_script|update_plan|final_answer>",
  "params": {{ ... }}
}}

## 约束
- 每次只能输出一个 action
- load_resource / run_script 必须在 load_skill 之后
- update_plan 中的 plan 为完整新 Plan（全量替换）
- final_answer 时输出完整最终答案
"""

class ContextBuilder:
    def __init__(self, max_context_tokens: int = 100_000):
        self._max_tokens = max_context_tokens

    def estimate_tokens(self, text: str) -> int:
        """粗略估算：字符数 / 4（不依赖 tiktoken）。"""
        return len(text) // 4

    def build(
        self,
        state: AgentState,
        registry: SkillRegistry,
        react_history: list[tuple],        # 本次任务内 [(action, observation), ...]
        history_messages: list[dict],      # 跨任务会话历史（来自 SessionContext）
    ) -> list[dict]:
        """
        构建完整 messages 列表。
        超出 _max_tokens 时调用 _trim_to_limit 裁剪。
        """
        ...

    def _trim_to_limit(self, msgs: list[dict], max_tokens: int) -> list[dict]:
        """
        裁剪策略（由宽到严）：
        1. 截断 observation 内容（保留前 500 字符）
        2. 丢弃最老的 ReAct 历史轮（保留最近 3 轮）
        3. 截断已加载技能正文（保留摘要行）
        不可丢弃：user_input（当前输入）、Plan 摘要、技能索引
        """
        ...
```

**裁剪优先级（保留优先级从高到低）**:
1. 系统 prompt（角色说明 + 技能索引 + Plan 摘要）— 永不裁剪
2. 当前用户输入 — 永不裁剪
3. 最近 3 轮 ReAct 历史（动作 + 观察）— 优先保留
4. 跨任务会话历史 — 次优先
5. 较老的 ReAct 历史轮、长 observation 内容 — 优先裁减

**验收标准**:
- [ ] 超出 token 限制时，优先截断/丢弃早期 ReAct 历史
- [ ] 系统 prompt 和当前 user_input 始终保留
- [ ] 裁剪后 `estimate_tokens` 之和不超过 `max_context_tokens`
- [ ] `build()` 消息层次顺序正确（system → 历史摘要 → 近期原文 → ReAct 历史 → 当前输入）

---

### 任务 B4.1: AgentState 持久化与崩溃恢复

**目标**: 实现 AgentState 的运行时持久化（state.json）和 compress-rebuild 崩溃恢复

**输出文件**: `src/agent/recovery.py`（恢复逻辑），修改 `src/agent/core.py`（持久化逻辑）

**背景说明**:

运行目录布局（`.agent/runs/<session_id>/`）：

```
.agent/runs/<session-id>/
├── events.jsonl        # 事件流（事实来源，追加写）
├── state.json          # AgentState 快照（每轮覆盖写）
├── final.md            # 最终答案（任务完成时写入）
└── observations/       # 大块工具输出（按引用存储）
```

**持久化时机**：每次 Plan 更新后，立即将 AgentState 写入 `state.json`（覆盖写）。

恢复策略：**compress-rebuild**（不逐条重放消息）

```
恢复流程：
  1. 从 state.json 加载 AgentState（含 Plan + active_skills）
  2. 从 events.jsonl 提取已执行动作序列与 observation 摘要
  3. 构造"恢复摘要消息"注入上下文：
     {"role": "user", "content":
       "<resume>已完成步骤：s1（加载技能），s2（执行脚本，退出码=0）
        当前状态：s3 待执行
        请基于以上摘要继续推进任务。</resume>"}
  4. 继续 ReAct 循环
```

**接口**:

```python
# src/agent/recovery.py
from pathlib import Path
from typing import Optional
from .state import AgentState
from .events import Event, EventType

def load_state(run_dir: Path) -> Optional[AgentState]:
    """
    从 run_dir/state.json 加载 AgentState。
    文件不存在或解析失败时返回 None。
    """
    state_path = run_dir / "state.json"
    if not state_path.exists():
        return None
    try:
        return AgentState.model_validate_json(state_path.read_text())
    except Exception:
        return None

def save_state(run_dir: Path, state: AgentState) -> None:
    """将 AgentState 写入 run_dir/state.json（覆盖写）。"""
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "state.json").write_text(state.model_dump_json(indent=2))

def build_resume_context(run_dir: Path, state: AgentState) -> list[dict]:
    """
    从 events.jsonl 提取已完成动作摘要，构造恢复消息列表。
    返回的列表将被注入 AgentCore.run() 的 history_messages。
    """
    events_path = run_dir / "events.jsonl"
    if not events_path.exists():
        return []
    # 提取动作序列摘要，生成 <resume> 消息
    ...
```

**CLI 集成**：在 `skills-agent run` 命令中增加 `--resume <session-id>` 参数：
```bash
skills-agent run --resume <session-id> "继续之前的任务"
```

**验收标准**:
- [ ] 每次 Plan 更新后 `state.json` 正确写入，包含完整 AgentState
- [ ] `load_state()` 能从 `state.json` 恢复 AgentState（含 Plan 和 active_skills）
- [ ] `build_resume_context()` 从 events.jsonl 生成合理的恢复摘要消息
- [ ] `--resume <session-id>` 从中断点继续执行，不重复已完成步骤
- [ ] `state.json` 不存在时 `load_state()` 返回 None

---

### 任务 B5.1: 会话上下文压缩（ConversationCompressor）

**目标**: 实现基于模型的历史轮次压缩，防止多轮对话中上下文无限膨胀

**输入**: A4.3（SessionContext）、A3.2（ModelAdapter）

**输出文件**: `src/session/compressor.py`

**背景说明**:

详见 [Chat 会话管理设计](../design/chat-session.md)。压缩策略：当 `recent_turns` 的 token 估算值超过阈值时，取最旧的 M 轮，调用模型生成摘要，追加到 `compressed_summary`，再从 `recent_turns` 中移除。

**接口**:

```python
class ConversationCompressor:
    """
    对话历史压缩器。
    使用模型生成摘要（而非规则截断），保留语义质量。
    """

    def __init__(
        self,
        model: ModelAdapter,
        context_limit_tokens: int = 100_000,
        threshold_ratio: float = 0.25,     # recent_turns 占 context limit 的比例上限
        compress_oldest_m: int = 2,        # 每次压缩最旧的 M 轮
    ):
        self._model = model
        self._threshold = int(context_limit_tokens * threshold_ratio)
        self._m = compress_oldest_m

    def maybe_compress(self, ctx: SessionContext) -> bool:
        """
        检查是否需要压缩，需要时执行压缩并更新 ctx。
        返回 True 表示发生了压缩，False 表示无需压缩。
        """
        if ctx.estimated_recent_tokens() <= self._threshold:
            return False
        self._compress(ctx)
        return True

    def _compress(self, ctx: SessionContext) -> None:
        """取最旧 M 轮，生成摘要，更新 compressed_summary。"""
        turns_to_compress = ctx.recent_turns[: self._m * 2]
        if not turns_to_compress:
            return

        conversation_text = "\n".join(
            f"{t.role.capitalize()}: {t.content}" for t in turns_to_compress
        )
        summary = self._summarize(conversation_text)

        ctx.compressed_summary = (
            (ctx.compressed_summary + "\n" + summary).strip()
            if ctx.compressed_summary
            else summary
        )
        ctx.recent_turns = ctx.recent_turns[self._m * 2:]

    def _summarize(self, conversation_text: str) -> str:
        """调用模型生成摘要，返回摘要字符串。"""
        prompt = (
            "以下是一段用户与 AI 助手的对话记录。\n"
            "请用不超过 150 字概括：用户的核心问题是什么，AI 执行了哪些操作，最终得出了什么结论。\n"
            "仅保留事实结论，省略执行过程与中间推理。\n\n"
            f"对话记录：\n{conversation_text}"
        )
        messages = [{"role": "user", "content": prompt}]
        action = self._model.next_action(messages)
        return action.params.get("content", "（摘要生成失败）")
```

**集成点**：在 `chat.py` 的主循环中，每轮结束后调用 `compressor.maybe_compress(ctx)`，再调用 `session_manager.save(ctx)`：

```python
# chat.py 主循环中（Phase B 新增）
from ..session.compressor import ConversationCompressor

compressor = ConversationCompressor(model=core._model)  # 复用同一 ModelAdapter

# ... 在每轮 AgentCore.run() 之后：
ctx.record_turn(user_input, response)
session_manager.append_log(ctx, user_input, response)
compressor.maybe_compress(ctx)   # ← Phase B 新增
session_manager.save(ctx)
```

**验收标准**:
- [ ] `recent_turns` token 估算未超阈值时，`maybe_compress` 返回 False，无模型调用
- [ ] 超阈值时，最旧 M 轮被移除，`compressed_summary` 被更新
- [ ] 压缩后 `ctx.recent_turns` 长度减少 M*2

**测试用例** (`tests/unit/test_compressor.py`):
```python
def test_no_compress_when_below_threshold():
    mock = MockModel(actions=[])
    compressor = ConversationCompressor(
        model=mock,
        threshold_ratio=1.0,  # 设很高，不触发
        context_limit_tokens=100_000,
    )
    ctx = SessionContext()
    ctx.record_turn("hello", "hi")
    result = compressor.maybe_compress(ctx)
    assert result is False
    assert mock.call_count == 0

def test_compress_oldest_turns():
    mock = MockModel(actions=[
        Action(type=ActionType.FINAL_ANSWER, params={"content": "摘要内容"})
    ])
    compressor = ConversationCompressor(
        model=mock,
        threshold_ratio=0.0,  # 强制触发
        context_limit_tokens=100_000,
        compress_oldest_m=1,
    )
    ctx = SessionContext()
    ctx.record_turn("问题1", "回答1")
    ctx.record_turn("问题2", "回答2")

    result = compressor.maybe_compress(ctx)
    assert result is True
    assert ctx.compressed_summary == "摘要内容"
    assert len(ctx.recent_turns) == 2  # 只剩第2轮（2条消息）
```

---

## Phase B 验收标准（整体）

```bash
# 单元测试（包含 B 阶段新增）
uv run pytest tests/unit/ -v

# 脚本执行测试（手动）
# 创建含 run_script 权限的技能，运行后能看到脚本 stdout

# 权限测试（手动）
# 技能不含 run_script 权限时执行脚本应返回 ToolNotAllowed observation

# 死循环检测测试
# MockModel 返回重复动作 2 次后触发 dead_loop_triggered=True，循环中止
# events.jsonl 中出现 DEAD_LOOP_DETECTED 事件

# Plan 进展监测测试
# MockModel 连续 4 轮返回不更新 Plan 的动作，events.jsonl 出现 plan_stall DEAD_LOOP_DETECTED

# 崩溃恢复测试
# 运行中途 kill 进程，.agent/runs/<session-id>/state.json 存在且包含已完成步骤
# --resume <session-id> 能从中断点继续

# Context 裁剪测试
# max_context_tokens 设为极小值时，ContextBuilder 正确裁剪历史，不丢弃 user_input

# 会话压缩测试（手动）
# skills-agent chat，输入 10+ 轮消息，检查 .agent/sessions/*/context.json
# 确认 compressed_summary 非空，recent_turns 长度不超过 K*2

# 安全检查
grep -r "yaml.load(" src/         # 应无输出
grep -r "allowed_tools" src/agent/core.py  # 应无输出（控制字段不进入 Agent Core context 模板）
```