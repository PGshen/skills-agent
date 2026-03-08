"""OutputSink base class (NullSink = base class itself)."""
from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..agent.plan import Plan


class OutputSink:
    """
    输出接收器基类。
    所有方法提供空实现（pass），子类只需覆盖关心的方法。
    直接用作 NullSink（测试时不需要子类或 mock）。

    设计原则：
    - 方法调用总是幂等且无副作用（除了输出）
    - 方法调用不应抛出异常（实现者负责内部异常处理）
    - 方法调用是同步的（Phase C 若需要异步，由实现者自行处理）
    """

    def on_progress(self, action: str, detail: str = "") -> None:
        """
        Agent 开始执行某个动作。
        action: 动作类型名称，如 "load_skill"、"run_script"
        detail: 补充信息，如技能名称、脚本路径
        """

    def on_plan_updated(self, plan: "Plan") -> None:
        """
        Plan 被更新（创建或全量替换）。
        plan: 新的 Plan 对象，包含当前所有步骤及其状态
        """

    def on_text_chunk(self, chunk: str, done: bool) -> None:
        """
        最终答案的流式文本片段。
        chunk: 本次新增的文本内容（Phase A 为完整答案，Phase C 为逐字片段）
        done: True 表示答案已完整生成
        """

    def on_observation(self, source: str, content: str) -> None:
        """
        工具/脚本执行结果摘要。
        source: 来源标识，如 "skill"、"script"、"resource"
        content: 结果摘要（已截断到合理长度）
        """

    def on_error(self, message: str, recoverable: bool = True) -> None:
        """
        错误发生。
        recoverable: True 表示 Agent 将继续尝试（如重试），False 表示致命错误
        """

    def on_session_end(self, turn_count: int, status: str) -> None:
        """
        会话/任务结束通知。
        status: "completed" / "failed" / "dead_loop" / "max_turns"
        """


# NullSink = OutputSink 基类本身（空实现即可）
NullSink = OutputSink
