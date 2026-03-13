"""skills-agent chat: interactive multi-turn session mode."""

import sys

from agent.core import AgentCore
from common.config import get_config
from output.cli_sink import CLISink
from session.compressor import ConversationCompressor
from session.session import SessionManager
from skills.loader import SkillLoader


def run_chat(args) -> None:
    """Interactive chat mode main loop."""
    from cli.main import _build_event_logger, _build_model, _build_registry

    cfg = get_config()

    session_manager = SessionManager(sessions_root=args.session_dir)

    # Support --resume <session-id> to restore an existing session
    use_color = not getattr(args, "no_color", False)
    _cyan  = "\033[36m" if use_color else ""
    _dim   = "\033[2m"  if use_color else ""
    _reset = "\033[0m"  if use_color else ""

    if getattr(args, "resume", None):
        ctx = session_manager.load(args.resume)
        if ctx is None:
            print(f"Session '{args.resume}' not found.", file=sys.stderr)
            return
        print(f"{_dim}Resumed session {ctx.session_id} · turn {ctx.total_turn_count}{_reset}")
    else:
        ctx = session_manager.create(model_id=getattr(args, "model", cfg.model.backend))
        print(f"{_dim}Session {ctx.session_id}{_reset}")

    sink = CLISink(
        verbose=getattr(args, "verbose", False),
        color=use_color,
    )

    registry = _build_registry(args.skill_root)
    loader = SkillLoader()
    event_logger = _build_event_logger(getattr(args, "log_dir", None))

    print(f"{_dim}Type 'exit' or Ctrl+C to quit.{_reset}\n")

    model = _build_model(args, sink=sink)

    # One compressor instance for the whole session; reuses the same model adapter.
    compressor = ConversationCompressor(
        model=model,
        context_limit_tokens=cfg.agent.max_context_tokens,
        threshold_ratio=cfg.compressor.threshold_ratio,
        compress_oldest_m=cfg.compressor.compress_oldest_m,
    )

    while True:
        try:
            user_input = input(f"{_cyan}>{_reset} ").strip()
        except (KeyboardInterrupt, EOFError):
            print(f"\n{_dim}Goodbye.{_reset}")
            break

        if user_input.lower() in ("exit", "quit", "q"):
            break
        if not user_input:
            continue

        core = AgentCore(
            model=model,
            registry=registry,
            loader=loader,
            event_logger=event_logger,
            sink=sink,
            max_turns=cfg.agent.max_turns,
            dead_loop_window=cfg.agent.dead_loop_window,
            dead_loop_stall_turns=cfg.agent.dead_loop_stall_turns,
            max_context_tokens=cfg.agent.max_context_tokens,
        )

        # Assemble history layer from session context
        history_messages = ctx.build_history_messages()

        # Run agent
        try:
            response = core.run(user_input, history_messages=history_messages)
        except Exception as exc:  # noqa: BLE001
            sink.on_error(str(exc), recoverable=False)
            continue

        # Persist state
        ctx.record_turn(user_input, response)
        session_manager.append_log(ctx, user_input, response)

        # Phase B: compress if recent_turns exceeded threshold, then save
        compressor.maybe_compress(ctx)
        session_manager.save(ctx)
