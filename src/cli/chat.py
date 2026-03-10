"""skills-agent chat: interactive multi-turn session mode."""

import sys

from agent.core import AgentCore
from model.mock import MockModel
from output.cli_sink import CLISink
from session.compressor import ConversationCompressor
from session.session import SessionManager
from skills.loader import SkillLoader


def run_chat(args) -> None:
    """Interactive chat mode main loop."""
    from cli.main import _build_event_logger, _build_registry

    session_manager = SessionManager(sessions_root=args.session_dir)

    # Support --resume <session-id> to restore an existing session
    if getattr(args, "resume", None):
        ctx = session_manager.load(args.resume)
        if ctx is None:
            print(f"Session '{args.resume}' not found.", file=sys.stderr)
            return
        print(f"Resuming session {ctx.session_id} (turn {ctx.total_turn_count})")
    else:
        ctx = session_manager.create(model_id="mock")
        print(f"Chat session started. Session: {ctx.session_id}")

    sink = CLISink(
        verbose=getattr(args, "verbose", False),
        color=not getattr(args, "no_color", False),
    )

    registry = _build_registry(args.skill_root)
    loader = SkillLoader()
    event_logger = _build_event_logger(getattr(args, "log_dir", None))

    print("Type 'exit' or press Ctrl+C to quit.\n")

    # Phase B: one compressor instance for the whole session; model reference
    # is updated each turn so the compressor reuses the same adapter as AgentCore.
    compressor = ConversationCompressor(model=MockModel(actions=[]))

    while True:
        try:
            user_input = input("You: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nGoodbye.")
            break

        if user_input.lower() in ("exit", "quit", "q"):
            break
        if not user_input:
            continue

        # Phase A: MockModel with per-turn FINAL_ANSWER echoing the user input
        model = MockModel(actions=[
            {
                "type": "final_answer",
                "params": {"content": f"[MockModel] Received: {user_input}"},
            }
        ])
        core = AgentCore(
            model=model,
            registry=registry,
            loader=loader,
            event_logger=event_logger,
            sink=sink,
        )

        # Assemble history layer from session context
        history_messages = ctx.build_history_messages()

        # Run agent
        response = core.run(user_input, history_messages=history_messages)

        # Persist state
        ctx.record_turn(user_input, response)
        session_manager.append_log(ctx, user_input, response)

        # Phase B: compress if recent_turns exceeded threshold, then save
        compressor._model = core._model
        compressor.maybe_compress(ctx)
        session_manager.save(ctx)
