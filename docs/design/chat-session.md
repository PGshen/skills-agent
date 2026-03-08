# Chat 会话管理与多轮对话上下文工程 设计

**状态**：Phase A（会话骨架）→ Phase B（压缩策略）分阶段实现

---

## 1. 问题定位

多轮对话在 Agent 系统中存在两个完全不同的层面：

| 层面 | 说明 | 本文覆盖 |
|------|------|----------|
| **Agent 内部多轮** | 单次任务内的 ReAct 循环（Reason→Act→Observe→...），由 Agent Core 管理，与用户无感知 | 否（见 agent-core.md） |
| **用户侧多轮** | 用户收到 `final_answer` 后继续提问，需要跨任务的会话记忆 | **是（本文）** |

用户侧多轮的核心挑战：

1. **上下文无限膨胀**：朴素方案是把所有历史消息全量传入，N 轮后 context 超出模型 limit
2. **模型焦点失散**：历史轮次过多时，模型注意力分散，对当前问题的响应质量下降
3. **无关信息干扰**：早期轮次与当前任务无关时，仍然占用 context 空间
4. **跨会话恢复**：中断后需能恢复，不要求精确重放（压缩重建即可）

---

## 2. 运行模式

CLI 提供两种模式：

```bash
skills-agent run "query"   # 单次模式：无会话记忆，每次独立执行，适合脚本/CI
skills-agent chat          # 会话模式：维护多轮对话，直到用户 exit 或 Ctrl+C
```

两种模式**共用同一个 AgentCore**，区别仅在外层循环：

```
run 模式：
  AgentCore.run(user_input)  →  final_answer  →  退出

chat 模式：
  while True:
    user_input = input()
    history = session.build_history_messages()
    AgentCore.run(user_input, history_messages=history)  →  final_answer
    session.record(user_input, final_answer)
    session.maybe_compress()
    session.save()
```

---

## 3. 会话文件布局

```
.agent/
└── sessions/
    └── <session-id>/           # UUID，每次 chat 启动时创建
        ├── session.json        # 元信息（只写一次）
        ├── conversation.jsonl  # 完整对话原始记录（追加写入，永久保留）
        └── context.json        # 活跃上下文状态（每轮更新）
```

**session.json** — 创建时写入，不再修改：

```json
{
  "session_id": "uuid",
  "created_at": 1700000000.0,
  "model_id": "claude-sonnet-4-6",
  "skill_roots": ["./skills"],
  "config_snapshot": {}
}
```

**conversation.jsonl** — 每轮对话追加一行，格式：

```jsonl
{"turn": 1, "timestamp": 1700000001.0, "user": "...", "assistant": "..."}
{"turn": 2, "timestamp": 1700000060.0, "user": "...", "assistant": "..."}
```

**context.json** — 每轮更新，包含当前活跃上下文状态：

```json
{
  "session_id": "uuid",
  "compressed_summary": "用户询问了 X，Agent 通过 data-analysis 技能分析后得出结论 Y。",
  "recent_turns": [
    {"role": "user", "content": "..."},
    {"role": "assistant", "content": "..."}
  ],
  "last_used_skills": ["data-analysis"],
  "total_turn_count": 12,
  "recent_window_k": 3
}
```

---

## 4. 数据结构

