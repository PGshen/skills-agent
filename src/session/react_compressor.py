"""ReactHistoryCompressor: semantic compression of ReactAgent turn history."""

from model.base import ModelAdapter


class ReactHistoryCompressor:
    """Compresses a list of (action_msg, observation_msg) pairs into a single
    summary message by calling the model.

    Used by ReactContextBuilder when the accumulated react history exceeds the
    token budget. Summarisation preserves semantic information (what was done,
    what was found) instead of discarding raw content.
    """

    def __init__(self, model: ModelAdapter):
        self._model = model

    def compress(self, pairs: list[tuple[dict, dict]]) -> dict:
        """Summarise a list of (assistant_action_msg, user_observation_msg) pairs.

        Returns a single user-role message dict containing the summary,
        formatted so ReactAgent can understand what happened in those turns.
        """
        if not pairs:
            return {"role": "user", "content": "[History summary: no prior actions]"}

        turns_text = self._format_pairs(pairs)
        prompt = (
            "以下是一个执行代理在完成任务过程中的历史操作记录（JSON 动作 + 观测结果）。\n"
            "请用不超过 400 字简洁总结：代理执行了哪些操作，发现了什么关键信息，"
            "目前任务进展如何。仅保留对后续执行有参考价值的事实，省略原始数据细节。\n\n"
            f"历史记录：\n{turns_text}"
        )
        messages = [{"role": "user", "content": prompt}]
        action = self._model.next_action(messages)
        summary = action.params.get("content", "(summary unavailable)")
        return {"role": "user", "content": f"[History summary]\n{summary}"}

    def _format_pairs(self, pairs: list[tuple[dict, dict]]) -> str:
        lines: list[str] = []
        for i, (action_msg, obs_msg) in enumerate(pairs, 1):
            lines.append(f"Turn {i} — Action: {action_msg.get('content', '')}")
            lines.append(f"Turn {i} — Observation: {obs_msg.get('content', '')}")
        return "\n".join(lines)
