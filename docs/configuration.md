# 配置说明

skills-agent 的配置分三层，优先级从低到高：

```
内置默认值  <  配置文件  <  环境变量  <  CLI 参数
```

---

## 配置文件

### 搜索顺序

启动时依次查找，找到第一个存在的文件即使用：

| 优先级 | 路径 | 说明 |
|--------|------|------|
| 1 | `$SKILLS_AGENT_CONFIG` | 环境变量指定的绝对路径 |
| 2 | `./.skills-agent.json` | 项目级（当前工作目录） |
| 3 | `~/.skills-agent/config.json` | 用户级（全局默认） |

如果三处都没有配置文件，则完全使用内置默认值。

### 格式

JSON 文件，所有字段均为可选（只需写入需要覆盖的部分）：

```json
{
  "model": {
    "backend": "openai",
    "openai_model": "gpt-4o",
    "max_tokens": 4096
  },
  "agent": {
    "max_turns": 20,
    "dead_loop_window": 6,
    "dead_loop_stall_turns": 4,
    "max_context_tokens": 100000
  },
  "paths": {
    "skill_root": "./skills",
    "run_base": ".agent/runs",
    "session_dir": ".agent/sessions",
    "log_dir": null
  },
  "compressor": {
    "threshold_ratio": 0.25,
    "compress_oldest_m": 2
  },
  "cli": {
    "verbose": false,
    "color": true
  }
}
```

---

## 配置项参考

### `model` — 模型后端

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `backend` | string | `"openai"` | 模型后端：`"openai"` 或 `"mock"`（测试用） |
| `openai_model` | string | `"gpt-4o"` | OpenAI 模型 ID，例如 `"gpt-4o-mini"` |
| `max_tokens` | int | `4096` | 单次响应最大 token 数 |

### `agent` — ReAct 循环行为

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `max_turns` | int | `20` | 最大 ReAct 循环轮数，超出后强制终止 |
| `dead_loop_window` | int | `6` | 死循环检测滑动窗口大小（action hash 队列长度） |
| `dead_loop_stall_turns` | int | `4` | Plan 无进展告警阈值（轮数） |
| `max_context_tokens` | int | `100000` | 上下文 token 上限，超出后触发裁剪 |

### `paths` — 文件路径

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `skill_root` | string | `"./skills"` | 技能目录根路径（相对于工作目录） |
| `run_base` | string | `".agent/runs"` | 单次运行状态存储目录（崩溃恢复） |
| `session_dir` | string | `".agent/sessions"` | 多轮对话 session 文件目录 |
| `log_dir` | string \| null | `null` | 事件日志目录，`null` 表示使用系统临时目录 |

### `compressor` — 对话历史压缩

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `threshold_ratio` | float | `0.25` | 当 recent_tokens > max_context_tokens × ratio 时触发压缩 |
| `compress_oldest_m` | int | `2` | 每次压缩最旧 M 轮对话 |

### `cli` — 终端输出

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `verbose` | bool | `false` | 在 stderr 显示 observation 详情 |
| `color` | bool | `true` | 启用 ANSI 颜色输出 |

### `tools` — 工具执行

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `allowed_tools` | string[] | 全部工具 | Agent 可使用的工具白名单 |
| `interactive` | bool | `true` | 高风险工具（`write_file`/`delete_file`/`run_script`）执行前是否弹出审批提示 |
| `tavily_api_key` | string \| null | `null` | Tavily Search API Key，配置后启用 `web_search` 工具 |

**可用工具列表：**

| 工具 | 风险 | 说明 |
|------|------|------|
| `read_file` | low | 读取文件内容 |
| `list_dir` | low | 列举目录条目 |
| `grep` | low | 正则搜索文件内容 |
| `run_script` | medium | 执行技能目录内的脚本，需审批 |
| `write_file` | high | 写入文件，需审批 |
| `delete_file` | high | 删除文件，需审批 |
| `web_search` | low | 联网搜索，需配置 `tavily_api_key` |

> **注意**：`tavily_api_key` 建议通过环境变量 `SKILLS_AGENT_TAVILY_API_KEY` 注入，避免明文写入配置文件。

---

## 环境变量

所有环境变量的优先级高于配置文件，低于 CLI 参数。

