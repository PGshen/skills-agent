# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Run all unit tests
uv run pytest tests/unit/ -v

# Run a single test file
uv run pytest tests/unit/test_frontmatter.py -v

# Run a single test by name
uv run pytest tests/unit/test_frontmatter.py::TestParseValid::test_basic_fields -v

# Run integration tests (skipped by default in CI)
uv run pytest tests/integration/ -v

# Install dependencies
uv sync
```

## Architecture Overview

This is an **Agent Skills system** — a Python-native framework that lets a ReAct-loop AI agent discover and invoke "skills" (task-specific bundles of prompts, scripts, and resources) without being coupled to any particular LLM.

### Three-Phase Delivery

| Phase | Status | Description |
|-------|--------|-------------|
| **A** | In progress | Core data structures + MockModel-driven ReAct loop |
| **B** | Not started | Script execution, permissions, context trimming |
| **C** | Not started | Real Anthropic API adapter + streaming JSON parser |

Current progress is tracked in [docs/dev/dev_plan.md](docs/dev/dev_plan.md). Detailed task specs are in [docs/dev/phase_a.md](docs/dev/phase_a.md), [phase_b.md](docs/dev/phase_b.md), [phase_c.md](docs/dev/phase_c.md).

### Key Abstractions

**Skill** — a directory containing `SKILL.md` (YAML frontmatter + Markdown body) plus optional scripts and reference files. Skills are the unit of capability.

**SKILL.md structure:**
```
---
name: skill-name
description: "What this skill does"
allowed-tools: [read_file, run_script]
resource-limits:
  max-script-time-sec: 30
---
# Skill body (injected into model context on LOAD_SKILL)
```

**ReAct Loop** (`AgentCore`) — each turn the model returns an `Action` (one of `LOAD_SKILL`, `LOAD_RESOURCE`, `RUN_SCRIPT`, `UPDATE_PLAN`, `FINAL_ANSWER`). The core executes it and loops until `FINAL_ANSWER` or termination.

**ModelAdapter** — the only interface between AgentCore and a model. `next_action(messages) -> Action`. Phase A/B use `MockModel`; Phase C swaps in `AnthropicAdapter` without changing the core.

**OutputSink** — decouples AgentCore from output. `NullSink` (= base class) for tests, `CLISink` for terminal (progress → stderr, answer → stdout), `SSESink` for HTTP streaming.

### Module Map

```
src/
  skills/
    frontmatter.py   # parse_skill_file() → ParsedSkillFile
                     # SkillFrontmatter (Pydantic), ResourceLimitsConfig
    metadata.py      # SkillMetadata(SkillFrontmatter) + source + skill_path
                     # ResourceLimits = ResourceLimitsConfig (alias)
    registry.py      # SkillRegistry: scan skill roots, build index
    loader.py        # SkillLoader: load_body(), load_resource()
  agent/
    plan.py          # Plan, Step, StepStatus, Action, ActionType
    state.py         # AgentState (runtime state per run)
    events.py        # EventType, Event, EventLogger → events.jsonl
    context.py       # ContextBuilder (token budget, history trimming)
    core.py          # AgentCore: the ReAct main loop
  model/
    base.py          # ModelAdapter ABC + parse_action_response + RetryAdapter
    mock.py          # MockModel(actions=[...]) for testing
    anthropic.py     # AnthropicAdapter (Phase C)
    streaming.py     # StreamingJSONParser FSM (Phase C)
  output/
    sink.py          # OutputSink base class; NullSink = OutputSink
    cli_sink.py      # CLISink: progress→stderr, answer→stdout
    sse_sink.py      # SSESink: Server-Sent Events
  tools/
    executor.py      # ScriptExecutor: subprocess with timeout + cwd
    permissions.py   # Tool whitelist enforcement
    approval.py      # User approval flow
    runtime.py       # ToolsRuntime: orchestrates executor + permissions
  session/
    session.py       # SessionContext: multi-turn conversation state
    compressor.py    # ConversationCompressor (Phase B)
  common/
    config.py        # Config loading
    logging.py       # Structured logging
    security.py      # validate_path_within_root()
  cli/
    main.py          # CLI entry point (argparse)
    run.py           # `skills-agent run` subcommand
    chat.py          # `skills-agent chat` subcommand
```

### Critical Design Invariants

1. **`allowed_tools` and other control fields must never reach model context.** `SkillRegistry.list_model_views()` is the safe boundary — it calls `to_model_view()` which projects only `{name, description, source}`.

2. **`SkillMetadata` extends `SkillFrontmatter`.** `SkillFrontmatter` (in `frontmatter.py`) is the parsed YAML. `SkillMetadata` (in `metadata.py`) adds the two runtime fields `source` and `skill_path` injected by the registry. Do not duplicate fields between them.

3. **`OutputSink` methods must not raise.** Callers (AgentCore) never wrap sink calls in try/except — the sink implementation is responsible for swallowing its own errors.

4. **`yaml.load()` is forbidden.** Always use `yaml.safe_load()`. The frontmatter parser also pre-rejects any content containing `<` before calling YAML.

5. **Path traversal guard.** `load_resource()` must reject paths containing `..` or starting with `/`, then verify the resolved path stays within the skill directory via `str(resolved).startswith(str(skill_dir.resolve()))`.

6. **`ModelAdapter.next_action(messages) -> Action` interface is frozen.** Swapping MockModel → AnthropicAdapter in Phase C must require zero changes to AgentCore.

### Test Layout

```
tests/
  unit/            # Fast, no real API calls, no filesystem side effects
  integration/     # Marked @pytest.mark.integration, skipped by default
  fixtures/
    skills/        # Sample SKILL.md directories used across tests
      example-skill/SKILL.md
      advanced-skill/SKILL.md
```

`pythonpath = ["src"]` in `pyproject.toml` — imports in tests use bare module names: `from skills.frontmatter import ...`, `from agent.plan import ...` (no `src.` prefix).

### Key Default Values

| Parameter | Default |
|-----------|---------|
| `AgentCore.max_turns` | 20 |
| `AgentCore.dead_loop_window` | 6 |
| `AgentCore.dead_loop_stall_turns` | 4 |
| `ContextBuilder.max_context_tokens` | 100,000 |
| `ResourceLimitsConfig.max_script_time_sec` | 30 |
| `ConversationCompressor.threshold_ratio` | 0.25 |