```python
# src/session/session.py
from pydantic import BaseModel, Field
from typing import Optional
import uuid, time, json
from pathlib import Path

class ConversationTurn(BaseModel):
    role: str           # "user" 或 "assistant"
    content: str
    timestamp: float = Field(default_factory=time.time)

class SessionContext(BaseModel):
    """
    活跃上下文状态。
    持久化到 context.json，每轮结束后更新。
    """
    session_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    compressed_summary: str = ""
    recent_turns: list[ConversationTurn] = Field(default_factory=list)
    last_used_skills: list[str] = Field(default_factory=list)
    total_turn_count: int = 0
    recent_window_k: int = 3

    def build_history_messages(self) -> list[dict]:
        """
        组装历史层消息，供 AgentCore 在系统 prompt 之后、当前用户输入之前注入。

        返回格式：[{"role": str, "content": str}, ...]

        层次：
          1. 历史摘要层（有 compressed_summary 时）
          2. 近期原文层（最近 K 轮）
        """
        msgs: list[dict] = []

        # 层 1：历史摘要（有时）
        if self.compressed_summary:
            msgs.append({
                "role": "user",
                "content": (
                    f"<summary>历史对话摘要：{self.compressed_summary}</summary>"
                ),
            })

        # 层 2：近期原文（最近 K 轮 = K*2 条消息）
        cutoff = self.recent_window_k * 2
        recent = self.recent_turns[-cutoff:] if len(self.recent_turns) > cutoff else self.recent_turns
        msgs.extend({"role": t.role, "content": t.content} for t in recent)

        return msgs

    def record_turn(self, user_input: str, assistant_response: str) -> None:
        """记录本轮对话，追加到 recent_turns，更新计数。"""
        self.recent_turns.append(
            ConversationTurn(role="user", content=user_input)
        )
        self.recent_turns.append(
            ConversationTurn(role="assistant", content=assistant_response)
        )
        self.total_turn_count += 1

    def estimated_recent_tokens(self) -> int:
        """粗略估算 recent_turns 的 token 数（字符数 / 4）。"""
        total_chars = sum(len(t.content) for t in self.recent_turns)
        return total_chars // 4
```

---

## 5. SessionManager

```python
# src/session/session.py（续）
import json
from pathlib import Path

class SessionManager:
    """
    会话文件管理器。
    负责创建、持久化、加载会话文件（session.json / context.json / conversation.jsonl）。
    """

    def __init__(self, sessions_root: str = ".agent/sessions"):
        self._root = Path(sessions_root)

    def create(self, model_id: str = "mock", config: dict = None) -> SessionContext:
        """创建新会话，写入 session.json，返回空 SessionContext。"""
        ctx = SessionContext()
        session_dir = self._root / ctx.session_id
        session_dir.mkdir(parents=True, exist_ok=True)

        # 写 session.json（只写一次）
        meta = {
            "session_id": ctx.session_id,
            "created_at": time.time(),
            "model_id": model_id,
            "config_snapshot": config or {},
        }
        (session_dir / "session.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2)
        )
        return ctx

    def load(self, session_id: str) -> Optional[SessionContext]:
        """加载已有会话的 context.json，不存在返回 None。"""
        context_path = self._root / session_id / "context.json"
        if not context_path.exists():
            return None
        return SessionContext.model_validate_json(context_path.read_text())

    def save(self, ctx: SessionContext) -> None:
        """持久化 context.json（覆盖写入）。"""
        session_dir = self._root / ctx.session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / "context.json").write_text(
            ctx.model_dump_json(indent=2)
        )

    def append_log(self, ctx: SessionContext, user_input: str, response: str) -> None:
        """追加一轮记录到 conversation.jsonl（原始完整记录）。"""
        session_dir = self._root / ctx.session_id
        entry = {
            "turn": ctx.total_turn_count,
            "timestamp": time.time(),
            "user": user_input,
            "assistant": response,
        }
        with open(session_dir / "conversation.jsonl", "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def list_sessions(self) -> list[dict]:
        """列出所有会话的元信息（session.json）。"""
        result = []
        if not self._root.exists():
            return result
        for session_dir in sorted(self._root.iterdir(), reverse=True):
            meta_path = session_dir / "session.json"
            if meta_path.exists():
                result.append(json.loads(meta_path.read_text()))
        return result
```

---

## 6. 上下文组装策略

每次新用户请求时，AgentCore 组装发给模型的 messages，层次如下：

```
messages = [
  # ── 系统层（永不压缩，每次实时重建）──────────────────────────────────
  {"role": "system", "content": SYSTEM_PROMPT + skill_index_text},

  # ── 历史摘要层（有 compressed_summary 时才存在）──────────────────────
  {"role": "user",   "content": "<summary>历史摘要：...</summary>"},

  # ── 近期原文层（最近 K 轮，默认 K=3）────────────────────────────────
  {"role": "user",      "content": "用户第 N-2 轮输入"},
  {"role": "assistant", "content": "助手第 N-2 轮回复"},
  {"role": "user",      "content": "用户第 N-1 轮输入"},
  {"role": "assistant", "content": "助手第 N-1 轮回复"},

  # ── 当前层────────────────────────────────────────────────────────────
  {"role": "user",   "content": "本轮用户输入"},
]
```

**关键约束**：

