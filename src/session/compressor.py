"""ConversationCompressor (Phase B)."""
from model.base import ModelAdapter
from session.session import SessionContext


class ConversationCompressor:
    """
    对话历史压缩器。
    使用模型生成摘要（而非规则截断），保留语义质量。
    """

    def __init__(
        self,
        model: ModelAdapter,
        context_limit_tokens: int = 100_000,
        threshold_ratio: float = 0.25,
        compress_oldest_m: int = 2,
    ):
        self._model = model
        self._threshold = int(context_limit_tokens * threshold_ratio)
        self._m = compress_oldest_m

    def maybe_compress(self, ctx: SessionContext) -> bool:
        """
        检查是否需要压缩，需要时执行压缩并更新 ctx。
        返回 True 表示发生了压缩，False 表示无需压缩。
        """
        if ctx.estimated_recent_tokens() <= self._threshold:
            return False
        self._compress(ctx)
        return True

    def _compress(self, ctx: SessionContext) -> None:
        """取最旧 M 轮，生成摘要，更新 compressed_summary。"""
        turns_to_compress = ctx.recent_turns[: self._m * 2]
        if not turns_to_compress:
            return

        conversation_text = "\n".join(
            f"{t.role.capitalize()}: {t.content}" for t in turns_to_compress
        )
        summary = self._summarize(conversation_text)

        ctx.compressed_summary = (
            (ctx.compressed_summary + "\n" + summary).strip()
            if ctx.compressed_summary
            else summary
        )
        ctx.recent_turns = ctx.recent_turns[self._m * 2 :]

    def _summarize(self, conversation_text: str) -> str:
        """调用模型生成摘要，返回摘要字符串。"""
        prompt = (
            "以下是一段用户与 AI 助手的对话记录。\n"
            "请用不超过 600 字概括：用户的核心问题是什么，AI 执行了哪些操作，最终得出了什么结论。\n"
            "仅保留事实结论，省略执行过程与中间推理。\n\n"
            f"对话记录：\n{conversation_text}"
        )
        messages = [{"role": "user", "content": prompt}]
        action = self._model.next_action(messages)
        return action.params.get("content", "（摘要生成失败）")
