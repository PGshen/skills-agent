"""CLI entry point and subcommand routing."""

import argparse
import sys
import tempfile
import uuid
from pathlib import Path


def _build_registry(skill_root: str):
    from skills.registry import SkillRegistry

    roots = [{"source": "project", "path": skill_root, "priority": 0}]

    # Include built-in skills directory if it exists next to the project root
    builtin_dir = Path(__file__).parent.parent.parent / "skills_builtin"
    if builtin_dir.is_dir():
        roots.append({"source": "builtin", "path": str(builtin_dir), "priority": 100})

    return SkillRegistry(roots)


def _build_event_logger(log_dir: str | None = None):
    from agent.events import EventLogger

    session_id = str(uuid.uuid4())
    if log_dir is None:
        log_dir = tempfile.mkdtemp()
    log_path = str(Path(log_dir) / f"{session_id}.jsonl")
    return EventLogger(path=log_path, session_id=session_id)


def _add_skill_root(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--skill-root",
        default="./skills",
        metavar="PATH",
        help="Directory to scan for skills (default: ./skills)",
    )


def _add_log_dir(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--log-dir",
        default=None,
        metavar="PATH",
        help="Directory for event log files (default: system temp dir)",
    )


def main() -> None:
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
    _add_skill_root(run_parser)
    _add_log_dir(run_parser)
    run_parser.add_argument("query", help="User query / task description")
    run_parser.add_argument(
        "--verbose", action="store_true", help="Show observation details in stderr"
    )
    run_parser.add_argument(
        "--no-color", action="store_true", help="Disable ANSI color output"
    )

    # --- chat ---
    chat_parser = subparsers.add_parser(
        "chat",
        help="Start interactive multi-turn chat session",
        description="Run the agent in interactive chat mode with session persistence.",
    )
    _add_skill_root(chat_parser)
    _add_log_dir(chat_parser)
    chat_parser.add_argument(
        "--session-dir",
        default=".agent/sessions",
        metavar="PATH",
        help="Directory to store session files (default: .agent/sessions)",
    )
    chat_parser.add_argument(
        "--resume",
        default=None,
        metavar="SESSION-ID",
        help="Resume an existing session by ID",
    )
    chat_parser.add_argument(
        "--verbose", action="store_true", help="Show observation details in stderr"
    )
    chat_parser.add_argument(
        "--no-color", action="store_true", help="Disable ANSI color output"
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
    _add_skill_root(list_parser)

    # skills inspect
    inspect_parser = skills_sub.add_parser(
        "inspect",
        help="Show full details of a skill",
        description="Print all frontmatter fields for a named skill.",
    )
    _add_skill_root(inspect_parser)
    inspect_parser.add_argument("skill_name", metavar="SKILL-NAME", help="Name of the skill")

    args = parser.parse_args()

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