| 层 | 是否压缩 | 更新时机 |
|----|----------|----------|
| 系统层（prompt + 技能索引） | 永不压缩 | 每轮重建（技能目录可能变化） |
| 历史摘要层 | 追加方式增长（旧轮次压进来） | 触发压缩后更新 |
| 近期原文层 | 超出 K 轮时最旧一轮移出 | 每轮结束后更新 |
| 当前层 | 不存储 | 每次调用时直接传入 |

---

## 7. 压缩触发与执行

### 7.1 触发条件

```python
def should_compress(ctx: SessionContext, threshold_tokens: int) -> bool:
    return ctx.estimated_recent_tokens() > threshold_tokens
```

`threshold_tokens` 默认为模型 context limit 的 **25%**。

选择 25% 的理由：
- 系统层（prompt + 技能索引）通常占 5-10%
- 当前任务的 ReAct 历史（动作 + 观察）可能占 30-50%
- 历史层若超过 25%，与系统层加在一起极易撑满 context

### 7.2 压缩过程（compress-oldest）

```
触发压缩时：
  1. 取 recent_turns 中最旧的 M 轮（默认 M=2，即 4 条消息）
  2. 构造压缩 prompt（独立调用模型，不进入主对话历史）
  3. 将模型返回的摘要追加到 compressed_summary
  4. 从 recent_turns 中移除这 M 轮
  5. 持久化 context.json
```

压缩 prompt 模板：

```
以下是一段用户与 AI 助手的对话记录。
请用不超过 150 字概括：用户的核心问题是什么，AI 执行了哪些操作，最终得出了什么结论。
仅保留事实结论，省略执行过程与中间推理。

对话记录：
User: {turn1_user}
Assistant: {turn1_assistant}
User: {turn2_user}
Assistant: {turn2_assistant}
```

### 7.3 ConversationCompressor 接口

```python
# src/session/compressor.py
from ..model.base import ModelAdapter
from .session import SessionContext
from ..agent.plan import Action, ActionType

class ConversationCompressor:
    """
    对话历史压缩器。
    使用 ModelAdapter 生成摘要（而非规则截断），保证语义质量。
    压缩调用独立于主对话，不写入 conversation.jsonl。
    """

    def __init__(
        self,
        model: ModelAdapter,
        context_limit_tokens: int = 100_000,
        threshold_ratio: float = 0.25,
        compress_oldest_m: int = 2,
    ):
        self._model = model
        self._threshold = int(context_limit_tokens * threshold_ratio)
        self._m = compress_oldest_m

    def maybe_compress(self, ctx: SessionContext) -> bool:
        """
        检查是否需要压缩，需要则执行。
        返回 True 表示发生了压缩；False 表示无需压缩。
        """
        if ctx.estimated_recent_tokens() <= self._threshold:
            return False
        self._compress(ctx)
        return True

    def _compress(self, ctx: SessionContext) -> None:
        """压缩最旧 M 轮，更新 ctx.compressed_summary 和 ctx.recent_turns。"""
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
        ctx.recent_turns = ctx.recent_turns[self._m * 2 :]

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

---

## 8. Chat 命令主循环

```python
# src/cli/chat.py
from ..session.session import SessionContext, SessionManager
from ..session.compressor import ConversationCompressor
from ..agent.core import AgentCore
from ..output.cli_sink import CLISink


def run_chat(args):
    """交互式聊天模式主循环。"""
    session_manager = SessionManager(sessions_root=args.session_dir)

    # 支持 --resume <session-id> 恢复已有会话
    if args.resume:
        ctx = session_manager.load(args.resume)
        if ctx is None:
            print(f"Session '{args.resume}' not found.", file=sys.stderr)
            return
        print(f"Resuming session {ctx.session_id} (turn {ctx.total_turn_count})")
    else:
        ctx = session_manager.create(model_id=args.model)
        print(f"Chat session started. Session: {ctx.session_id}")

    sink = CLISink(verbose=args.verbose)
    core = build_agent_core(args, sink=sink)
    compressor = ConversationCompressor(model=core.model)

    print("Type 'exit' or press Ctrl+C to quit.\n")

    while True:
        try:
            user_input = input("You: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nGoodbye.")
            break

        if user_input.lower() in ("exit", "quit", "q"):
            break
        if not user_input:
            continue

        # 组装含历史的消息层（AgentCore 在此基础上追加系统 prompt 和当前输入）
        history_messages = ctx.build_history_messages()

        # 执行 Agent
        response = core.run(user_input, history_messages=history_messages)

        # 更新会话状态
        ctx.record_turn(user_input, response)
        session_manager.append_log(ctx, user_input, response)

        # 检查并执行压缩（Phase B5）
        compressor.maybe_compress(ctx)

        # 持久化
        session_manager.save(ctx)
