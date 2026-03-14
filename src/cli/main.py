"""CLI entry point and subcommand routing."""

import argparse
import sys
import tempfile
import uuid
from pathlib import Path

from common.config import Config, get_config
from common.logging import setup_logging


def _add_skill_root(parser: argparse.ArgumentParser, cfg: Config) -> None:
    parser.add_argument(
        "--skill-root",
        default=cfg.paths.skill_root,
        metavar="PATH",
        help=f"Directory to scan for skills (default: {cfg.paths.skill_root!r})",
    )


def _add_model(parser: argparse.ArgumentParser, cfg: Config) -> None:
    parser.add_argument(
        "--model",
        default=cfg.model.backend,
        choices=["openai", "mock"],
        metavar="MODEL",
        help=f"Model backend: 'openai' or 'mock' (default: {cfg.model.backend!r})",
    )


def _build_model(args, sink=None):
    """Instantiate the model adapter selected by --model."""
    cfg = get_config()
    model_id = getattr(args, "model", cfg.model.backend)
    if model_id == "mock":
        from model.mock import MockModel
        return MockModel(actions=[
            {"type": "final_answer", "params": {"content": "[MockModel] ready"}}
        ])
    from model.openai import OpenAIAdapter
    return OpenAIAdapter(
        model=cfg.model.openai_model,
        max_tokens=cfg.model.max_tokens,
        sink=sink,
    )


def _build_tools_runtime(cfg: Config):
    """Instantiate ToolsRuntime from config, wiring up the web search adapter if configured."""
    from tools.runtime import ToolsRuntime

    web_adapter = None
    if cfg.tools.tavily_api_key:
        from tools.executor import TavilyAdapter
        web_adapter = TavilyAdapter(api_key=cfg.tools.tavily_api_key)

    return ToolsRuntime(
        global_allowed_tools=cfg.tools.allowed_tools,
        interactive=cfg.tools.interactive,
        web_search_adapter=web_adapter,
    )


def _build_registry(skill_root: str):
    from skills.registry import SkillRegistry

    roots = [{"source": "project", "path": skill_root, "priority": 0}]
    builtin_dir = Path(__file__).parent.parent.parent / "skills_builtin"
    if builtin_dir.is_dir():
        roots.append({"source": "builtin", "path": str(builtin_dir), "priority": 100})
    return SkillRegistry(roots)


def _build_run_dir_and_logger(
    run_base: str = ".agent/runs",
    session_id: str | None = None,
):
    from agent.events import EventLogger

    if session_id is None:
        session_id = str(uuid.uuid4())
    run_dir = Path(run_base) / session_id
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = str(run_dir / "events.jsonl")
    return run_dir, EventLogger(path=log_path, session_id=session_id)


def _build_event_logger(log_dir: str | None = None):
    from agent.events import EventLogger

    session_id = str(uuid.uuid4())
    if log_dir is None:
        log_dir = tempfile.mkdtemp()
    log_path = str(Path(log_dir) / f"{session_id}.jsonl")
    return EventLogger(path=log_path, session_id=session_id)


def _add_log_dir(parser: argparse.ArgumentParser, cfg: Config) -> None:
    parser.add_argument(
        "--log-dir",
        default=cfg.paths.log_dir,
        metavar="PATH",
        help="Directory for event log files (default: system temp dir)",
    )


def main() -> None:
    cfg = get_config()

    parser = argparse.ArgumentParser(
        prog="skills-agent",
        description="Agent Skills System — ReAct loop with skill discovery",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    # --- run ---
    run_parser = subparsers.add_parser(
        "run",
        help="Run the agent on a single query",
        description="Run the ReAct agent on a query using MockModel (Phase A).",
    )
    _add_skill_root(run_parser, cfg)
    _add_log_dir(run_parser, cfg)
    _add_model(run_parser, cfg)
    run_parser.add_argument("query", help="User query / task description")
    run_parser.add_argument(
        "--resume",
        default=None,
        metavar="SESSION-ID",
        help="Resume a crashed run by session ID (reads from .agent/runs/<SESSION-ID>/)",
    )
    run_parser.add_argument(
        "--run-base",
        default=cfg.paths.run_base,
        metavar="PATH",
        help=f"Base directory for run state (default: {cfg.paths.run_base!r})",
    )
    run_parser.add_argument(
        "--verbose",
        action="store_true",
        default=cfg.cli.verbose,
        help="Show observation details in stderr",
    )
    run_parser.add_argument(
        "--no-color",
        action="store_true",
        default=not cfg.cli.color,
        help="Disable ANSI color output",
    )

    # --- chat ---
    chat_parser = subparsers.add_parser(
        "chat",
        help="Start interactive multi-turn chat session",
        description="Run the agent in interactive chat mode with session persistence.",
    )
    _add_skill_root(chat_parser, cfg)
    _add_log_dir(chat_parser, cfg)
    _add_model(chat_parser, cfg)
    chat_parser.add_argument(
        "--session-dir",
        default=cfg.paths.session_dir,
        metavar="PATH",
        help=f"Directory to store session files (default: {cfg.paths.session_dir!r})",
    )
    chat_parser.add_argument(
        "--resume",
        default=None,
        metavar="SESSION-ID",
        help="Resume an existing session by ID",
    )
    chat_parser.add_argument(
        "--verbose",
        action="store_true",
        default=cfg.cli.verbose,
        help="Show observation details in stderr",
    )
    chat_parser.add_argument(
        "--no-color",
        action="store_true",
        default=not cfg.cli.color,
        help="Disable ANSI color output",
    )

    # --- skills ---
    skills_parser = subparsers.add_parser(
        "skills",
        help="Skill management commands",
        description="Commands for inspecting available skills.",
    )
    skills_sub = skills_parser.add_subparsers(dest="skills_command", metavar="SUBCOMMAND")

    # skills list
    list_parser = skills_sub.add_parser(
        "list",
        help="List all available skills",
        description="Print a table of discovered skills.",
    )
    _add_skill_root(list_parser, cfg)

    # skills inspect
    inspect_parser = skills_sub.add_parser(
        "inspect",
        help="Show full details of a skill",
        description="Print all frontmatter fields for a named skill.",
    )
    _add_skill_root(inspect_parser, cfg)
    inspect_parser.add_argument("skill_name", metavar="SKILL-NAME", help="Name of the skill")

    args = parser.parse_args()
    setup_logging(level=cfg.cli.log_level, log_file=cfg.cli.log_file)

    if args.command == "run":
        from cli.run import cmd_run
        sys.exit(cmd_run(args))
    elif args.command == "chat":
        from cli.chat import run_chat
        run_chat(args)
        sys.exit(0)
    elif args.command == "skills":
        from cli.skills import cmd_skills_inspect, cmd_skills_list
        if args.skills_command == "list":
            sys.exit(cmd_skills_list(args))
        elif args.skills_command == "inspect":
            sys.exit(cmd_skills_inspect(args))
        else:
            skills_parser.print_help()
            sys.exit(1)
    else:
        parser.print_help()
        sys.exit(1)
