# CLI 设计

**状态**：Phase A（基础 CLI：run / chat / skills list/inspect）→ 后续阶段（技能包分发：zip 安装/卸载/校验）分阶段实现

> **后续阶段（暂不实现）**：zip 包安装/卸载/签名校验等分发能力。当前阶段技能目录直接通过文件系统管理（手动复制 / git clone）。本文档 Phase A 部分为当前实现目标，后续部分保留设计草案供参考。

---

## 1. Phase A：基础 CLI

### 1.1 命令概览

```
skills-agent <command> [options]

命令：
  run <query>      单次模式：执行一次任务后退出
  chat             会话模式：持续多轮对话
  skills list      列出所有可用技能（技能索引）
  skills inspect   查看指定技能的元数据
```

### 1.2 入口与路由

```python
# src/cli/main.py
import argparse
import sys

def main():
    parser = argparse.ArgumentParser(
        prog="skills-agent",
        description="AI Agent with Skills system",
    )
    parser.add_argument("--config", default=".agent/config.json",
                        help="Path to config file")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Show observations and internal details")
    parser.add_argument("--no-color", action="store_true",
                        help="Disable colored output")

    subparsers = parser.add_subparsers(dest="command", required=True)

    # run 子命令
    run_parser = subparsers.add_parser("run", help="Execute a single query")
    run_parser.add_argument("query", help="Query or task description")
    run_parser.add_argument("--model", default=None,
                            help="Override model provider")

    # chat 子命令
    chat_parser = subparsers.add_parser("chat", help="Start interactive chat session")
    chat_parser.add_argument("--resume", default=None, metavar="SESSION_ID",
                             help="Resume an existing session")
    chat_parser.add_argument("--session-dir", default=".agent/sessions",
                             help="Directory for session files")

    # skills 子命令组
    skills_parser = subparsers.add_parser("skills", help="Manage skills")
    skills_sub = skills_parser.add_subparsers(dest="skills_command", required=True)
    skills_sub.add_parser("list", help="List all available skills")
    inspect_parser = skills_sub.add_parser("inspect", help="Inspect a specific skill")
    inspect_parser.add_argument("name", help="Skill name")
    inspect_parser.add_argument("--show-body", action="store_true",
                                help="Also show skill body (SKILL.md content)")

    args = parser.parse_args()

    from ..common.config import load_config
    config = load_config(args.config)

    if args.command == "run":
        from .run import run_command
        run_command(args, config)
    elif args.command == "chat":
        from .chat import chat_command
        chat_command(args, config)
    elif args.command == "skills":
        from .skills_cmd import skills_command
        skills_command(args, config)
```

---

### 1.3 `run` 命令

```python
# src/cli/run.py
import sys
from ..agent.core import AgentCore
from ..output.cli_sink import CLISink
from .builder import build_agent_core

def run_command(args, config: dict) -> None:
    """
    单次执行模式。
    进度 → stderr；最终答案 → stdout。
    退出码：0=成功，1=失败/死循环。
    """
    sink = CLISink(verbose=args.verbose, color=not args.no_color)
    core = build_agent_core(config, sink=sink)

    try:
        answer = core.run(args.query)
        sys.exit(0)
    except Exception as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        sys.exit(1)
```

### 1.4 `chat` 命令

详见 [chat-session.md](chat-session.md)。

```python
# src/cli/chat.py
import sys
from ..session.session import SessionManager
from ..session.compressor import ConversationCompressor
from ..output.cli_sink import CLISink
from .builder import build_agent_core

def chat_command(args, config: dict) -> None:
    """
    交互式会话模式。主循环在 chat-session.md 第 8 节定义。
    """
    session_manager = SessionManager(sessions_root=args.session_dir)
    sink = CLISink(verbose=args.verbose, color=not args.no_color)
    core = build_agent_core(config, sink=sink)

    # 加载或创建会话
    if args.resume:
        ctx = session_manager.load(args.resume)
        if ctx is None:
            print(f"Session '{args.resume}' not found.", file=sys.stderr)
            sys.exit(1)
        print(f"Resuming session {ctx.session_id} (turn {ctx.total_turn_count})",
              file=sys.stderr)
    else:
        ctx = session_manager.create(model_id=config.get("model", {}).get("provider", "mock"))
        print(f"Session: {ctx.session_id}", file=sys.stderr)

    compressor = ConversationCompressor(model=core._model)
    print("Type 'exit' or Ctrl+C to quit.\n", file=sys.stderr)

    while True:
        try:
            user_input = input("You: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nGoodbye.", file=sys.stderr)
            break

        if not user_input or user_input.lower() in ("exit", "quit", "q"):
            break

        history = ctx.build_history_messages()
        answer = core.run(user_input, history_messages=history)

        ctx.record_turn(user_input, answer)
        session_manager.append_log(ctx, user_input, answer)
        compressor.maybe_compress(ctx)
        session_manager.save(ctx)
```

### 1.5 `skills list` 与 `skills inspect`

