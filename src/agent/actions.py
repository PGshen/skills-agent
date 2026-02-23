from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional, Union
from abc import ABC, abstractmethod


class Action(ABC):
    """动作基类"""

    @abstractmethod
    def action_type(self) -> str:
        """返回动作类型"""
        pass

    @abstractmethod
    def to_dict(self) -> Dict[str, Any]:
        """序列化为字典"""
        pass

    @abstractmethod
    def validate(self) -> bool:
        """验证动作参数"""
        pass


@dataclass
class SkillReference:
    """技能引用"""
    name: str
    source: Optional[str] = None  # project/user/builtin

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "source": self.source}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SkillReference":
        return cls(name=data["name"], source=data.get("source"))


@dataclass
class SelectSkillsAction(Action):
    """选择技能动作"""
    skills: List[SkillReference]
    reason: str

    def action_type(self) -> str:
        return "select_skills"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": "select_skills",
            "skills": [s.to_dict() for s in self.skills],
            "reason": self.reason,
        }

    def validate(self) -> bool:
        return len(self.skills) > 0 and bool(self.reason)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SelectSkillsAction":
        return cls(
            skills=[SkillReference.from_dict(s) for s in data["skills"]],
            reason=data["reason"],
        )


@dataclass
class LoadResourceAction(Action):
    """加载资源动作"""
    skill: SkillReference
    relative_path: str
    section_hint: Optional[str] = None

    def action_type(self) -> str:
        return "load_resource"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": "load_resource",
            "skill": self.skill.to_dict(),
            "relative_path": self.relative_path,
            "section_hint": self.section_hint,
        }

    def validate(self) -> bool:
        return bool(self.relative_path) and ".." not in self.relative_path

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "LoadResourceAction":
        return cls(
            skill=SkillReference.from_dict(data["skill"]),
            relative_path=data["relative_path"],
            section_hint=data.get("section_hint"),
        )


@dataclass
class RunScriptAction(Action):
    """执行脚本动作"""
    skill: SkillReference
    relative_path: str
    args: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)

    def action_type(self) -> str:
        return "run_script"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": "run_script",
            "skill": self.skill.to_dict(),
            "relative_path": self.relative_path,
            "args": self.args,
            "env": self.env,
        }

    def validate(self) -> bool:
        return bool(self.relative_path) and ".." not in self.relative_path

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RunScriptAction":
        return cls(
            skill=SkillReference.from_dict(data["skill"]),
            relative_path=data["relative_path"],
            args=data.get("args", []),
            env=data.get("env", {}),
        )


@dataclass
class FinalAnswerAction(Action):
    """最终答复动作"""
    answer: str
    completed: bool = True

    def action_type(self) -> str:
        return "final_answer"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": "final_answer",
            "answer": self.answer,
            "completed": self.completed,
        }

    def validate(self) -> bool:
        return bool(self.answer)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FinalAnswerAction":
        return cls(
            answer=data["answer"],
            completed=data.get("completed", True),
        )


@dataclass
class PlanUpdateAction(Action):
    """规划更新动作"""
    updates: Dict[str, Any]

    def action_type(self) -> str:
        return "plan_update"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": "plan_update",
            "updates": self.updates,
        }

    def validate(self) -> bool:
        return bool(self.updates)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PlanUpdateAction":
        return cls(updates=data["updates"])


# 联合类型
AgentAction = Union[
    SelectSkillsAction,
    LoadResourceAction,
    RunScriptAction,
    FinalAnswerAction,
    PlanUpdateAction,
]

_ACTION_REGISTRY = {
    "select_skills": SelectSkillsAction,
    "load_resource": LoadResourceAction,
    "run_script": RunScriptAction,
    "final_answer": FinalAnswerAction,
    "plan_update": PlanUpdateAction,
}


def parse_action(data: Dict[str, Any]) -> AgentAction:
    """从字典解析动作"""
    action_type = data.get("action")
    cls = _ACTION_REGISTRY.get(action_type)  # type: ignore[arg-type]
    if cls is None:
        raise ValueError(f"Unknown action type: {action_type}")
    return cls.from_dict(data)
