"""CLISink: real-time terminal output."""
import sys
from .sink import OutputSink
from agent.plan import Plan, StepStatus

# ANSI 颜色码
_DIM    = "\033[2m"
_RED    = "\033[31m"
_YELLOW = "\033[33m"
_RESET  = "\033[0m"

# 步骤状态符号
_STEP_ICON = {
    StepStatus.PENDING:     "○",
    StepStatus.IN_PROGRESS: "→",
    StepStatus.DONE:        "✓",
    StepStatus.FAILED:      "✗",
}


class CLISink(OutputSink):
    """
    终端输出实现。

    使用方：
        sink = CLISink(verbose=args.verbose)
        core = AgentCore(..., sink=sink)
        core.run("query")
    """

    def __init__(self, verbose: bool = False, color: bool = True):
        self._verbose = verbose
        self._color = color and sys.stderr.isatty()
        self._answer_started = False  # 是否已开始输出答案（用于首次换行）

    def _dim(self, text: str) -> str:
        return f"{_DIM}{text}{_RESET}" if self._color else text

    def _red(self, text: str) -> str:
        return f"{_RED}{text}{_RESET}" if self._color else text

    def _yellow(self, text: str) -> str:
        return f"{_YELLOW}{text}{_RESET}" if self._color else text

    # ── 进度（→ stderr）────────────────────────────────────────────────

    def on_progress(self, action: str, detail: str = "") -> None:
        label = f"{action}: {detail}" if detail else action
        print(self._dim(f"  → {label}"), file=sys.stderr)

    def on_plan_updated(self, plan: Plan) -> None:
        print(self._dim("[Plan]"), file=sys.stderr)
        for step in plan.steps:
            icon = _STEP_ICON.get(step.status, "?")
            line = f"  {icon} {step.description}"
            if step.status == StepStatus.IN_PROGRESS:
                line = self._dim(line)
            print(line, file=sys.stderr)

    def on_observation(self, source: str, content: str) -> None:
        if not self._verbose:
            return
        # 截断长观察，避免刷屏
        preview = content[:300].replace("\n", " ")
        if len(content) > 300:
            preview += "..."
        print(self._dim(f"  [obs/{source}] {preview}"), file=sys.stderr)

    def on_error(self, message: str, recoverable: bool = True) -> None:
        if recoverable:
            print(self._yellow(f"[WARN] {message}"), file=sys.stderr)
        else:
            print(self._red(f"[ERROR] {message}"), file=sys.stderr)

    def on_session_end(self, turn_count: int, status: str) -> None:
        print(
            self._dim(f"\nCompleted in {turn_count} turns ({status})."),
            file=sys.stderr,
        )

    # ── 最终答案（→ stdout）────────────────────────────────────────────

    def on_text_chunk(self, chunk: str, done: bool) -> None:
        if not self._answer_started and chunk:
            # 首个 chunk 前确保进度行已换行
            print(file=sys.stderr, flush=True)
            self._answer_started = True

        sys.stdout.write(chunk)
        sys.stdout.flush()

        if done:
            # 答案结束，确保末尾有换行
            if chunk and not chunk.endswith("\n"):
                sys.stdout.write("\n")
                sys.stdout.flush()
            self._answer_started = False