```

---

## 9. AgentCore.run() 接口扩展

```python
class AgentCore:
    def run(
        self,
        user_input: str,
        history_messages: list[dict] = None,
    ) -> str:
        """
        执行完整 ReAct 循环，返回最终答案字符串。

        history_messages：由 SessionContext.build_history_messages() 生成。
        注入位置：系统 prompt 之后，当前用户输入之前。

        单次模式（run 命令）：不传 history_messages，等同于 []。
        会话模式（chat 命令）：传入上一轮历史。
        """
```

Context 组装时的完整顺序（在 ContextBuilder 中实现）：

```python
def build(
    self,
    state: AgentState,
    history: list[tuple],          # 本次任务内的 ReAct 历史
    history_messages: list[dict],  # 跨任务的会话历史（来自 SessionContext）
) -> list[dict]:
    msgs = []
    msgs.append({"role": "system", "content": self._system_prompt(state)})
    msgs.extend(history_messages)                # 跨任务会话历史
    msgs.extend(self._task_history(history))     # 本任务内 ReAct 历史
    msgs.append({"role": "user", "content": state.user_input})
    return self._trim_to_limit(msgs)             # 裁剪超出 token 上限的部分
```

---

## 10. 边界情况与错误处理

| 场景 | 处理方式 |
|------|----------|
| sessions 目录不存在 | 自动创建（`mkdir -p`） |
| context.json 损坏（JSON 解析失败） | 记录 WARNING，当作新会话启动 |
| 压缩模型调用失败 | 记录 WARNING，跳过此次压缩（不中止对话） |
| resume 的 session_id 不存在 | 打印错误信息，退出 |
| 压缩后 compressed_summary 超长 | 截断到最大长度（默认 1000 字符），记录 WARNING |
| recent_turns 为空时触发压缩 | 直接返回，不调用模型 |

---

## 11. 验收标准

### Phase A（会话骨架，无压缩）

- [ ] `skills-agent chat` 启动后可持续输入，每轮独立执行
- [ ] 第 2 轮请求时，`history_messages` 包含第 1 轮的 user/assistant 消息
- [ ] `context.json` 和 `conversation.jsonl` 在 `.agent/sessions/<uuid>/` 下正确生成
- [ ] `exit` 或 Ctrl+C 正常退出，不丢失已记录数据
- [ ] `--resume <session-id>` 可加载已有会话状态

### Phase B（压缩策略）

- [ ] 10+ 轮后 `compressed_summary` 非空
- [ ] 压缩后 `recent_turns` 长度不超过 `recent_window_k * 2`
- [ ] 压缩模型调用失败时对话不中断（可 mock 模型抛异常验证）
- [ ] `estimated_recent_tokens()` 在压缩后低于阈值

---

## 12. 设计权衡说明

| 决策点 | 选择 | 替代方案 | 选择理由 |
|--------|------|----------|----------|
| 压缩方式 | 模型生成摘要 | 规则截断（取最旧 N 字符） | 语义保留质量高；摘要 prompt 短，额外成本低 |
| 近期原文保留数 K | 默认 3 | K=5/10 | K=3 覆盖 90% 追问场景；K 过大浪费 context |
| 技能索引更新时机 | 每轮重建 | 缓存，变化时失效 | 技能目录可能随时变化；重建成本低（仅读 YAML 前言） |
| 历史摘要注入位置 | user-role 消息 | assistant-role / system | 避免修改 system prompt 结构；`<summary>` 标签让模型明确区分 |
| conversation.jsonl | 追加，永不删除 | 仅保留 context.json | 保留完整审计与恢复能力；磁盘代价低 |
| 压缩调用是否写日志 | 不写 conversation.jsonl | 写入 | 压缩是内部操作，不属于用户-助手对话 |
