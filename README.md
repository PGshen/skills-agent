# Agent Skills System

一个 Python 原生的 AI Agent 技能框架，让 ReAct 循环 Agent 能够发现并调用"技能"（Skills），而不与任何特定 LLM 耦合。

## 核心概念

**Skill（技能）** — 一个目录，包含 `SKILL.md`（YAML frontmatter + Markdown 正文）以及可选的脚本和资源文件。技能是能力的基本单位。

**ReAct Loop** — Agent 每轮从模型获取一个 `Action`（`LOAD_SKILL`、`LOAD_RESOURCE`、`RUN_SCRIPT`、`UPDATE_PLAN`、`FINAL_ANSWER`），执行后循环，直到 `FINAL_ANSWER` 或终止条件。

**ModelAdapter** — AgentCore 与模型之间的唯一接口，`next_action(messages) -> Action`。测试阶段用 `MockModel`，生产环境换 `OpenAIAdapter`，AgentCore 无需改动。

## 安装

```bash
# 安装依赖
uv sync

# 安装开发依赖
uv sync --dev
```

**依赖项**: Python >= 3.11, PyYAML >= 6.0, pydantic >= 2.0, openai >= 2.26.0

## 使用

```bash
# 列出所有可用技能
skills-agent skills list

# 执行单次任务
skills-agent run "your task description"

# 指定技能目录
skills-agent run --skills-dir ./my-skills "your task description"

# 进入多轮对话模式
skills-agent chat
```

## Skill 目录结构

```
my-skill/
  SKILL.md          # 技能定义（必须）
  scripts/          # 可执行脚本（可选）
  resources/        # 参考资料（可选）
```

**SKILL.md 格式：**

```markdown
---
name: my-skill
description: "这个技能的用途描述"
allowed-tools: [read_file, run_script]
resource-limits:
  max-script-time-sec: 30
---
# 技能正文
注入到模型上下文中的内容...
```

> 注意：`allowed-tools` 等控制字段不会出现在模型上下文中，通过 `SkillRegistry.list_model_views()` 进行隔离。

## 模块结构

```
src/
  skills/        # 技能解析、注册、加载
  agent/         # ReAct 主循环、Plan、状态、事件
  model/         # ModelAdapter 接口、MockModel、OpenAIAdapter
  output/        # OutputSink、CLISink（终端）、SSESink（HTTP）
  tools/         # 脚本执行、权限控制、用户审批
  session/       # 多轮对话状态与历史压缩
  common/        # 配置、日志、安全工具
  cli/           # CLI 入口（run / chat 子命令）
```

## 开发

```bash
# 运行单元测试
uv run pytest tests/unit/ -v

# 运行单个测试文件
uv run pytest tests/unit/test_frontmatter.py -v

# 运行集成测试（需要真实 API）
uv run pytest tests/integration/ -v

# 代码检查
uv run ruff check src/

# 类型检查
uv run mypy src/
```

单元测试不依赖真实 API，通过 `MockModel` 驱动。集成测试标记为 `@pytest.mark.integration`，CI 默认跳过。

## 交付进度

| 阶段 | 描述 | 状态 |
|------|------|------|
| Phase A | 核心链路（MockModel 驱动的完整 ReAct 循环） | 完成 |
| Phase B | 脚本执行、权限控制、上下文裁剪、崩溃恢复 | 完成 |
| Phase C | 真实 LLM 适配器（OpenAI）+ 流式 JSON 解析 | 进行中 |

详细任务进度见 [docs/dev/dev_plan.md](docs/dev/dev_plan.md)。

## 配置

配置优先级：**内置默认值 < 配置文件 < 环境变量 < CLI 参数**

### 配置文件

在项目根目录创建 `.skills-agent.json`（或用户级 `~/.skills-agent/config.json`），只需写需要覆盖的字段：

```json
{
  "model": { "openai_model": "gpt-4o-mini" },
  "agent": { "max_turns": 30 },
  "paths": { "skill_root": "./my-skills" }
}
```

### 常用环境变量

| 变量 | 说明 |
|------|------|
| `OPENAI_API_KEY` | OpenAI API 密钥 |
| `SKILLS_AGENT_MODEL_BACKEND` | `openai` 或 `mock` |
| `SKILLS_AGENT_OPENAI_MODEL` | OpenAI 模型 ID |
| `SKILLS_AGENT_MAX_TURNS` | 最大 ReAct 轮数 |
| `SKILLS_AGENT_MAX_CONTEXT_TOKENS` | 上下文 token 上限 |
| `SKILLS_AGENT_SKILL_ROOT` | 技能目录路径 |
| `SKILLS_AGENT_NO_COLOR` | 设为 `1` 禁用颜色输出 |

完整配置项列表见 [docs/configuration.md](docs/configuration.md)。

## 关键设计约束

- `yaml.load()` 禁止使用，只用 `yaml.safe_load()`
- `allowed_tools` 等控制字段不得进入模型上下文
- `load_resource()` 必须阻止路径遍历攻击（拒绝 `..` 和绝对路径）
- `OutputSink` 的所有方法不得抛出异常
- `ModelAdapter.next_action()` 接口冻结，换模型不改 AgentCore
