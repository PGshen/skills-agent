"""Configuration loading: defaults → config file → environment variable overrides.

Search order for config file (first found wins):
  1. SKILLS_AGENT_CONFIG env var (explicit path)
  2. ./.skills-agent.json  (project-level)
  3. ~/.skills-agent/config.json  (user-level)

Environment variables always take highest precedence (after CLI flags).
See docs/configuration.md for the full reference.
"""

import json
import os
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Config sections
# ---------------------------------------------------------------------------

class ModelConfig(BaseModel):
    """LLM backend settings."""
    backend: str = "openai"          # "openai" | "mock"
    openai_model: str = "gpt-4o"     # OpenAI model ID
    max_tokens: int = 4096           # Max tokens per response


class AgentConfig(BaseModel):
    """ReAct loop behaviour."""
    max_turns: int = 20
    dead_loop_window: int = 6        # Sliding window for action-hash dedup
    dead_loop_stall_turns: int = 4   # Plan stall warning threshold
    max_context_tokens: int = 100_000


class PathsConfig(BaseModel):
    """File-system paths used at runtime."""
    skill_root: str = "./skills"
    run_base: str = ".agent/runs"       # Per-run state (crash recovery)
    session_dir: str = ".agent/sessions"  # Multi-turn chat sessions
    log_dir: Optional[str] = None       # Event log directory (None = temp dir)


class CompressorConfig(BaseModel):
    """Conversation history compression."""
    threshold_ratio: float = 0.25    # Compress when recent_tokens > limit * ratio
    compress_oldest_m: int = 2       # Number of oldest turns to compress each cycle


class CLIConfig(BaseModel):
    """Terminal output defaults."""
    verbose: bool = False            # Show observation details on stderr
    color: bool = True               # ANSI colour output
    log_level: str = "WARNING"       # Logging level: DEBUG, INFO, WARNING, ERROR
    log_file: Optional[str] = None   # Optional file path for log output


class ToolsConfig(BaseModel):
    """Tool execution settings."""
    allowed_tools: list[str] = Field(default_factory=lambda: [
        "read_file", "list_dir", "grep",
        "run_script", "write_file", "delete_file", "web_search",
    ])
    interactive: bool = True         # Show approval prompts for high-risk tools
    tavily_api_key: Optional[str] = None  # API key for web_search via Tavily


class Config(BaseModel):
    model: ModelConfig = Field(default_factory=ModelConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    compressor: CompressorConfig = Field(default_factory=CompressorConfig)
    cli: CLIConfig = Field(default_factory=CLIConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)


# ---------------------------------------------------------------------------
# Loading helpers
# ---------------------------------------------------------------------------

def _find_config_file() -> Optional[Path]:
    """Return the first config file found in the search order, or None."""
    explicit = os.environ.get("SKILLS_AGENT_CONFIG")
    if explicit:
        p = Path(explicit)
        if p.is_file():
            return p
        raise FileNotFoundError(
            f"SKILLS_AGENT_CONFIG points to a non-existent file: {explicit}"
        )

    project_cfg = Path(".skills-agent.json")
    if project_cfg.is_file():
        return project_cfg

    user_cfg = Path.home() / ".skills-agent" / "config.json"
    if user_cfg.is_file():
        return user_cfg

    return None


def _load_file(path: Path) -> dict:
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in config file {path}: {exc}") from exc


def _str_to_bool(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes")


def _apply_env_overrides(data: dict) -> dict:
    """Merge SKILLS_AGENT_* environment variables into the data dict in-place."""
    env = os.environ

    model = data.setdefault("model", {})
    if v := env.get("SKILLS_AGENT_MODEL_BACKEND"):
        model["backend"] = v
    if v := env.get("SKILLS_AGENT_OPENAI_MODEL"):
        model["openai_model"] = v
    if v := env.get("SKILLS_AGENT_MAX_TOKENS"):
        model["max_tokens"] = int(v)

    agent = data.setdefault("agent", {})
    if v := env.get("SKILLS_AGENT_MAX_TURNS"):
        agent["max_turns"] = int(v)
    if v := env.get("SKILLS_AGENT_DEAD_LOOP_WINDOW"):
        agent["dead_loop_window"] = int(v)
    if v := env.get("SKILLS_AGENT_DEAD_LOOP_STALL_TURNS"):
        agent["dead_loop_stall_turns"] = int(v)
    if v := env.get("SKILLS_AGENT_MAX_CONTEXT_TOKENS"):
        agent["max_context_tokens"] = int(v)

    paths = data.setdefault("paths", {})
    if v := env.get("SKILLS_AGENT_SKILL_ROOT"):
        paths["skill_root"] = v
    if v := env.get("SKILLS_AGENT_RUN_BASE"):
        paths["run_base"] = v
    if v := env.get("SKILLS_AGENT_SESSION_DIR"):
        paths["session_dir"] = v
    if v := env.get("SKILLS_AGENT_LOG_DIR"):
        paths["log_dir"] = v

    cli = data.setdefault("cli", {})
    if v := env.get("SKILLS_AGENT_VERBOSE"):
        cli["verbose"] = _str_to_bool(v)
    if v := env.get("SKILLS_AGENT_NO_COLOR"):
        # NO_COLOR=1 → color=False
        cli["color"] = not _str_to_bool(v)
    if v := env.get("SKILLS_AGENT_LOG_LEVEL"):
        cli["log_level"] = v
    if v := env.get("SKILLS_AGENT_LOG_FILE"):
        cli["log_file"] = v

    tools = data.setdefault("tools", {})
    if v := env.get("SKILLS_AGENT_TAVILY_API_KEY"):
        tools["tavily_api_key"] = v
    if v := env.get("SKILLS_AGENT_TOOLS_INTERACTIVE"):
        tools["interactive"] = _str_to_bool(v)

    return data


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_config(config_file: Optional[Path] = None) -> Config:
    """Load and return a Config, merging file data with env var overrides.

    Args:
        config_file: Explicit path to a config file. If None, the normal
                     search order (env var → project → user) is used.
    """
    if config_file is not None:
        data = _load_file(config_file)
    else:
        found = _find_config_file()
        data = _load_file(found) if found is not None else {}

    data = _apply_env_overrides(data)
    return Config.model_validate(data)


_config_cache: Optional[Config] = None


def get_config() -> Config:
    """Return the process-wide singleton Config (lazy-loaded on first call)."""
    global _config_cache
    if _config_cache is None:
        _config_cache = load_config()
    return _config_cache


def reset_config() -> None:
    """Clear the singleton cache. Intended for tests."""
    global _config_cache
    _config_cache = None
