"""CLISink: real-time terminal output."""
import sys
import threading
from .sink import OutputSink
from agent.plan import Plan, StepStatus

# ANSI 颜色码
_BOLD   = "\033[1m"
_DIM    = "\033[2m"
_CYAN   = "\033[36m"
_GREEN  = "\033[32m"
_BLUE   = "\033[34m"
_RED    = "\033[31m"
_YELLOW = "\033[33m"
_RESET  = "\033[0m"

# Spinner 帧
_SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

# 步骤状态符号
_STEP_ICON = {
    StepStatus.PENDING:     "○",
    StepStatus.IN_PROGRESS: "→",
    StepStatus.DONE:        "✓",
    StepStatus.FAILED:      "✗",
}

# 动作类型显示样式: (颜色, 显示标签)
_ACTION_STYLES: dict[str, tuple[str, str]] = {
    "load_skill":     (_CYAN,   "Load Skill"),
    "load_resource":  (_BLUE,   "Load Resource"),
    "run_script":     (_GREEN,  "Run Script"),
}


class CLISink(OutputSink):
    """
    终端输出实现。

    使用方：
        sink = CLISink(verbose=args.verbose)
        core = AgentCore(..., sink=sink)
        core.run("query")
    """

    def __init__(self, verbose: bool = False, color: bool = True, max_obs_lines: int = 8):
        self._verbose = verbose
        self._color = color and sys.stderr.isatty()
        self._answer_started = False
        self._max_obs_lines = max_obs_lines
        # Spinner state
        self._spinner_stop = threading.Event()
        self._spinner_thread: threading.Thread | None = None
        self._spinner_active = False

    # ── ANSI 辅助 ─────────────────────────────────────────────────────

    def _fmt(self, code: str, text: str) -> str:
        return f"{code}{text}{_RESET}" if self._color else text

    # ── Spinner ────────────────────────────────────────────────────────

    def _clear_spinner(self) -> None:
        """停止 spinner 并清除当前行。"""
        if not self._spinner_active:
            return
        self._spinner_stop.set()
        if self._spinner_thread:
            self._spinner_thread.join(timeout=0.3)
            self._spinner_thread = None
        self._spinner_active = False
        if self._color:
            sys.stderr.write("\r\033[K")
            sys.stderr.flush()

    def on_thinking_start(self, turn: int) -> None:
        """模型推理开始：显示 spinner。"""
        if not self._color:
            return
        self._clear_spinner()
        label = f"Thinking… (turn {turn})"
        self._spinner_stop.clear()
        self._spinner_active = True

        def _spin() -> None:
            i = 0
            while not self._spinner_stop.is_set():
                frame = _SPINNER_FRAMES[i % len(_SPINNER_FRAMES)]
                sys.stderr.write(f"\r{_DIM}{frame} {label}{_RESET}")
                sys.stderr.flush()
                i += 1
                self._spinner_stop.wait(0.08)

        self._spinner_thread = threading.Thread(target=_spin, daemon=True)
        self._spinner_thread.start()

    # ── 进度（→ stderr）────────────────────────────────────────────────

    def on_progress(self, action: str, detail: str = "") -> None:
        self._clear_spinner()
        color, label = _ACTION_STYLES.get(action, (_DIM, action))
        detail_part = f"  {self._fmt(_DIM, detail)}" if detail else ""
        print(f"  {self._fmt(color, f'◆ {label}')}{detail_part}", file=sys.stderr)

    def on_plan_updated(self, plan: Plan) -> None:
        self._clear_spinner()
        header = self._fmt(_DIM, "─── Plan " + "─" * 30)
        print(header, file=sys.stderr)
        for step in plan.steps:
            icon = _STEP_ICON.get(step.status, "?")
            line = f"  {icon}  {step.description}"
            if step.status == StepStatus.IN_PROGRESS:
                line = self._fmt(_CYAN, line)
            elif step.status == StepStatus.DONE:
                line = self._fmt(_DIM, line)
            elif step.status == StepStatus.FAILED:
                line = self._fmt(_RED, line)
            print(line, file=sys.stderr)

    def on_observation(self, source: str, content: str) -> None:
        self._clear_spinner()
        if not content.strip():
            return
        limit = self._max_obs_lines * 3 if self._verbose else self._max_obs_lines
        lines = content.splitlines()
        print(self._fmt(_DIM, f"  ┌ {source}"), file=sys.stderr)
        shown = lines[:limit]
        for line in shown:
            print(f"  │ {line}", file=sys.stderr)
        if len(lines) > limit:
            print(self._fmt(_DIM, f"  └ … {len(lines) - limit} more lines"), file=sys.stderr)
        else:
            print(self._fmt(_DIM, "  └"), file=sys.stderr)

    def on_error(self, message: str, recoverable: bool = True) -> None:
        self._clear_spinner()
        if recoverable:
            print(self._fmt(_YELLOW, f"  ⚠  {message}"), file=sys.stderr)
        else:
            print(self._fmt(_RED, f"  ✗  {message}"), file=sys.stderr)

    def on_session_end(self, turn_count: int, status: str) -> None:
        self._clear_spinner()
        print(
            self._fmt(_DIM, f"\n  Completed in {turn_count} turn(s) · {status}"),
            file=sys.stderr,
        )

    # ── 最终答案（→ stdout）────────────────────────────────────────────

    def on_text_chunk(self, chunk: str, done: bool) -> None:
        self._clear_spinner()
        if not self._answer_started and chunk:
            # 首个 chunk 前确保进度行已换行
            print(file=sys.stderr, flush=True)
            self._answer_started = True

        sys.stdout.write(chunk)
        sys.stdout.flush()

        if done:
            if chunk and not chunk.endswith("\n"):
                sys.stdout.write("\n")
                sys.stdout.flush()
            self._answer_started = False
