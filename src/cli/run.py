"""skills-agent run: single-query agent execution."""

from agent.core import AgentCore
from model.mock import MockModel
from output.cli_sink import CLISink
from skills.loader import SkillLoader


def cmd_run(args) -> int:
    """Execute `skills-agent run`."""
    from cli.main import _build_event_logger, _build_registry

    registry = _build_registry(args.skill_root)
    loader = SkillLoader()
    event_logger = _build_event_logger(args.log_dir)
    sink = CLISink(verbose=args.verbose, color=not args.no_color)

    # Phase A: use MockModel that immediately returns FINAL_ANSWER
    mock_actions = [
        {
            "type": "final_answer",
            "params": {"content": f"[MockModel] Received: {args.query}"},
        }
    ]
    model = MockModel(actions=mock_actions)

    core = AgentCore(
        model=model,
        registry=registry,
        loader=loader,
        event_logger=event_logger,
        sink=sink,
    )

    core.run(args.query)
    return 0
