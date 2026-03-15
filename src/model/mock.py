"""MockModel: test-driving adapter for Phase A/B."""
from agent.plan import Action, ActionType
from model.base import ModelAdapter


class MockModel(ModelAdapter):
    """
    按顺序返回预设 Action 序列。
    用于单元测试与 Phase A/B 端到端验证。
    当序列耗尽时返回 FINAL_ANSWER（避免死循环）。
    """

    def __init__(self, actions: list[Action | dict]) -> None:
        """
        actions: 预设动作列表。
        每项可以是 Action 对象或 dict（自动转换）。
        """
        self._actions: list[Action] = []
        for a in actions:
            if isinstance(a, dict):
                self._actions.append(Action.model_validate(a))
            else:
                self._actions.append(a)
        self._index = 0
        self._call_history: list[list[dict]] = []

    def next_action(self, messages: list[dict], response_format: dict = None) -> Action:
        self._call_history.append(messages)

        if self._index >= len(self._actions):
            return Action(
                type=ActionType.FINAL_ANSWER,
                params={"content": "[MockModel: action sequence exhausted]"},
            )

        action = self._actions[self._index]
        self._index += 1
        return action

    @property
    def call_count(self) -> int:
        return len(self._call_history)

    @property
    def call_history(self) -> list[list[dict]]:
        return self._call_history

    def reset(self) -> None:
        self._index = 0
        self._call_history.clear()