```python
# src/cli/skills_cmd.py
import json
from ..skills.registry import SkillRegistry
from ..skills.loader import SkillLoader

def skills_command(args, config: dict) -> None:
    registry = SkillRegistry(config["skill_roots"])
    registry.scan()

    if args.skills_command == "list":
        skills = registry.all()
        if not skills:
            print("No skills found.")
            return
        print(f"Found {len(skills)} skill(s):\n")
        for meta in sorted(skills, key=lambda m: (m.source, m.name)):
            print(f"  [{meta.source}] {meta.name}")
            print(f"    {meta.description}")

    elif args.skills_command == "inspect":
        meta = registry.find(args.name)
        if not meta:
            print(f"Skill '{args.name}' not found.")
            return

        print(f"Name:        {meta.name}")
        print(f"Source:      {meta.source}")
        print(f"Version:     {meta.version}")
        print(f"Description: {meta.description}")
        print(f"Path:        {meta.skill_path}")
        print(f"Allowed tools: {meta.allowed_tools or '(none declared)'}")
        print(f"Resource limits:")
        print(f"  max_script_time_sec:  {meta.resource_limits.max_script_time_sec}")
        print(f"  max_concurrent_scripts: {meta.resource_limits.max_concurrent_scripts}")
        print(f"  allow_network:        {meta.resource_limits.allow_network}")

        if args.show_body:
            loader = SkillLoader()
            body, report = loader.load_body(meta)
            print(f"\n--- SKILL.md Body (sha256={report['sha256'][:8]}...) ---")
            print(body)
```

### 1.6 AgentCore 构造辅助函数

```python
# src/cli/builder.py
from ..agent.core import AgentCore
from ..agent.events import EventLogger
from ..skills.registry import SkillRegistry
from ..skills.loader import SkillLoader
from ..model.mock import MockModel
from ..output.sink import OutputSink
import os, uuid

def build_agent_core(config: dict, sink: OutputSink = None) -> AgentCore:
    """
    根据配置构造 AgentCore。
    当前仅支持 MockModel；Phase C 扩展为 AnthropicAdapter。
    """
    session_id = str(uuid.uuid4())
    run_dir = f".agent/runs/{session_id}"
    os.makedirs(run_dir, exist_ok=True)

    event_logger = EventLogger(
        path=f"{run_dir}/events.jsonl",
        session_id=session_id,
    )

    registry = SkillRegistry(config.get("skill_roots", []))
    loader = SkillLoader()

    model_config = config.get("model", {})
    provider = model_config.get("provider", "mock")

    if provider == "mock":
        # Phase A：MockModel 需要在测试中注入动作序列
        # 实际 run 命令中：MockModel 使用空序列（立即返回 FINAL_ANSWER）
        from ..model.mock import MockModel
        from ..agent.plan import Action, ActionType
        model = MockModel(actions=[
            Action(type=ActionType.FINAL_ANSWER,
                   params={"content": "[MockModel] No real model configured."})
        ])
    elif provider == "anthropic":
        from ..model.anthropic import AnthropicAdapter
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        model = AnthropicAdapter(api_key=api_key, sink=sink)
    else:
        raise ValueError(f"Unknown model provider: {provider!r}")

    budget = config.get("budget", {})

    return AgentCore(
        model=model,
        registry=registry,
        loader=loader,
        event_logger=event_logger,
        sink=sink,
        max_turns=budget.get("max_turns", 20),
    )
```

---

## 2. CLI 使用示例

```bash
# 单次执行
skills-agent run "分析 data.csv 并生成报告"

# 单次执行，显示详细进度
skills-agent run "填写表单" --verbose

# 单次执行，重定向答案（进度仍显示在终端）
skills-agent run "总结文档" > summary.md

# 交互式会话
skills-agent chat

# 恢复已有会话
skills-agent chat --resume <session-id>

# 列出技能
skills-agent skills list

# 查看技能详情
skills-agent skills inspect pdf-form-filler

# 查看技能详情（含正文）
skills-agent skills inspect pdf-form-filler --show-body
```

---

## 3. 验收标准（Phase A）

- [ ] `skills-agent run "test"` 正常运行，stdout 输出答案，stderr 输出进度
- [ ] `skills-agent run "test" 2>/dev/null` 只有答案出现在 stdout
- [ ] `skills-agent skills list` 正确列出测试 fixtures 中的技能
- [ ] `skills-agent skills inspect example-skill` 输出正确元数据
- [ ] `skills-agent chat` 启动后可连续输入，`exit` 正常退出
- [ ] `--resume <invalid-id>` 输出错误提示并以非 0 退出码退出

---

## 4. 后续阶段：技能包分发（暂不实现）

> 以下内容为后续增强的设计草案，Phase A 不实现。

### 4.1 计划中的命令

```bash
skills-agent skills install <zip>     # 从 zip 包安装技能
skills-agent skills uninstall <name>  # 卸载技能（移入 .trash）
skills-agent skills verify <zip>      # 校验 zip 包完整性
skills-agent skills export <name>     # 打包为 zip
skills-agent skills refresh           # 强制刷新索引
```

### 4.2 技能包格式（zip）

```
skills-pack.zip
  <skill-dir>/
    SKILL.md
    scripts/
    reference/
  manifest.json   （可选，包含文件哈希列表）
```

### 4.3 安装原则

- 解压到临时目录 → 完整性校验 → 原子移动到 skill root
- 冲突处理：默认拒绝，支持 `--force` 覆盖
- 安装后触发 SkillRegistry 刷新
- 卸载：移入 `.trash/`（不直接删除，支持恢复）
