"""skills-agent run: single-query agent execution."""

from agent.core import AgentCore
from output.cli_sink import CLISink
from skills.loader import SkillLoader


def cmd_run(args) -> int:
    """Execute `skills-agent run`."""
    from cli.main import _build_model, _build_registry, _build_run_dir_and_logger

    registry = _build_registry(args.skill_root)
    loader = SkillLoader()
    sink = CLISink(verbose=args.verbose, color=not args.no_color)

    resume_session_id = getattr(args, "resume", None)
    run_base = getattr(args, "run_base", ".agent/runs")

    if resume_session_id:
        # --- Resume path ---
        from pathlib import Path
        from agent.recovery import load_state, build_resume_context

        run_dir, event_logger = _build_run_dir_and_logger(
            run_base=run_base,
            session_id=resume_session_id,
        )
        initial_state = load_state(run_dir)
        if initial_state is None:
            print(f"Error: no state.json found in {run_dir}", flush=True)
            return 1

        resume_messages = build_resume_context(run_dir, initial_state)
    else:
        # --- Fresh run path ---
        run_dir, event_logger = _build_run_dir_and_logger(run_base=run_base)
        initial_state = None
        resume_messages = []

    model = _build_model(args, sink=sink)

    core = AgentCore(
        model=model,
        registry=registry,
        loader=loader,
        event_logger=event_logger,
        sink=sink,
        run_dir=run_dir,
    )

    core.run(args.query, history_messages=resume_messages, initial_state=initial_state)
    return 0
