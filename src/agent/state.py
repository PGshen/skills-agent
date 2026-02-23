from dataclasses import dataclass, field
from typing import List, Dict, Optional, Any
from enum import Enum
from datetime import datetime


@dataclass
class ToolBudget:
    """资源预算"""
    # 上限配置
    max_turns: int = 12
    max_tool_calls: int = 30
    max_script_executions: int = 10
    max_context_tokens: int = 100000

    # 运行时追踪
    turns_used: int = 0
    tool_calls_used: int = 0
    script_executions_used: int = 0
    context_tokens_used: int = 0

    def can_continue(self) -> bool:
        """检查是否还有预算"""
        return (
            self.turns_used < self.max_turns and
            self.tool_calls_used < self.max_tool_calls and
            self.script_executions_used < self.max_script_executions
        )

    def is_near_limit(self, threshold: float = 0.8) -> bool:
        """检查是否接近预算上限"""
        ratios = [
            self.turns_used / self.max_turns if self.max_turns > 0 else 0,
            self.tool_calls_used / self.max_tool_calls if self.max_tool_calls > 0 else 0,
            self.script_executions_used / self.max_script_executions if self.max_script_executions > 0 else 0,
        ]
        return any(r >= threshold for r in ratios)

    def consume_turn(self) -> None:
        """消耗一个 turn"""
        self.turns_used += 1

    def consume_tool_call(self) -> None:
        """消耗一次工具调用"""
        self.tool_calls_used += 1

    def consume_script_execution(self) -> None:
        """消耗一次脚本执行"""
        self.script_executions_used += 1

    def consume_context_tokens(self, tokens: int) -> None:
        """消耗上下文 token"""
        self.context_tokens_used += tokens

    def to_dict(self) -> Dict[str, Any]:
        """序列化为字典"""
        return {
            "max_turns": self.max_turns,
            "max_tool_calls": self.max_tool_calls,
            "max_script_executions": self.max_script_executions,
            "max_context_tokens": self.max_context_tokens,
            "turns_used": self.turns_used,
            "tool_calls_used": self.tool_calls_used,
            "script_executions_used": self.script_executions_used,
            "context_tokens_used": self.context_tokens_used,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ToolBudget":
        return cls(
            max_turns=data.get("max_turns", 12),
            max_tool_calls=data.get("max_tool_calls", 30),
            max_script_executions=data.get("max_script_executions", 10),
            max_context_tokens=data.get("max_context_tokens", 100000),
            turns_used=data.get("turns_used", 0),
            tool_calls_used=data.get("tool_calls_used", 0),
            script_executions_used=data.get("script_executions_used", 0),
            context_tokens_used=data.get("context_tokens_used", 0),
        )


@dataclass
class Observation:
    """观察结果"""
    action_type: str
    success: bool
    output: str
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    turn: int = 0
    timestamp: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> Dict[str, Any]:
        """序列化为字典"""
        return {
            "action_type": self.action_type,
            "success": self.success,
            "output": self.output,
            "error": self.error,
            "metadata": self.metadata,
            "turn": self.turn,
            "timestamp": self.timestamp.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Observation":
        return cls(
            action_type=data["action_type"],
            success=data["success"],
            output=data["output"],
            error=data.get("error"),
            metadata=data.get("metadata", {}),
            turn=data.get("turn", 0),
            timestamp=datetime.fromisoformat(data["timestamp"]) if "timestamp" in data else datetime.now(),
        )


class RunStatus(Enum):
    """运行状态枚举"""
    INITIALIZING = "initializing"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused"


@dataclass
class RunState:
    """Agent 运行状态"""
    run_id: str
    request: str
    status: RunStatus = RunStatus.INITIALIZING

    # 技能相关
    skill_index: List[Any] = field(default_factory=list)   # List[SkillMetadata]
    loaded_skills: Dict[str, Any] = field(default_factory=dict)  # Dict[str, LoadedSkill]

    # 规划与执行
    plan: Optional[Any] = None  # Optional[Plan]
    budget: ToolBudget = field(default_factory=ToolBudget)
    observations: List[Observation] = field(default_factory=list)

    # 轮次追踪
    current_turn: int = 0

    # 上下文管理
    context_tokens_estimate: int = 0

    # 错误信息
    error: Optional[str] = None
    error_trace: Optional[str] = None

    # 元数据
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)

    def add_observation(self, observation: Observation) -> None:
        """添加观察结果并更新时间戳"""
        self.observations.append(observation)
        self.updated_at = datetime.now()

    def estimate_context_tokens(self, text: str) -> int:
        """粗略估算文本的 token 数（字符数 / 4）"""
        return len(text) // 4

    def to_dict(self) -> Dict[str, Any]:
        """序列化为字典（用于持久化）"""
        return {
            "run_id": self.run_id,
            "request": self.request,
            "status": self.status.value,
            "current_turn": self.current_turn,
            "budget": self.budget.to_dict(),
            "error": self.error,
            "error_trace": self.error_trace,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "loaded_skills": list(self.loaded_skills.keys()),
            "observations_count": len(self.observations),
        }
