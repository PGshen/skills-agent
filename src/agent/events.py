"""事件流定义与管理"""
import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


class EventType(Enum):
    """事件类型"""
    # Run 级
    RUN_STARTED = "run_started"
    RUN_FINISHED = "run_finished"

    # Turn 级
    TURN_STARTED = "turn_started"
    TURN_FINISHED = "turn_finished"

    # 模型级
    MODEL_REQUEST = "model_request"
    MODEL_RESPONSE = "model_response"
    MODEL_DELTA = "model_delta"

    # 动作级
    ACTION_PLANNED = "action_planned"
    ACTION_VALIDATED = "action_validated"
    ACTION_EXECUTED = "action_executed"

    # 观察级
    OBSERVATION_RECORDED = "observation_recorded"

    # 计划级
    PLAN_CREATED = "plan_created"
    PLAN_UPDATED = "plan_updated"

    # 审批级
    APPROVAL_REQUIRED = "approval_required"
    APPROVAL_GRANTED = "approval_granted"
    APPROVAL_DENIED = "approval_denied"

    # 错误级
    ERROR_OCCURRED = "error_occurred"

    # 技能级
    SKILL_LOADED = "skill_loaded"
    RESOURCE_LOADED = "resource_loaded"


@dataclass
class Event:
    """事件数据容器"""
    type: EventType
    run_id: str
    turn: int
    data: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> Dict[str, Any]:
        """序列化为字典"""
        return {
            "type": self.type.value,
            "run_id": self.run_id,
            "turn": self.turn,
            "data": self.data,
            "timestamp": self.timestamp.isoformat(),
        }

    def to_json_line(self) -> str:
        """序列化为 JSONL 格式（单行 JSON）"""
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Event":
        """从字典反序列化"""
        return cls(
            type=EventType(data["type"]),
            run_id=data["run_id"],
            turn=data["turn"],
            data=data.get("data", {}),
            timestamp=datetime.fromisoformat(data["timestamp"]),
        )


class EventStream:
    """事件流管理器

    负责：
    - 将事件分发给注册的处理器
    - 将事件追加写入 JSONL 文件（可选）
    - 从 JSONL 文件回放事件
    """

    def __init__(self, output_path: Optional[Path] = None) -> None:
        """
        Args:
            output_path: 可选的 JSONL 输出文件路径。
                         若提供，每次 emit 都会将事件追加写入该文件。
        """
        self.output_path = output_path
        self._handlers: List[Callable[[Event], None]] = []

    def emit(self, event: Event) -> None:
        """发送事件

        将事件写入文件（若已配置），然后依次调用所有处理器。

        Args:
            event: 要发送的事件
        """
        if self.output_path:
            with open(self.output_path, "a", encoding="utf-8") as f:
                f.write(event.to_json_line() + "\n")

        for handler in self._handlers:
            handler(event)

    def add_handler(self, handler: Callable[[Event], None]) -> None:
        """注册事件处理器

        Args:
            handler: 接受 Event 参数的可调用对象
        """
        self._handlers.append(handler)

    def remove_handler(self, handler: Callable[[Event], None]) -> None:
        """移除已注册的处理器

        Args:
            handler: 要移除的处理器
        """
        self._handlers.remove(handler)

    @staticmethod
    def replay(file_path: Path) -> List[Event]:
        """从 JSONL 文件回放事件

        Args:
            file_path: JSONL 事件日志文件路径

        Returns:
            按文件顺序排列的 Event 列表
        """
        events: List[Event] = []
        with open(file_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                data = json.loads(line)
                events.append(Event.from_dict(data))
        return events