| 环境变量 | 对应配置项 | 说明 |
|----------|-----------|------|
| `OPENAI_API_KEY` | — | OpenAI API 密钥（由 OpenAI SDK 直接读取） |
| `SKILLS_AGENT_CONFIG` | — | 指定配置文件路径（覆盖文件搜索顺序） |
| `SKILLS_AGENT_MODEL_BACKEND` | `model.backend` | `"openai"` 或 `"mock"` |
| `SKILLS_AGENT_OPENAI_MODEL` | `model.openai_model` | OpenAI 模型 ID |
| `SKILLS_AGENT_MAX_TOKENS` | `model.max_tokens` | 整数 |
| `SKILLS_AGENT_MAX_TURNS` | `agent.max_turns` | 整数 |
| `SKILLS_AGENT_DEAD_LOOP_WINDOW` | `agent.dead_loop_window` | 整数 |
| `SKILLS_AGENT_DEAD_LOOP_STALL_TURNS` | `agent.dead_loop_stall_turns` | 整数 |
| `SKILLS_AGENT_MAX_CONTEXT_TOKENS` | `agent.max_context_tokens` | 整数 |
| `SKILLS_AGENT_SKILL_ROOT` | `paths.skill_root` | 路径字符串 |
| `SKILLS_AGENT_RUN_BASE` | `paths.run_base` | 路径字符串 |
| `SKILLS_AGENT_SESSION_DIR` | `paths.session_dir` | 路径字符串 |
| `SKILLS_AGENT_LOG_DIR` | `paths.log_dir` | 路径字符串 |
| `SKILLS_AGENT_VERBOSE` | `cli.verbose` | `1`/`true`/`yes` 表示启用 |
| `SKILLS_AGENT_NO_COLOR` | `cli.color` | `1`/`true`/`yes` 表示**禁用**颜色 |
| `SKILLS_AGENT_TAVILY_API_KEY` | `tools.tavily_api_key` | Tavily Search API Key |
| `SKILLS_AGENT_TOOLS_INTERACTIVE` | `tools.interactive` | `1`/`true`/`yes` 表示启用审批提示 |

---

## CLI 参数

CLI 参数优先级最高，会覆盖配置文件和环境变量。默认值来自配置文件（或内置默认值）。

### 通用参数

| 参数 | 说明 |
|------|------|
| `--skill-root PATH` | 技能目录（覆盖 `paths.skill_root`） |
| `--model MODEL` | 模型后端 `openai`/`mock`（覆盖 `model.backend`） |
| `--log-dir PATH` | 事件日志目录（覆盖 `paths.log_dir`） |
| `--verbose` | 启用详细输出（覆盖 `cli.verbose`） |
| `--no-color` | 禁用颜色（覆盖 `cli.color`） |

### `run` 子命令专属

| 参数 | 说明 |
|------|------|
| `--run-base PATH` | 运行状态目录（覆盖 `paths.run_base`） |
| `--resume SESSION-ID` | 恢复崩溃的运行 |

### `chat` 子命令专属

| 参数 | 说明 |
|------|------|
| `--session-dir PATH` | Session 文件目录（覆盖 `paths.session_dir`） |
| `--resume SESSION-ID` | 恢复已有对话 session |

---

## 示例

### 最小化项目配置

在项目根目录创建 `.skills-agent.json`，只写需要修改的字段：

```json
{
  "model": {
    "openai_model": "gpt-4o-mini"
  },
  "paths": {
    "skill_root": "./my-skills"
  }
}
```

### 启用 Web 搜索（Tavily）

通过环境变量注入 API Key，避免密钥写入配置文件：

```bash
export SKILLS_AGENT_TAVILY_API_KEY=tvly-xxxx
skills-agent run "search for recent Python 3.13 release notes"
```

或写入配置文件（仅限本地开发，勿提交到版本控制）：

```json
{
  "tools": {
    "tavily_api_key": "tvly-xxxx"
  }
}
```

### 限制可用工具（只读模式）

```json
{
  "tools": {
    "allowed_tools": ["read_file", "list_dir", "grep"],
    "interactive": false
  }
}
```

### CI 环境（无颜色，mock 模型）

```bash
export SKILLS_AGENT_MODEL_BACKEND=mock
export SKILLS_AGENT_NO_COLOR=1
skills-agent run "run tests"
```

### 在代码中使用配置

```python
from common.config import get_config

cfg = get_config()
print(cfg.model.openai_model)   # "gpt-4o"
print(cfg.agent.max_turns)      # 20
```

加载指定文件（测试用）：

```python
from pathlib import Path
from common.config import load_config, reset_config

cfg = load_config(config_file=Path("tests/fixtures/test-config.json"))
```

测试后重置单例：

```python
from common.config import reset_config
reset_config()
```
