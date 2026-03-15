"""ModelAdapter abstract base class, parse_action_response, RetryAdapter."""
import json
import re
from abc import ABC, abstractmethod

from agent.plan import Action, ActionType


class ModelResponseError(Exception):
    """模型响应无法解析为合法 Action 时抛出。"""


class ModelAdapter(ABC):
    """
    模型适配器抽象基类。
    Agent Core 仅依赖此接口，不依赖具体实现。
    """

    @abstractmethod
    def next_action(self, messages: list[dict], response_format: dict = None) -> Action:
        """
        向模型发送 messages，解析并返回单个 Action。
        messages 格式：OpenAI 风格 [{"role": str, "content": str}, ...]
        失败时抛出 ModelResponseError。

        response_format: 可选的结构化输出 schema（如 OpenAI JSON schema format）。
        不支持该参数的实现可忽略它。
        """

    def close(self) -> None:
        """释放资源（连接池等），默认空实现。"""


def parse_action_response(raw: str) -> Action:
    """
    从模型原始文本解析 Action。
    三步降级解析：
    1. 直接 json.loads()
    2. 提取最外层 {} 块再解析（去除 code fence 等噪声）
    3. 仍失败则抛出 ModelResponseError
    """
    raw = raw.strip()

    # 步骤 1：直接解析
    try:
        data = json.loads(raw)
        return _validate_action(data)
    except (json.JSONDecodeError, ValueError):
        pass

    # 步骤 2：提取 JSON 块
    match = re.search(r'\{.*\}', raw, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group())
            return _validate_action(data)
        except (json.JSONDecodeError, ValueError):
            pass

    raise ModelResponseError(f"Cannot parse model response as Action: {raw[:200]!r}")


def _validate_action(data: dict) -> Action:
    """最小 schema 校验：type 必须是合法枚举，params 必须是 dict。"""
    if "type" not in data:
        raise ValueError("Missing 'type' field")
    return Action(type=data["type"], params=data.get("params", {}))


class RetryAdapter(ModelAdapter):
    """
    包装任意 ModelAdapter，在解析失败时自动重试。
    重试时附加纠错消息（要求模型仅输出修正 JSON）。
    """

    def __init__(self, inner: ModelAdapter, max_retries: int = 2):
        self._inner = inner
        self._max_retries = max_retries

    def next_action(self, messages: list[dict]) -> Action:
        last_error = None
        for attempt in range(self._max_retries + 1):
            try:
                return self._inner.next_action(messages)
            except ModelResponseError as e:
                last_error = e
                if attempt < self._max_retries:
                    messages = messages + [{
                        "role": "user",
                        "content": (
                            f"你的上一次输出无法解析：{e}\n"
                            "请仅输出一个合法的 JSON 对象，格式：\n"
                            '{"type": "<动作类型>", "params": {...}}'
                        ),
                    }]
        raise last_error
