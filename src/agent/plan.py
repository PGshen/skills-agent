from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional
from enum import Enum


class StepStatus(Enum):
    """步骤状态"""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class PlanStep:
    """规划步骤"""
    id: str
    title: str
    status: StepStatus = StepStatus.PENDING
    reason: Optional[str] = None
    dependencies: List[str] = field(default_factory=list)
    started_at: Optional[str] = None
    completed_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status.value,
            "reason": self.reason,
            "dependencies": self.dependencies,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PlanStep":
        return cls(
            id=data["id"],
            title=data["title"],
            status=StepStatus(data.get("status", "pending")),
            reason=data.get("reason"),
            dependencies=data.get("dependencies", []),
            started_at=data.get("started_at"),
            completed_at=data.get("completed_at"),
        )


class CircularDependencyError(Exception):
    """循环依赖异常"""
    pass


@dataclass
class Plan:
    """Agent 规划"""
    goal: str
    steps: List[PlanStep]
    assumptions: List[str] = field(default_factory=list)
    constraints: Dict[str, Any] = field(default_factory=dict)
    version: int = 1

    def to_dict(self) -> Dict[str, Any]:
        return {
            "goal": self.goal,
            "steps": [step.to_dict() for step in self.steps],
            "assumptions": self.assumptions,
            "constraints": self.constraints,
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Plan":
        return cls(
            goal=data["goal"],
            steps=[PlanStep.from_dict(s) for s in data["steps"]],
            assumptions=data.get("assumptions", []),
            constraints=data.get("constraints", {}),
            version=data.get("version", 1),
        )

    def update_step_status(self, step_id: str, status: StepStatus) -> bool:
        """更新步骤状态"""
        for step in self.steps:
            if step.id == step_id:
                step.status = status
                return True
        return False

    def get_next_pending_step(self) -> Optional[PlanStep]:
        """获取下一个待执行步骤（考虑依赖），若存在循环依赖则抛出异常"""
        self._check_circular_dependencies()
        for step in self.steps:
            if step.status != StepStatus.PENDING:
                continue
            dependencies_met = all(
                self.is_step_completed(dep_id)
                for dep_id in step.dependencies
            )
            if dependencies_met:
                return step
        return None

    def is_step_completed(self, step_id: str) -> bool:
        """检查步骤是否已完成"""
        for step in self.steps:
            if step.id == step_id:
                return step.status == StepStatus.COMPLETED
        return False

    def _check_circular_dependencies(self) -> None:
        """检测循环依赖，若存在则抛出 CircularDependencyError"""
        step_map = {step.id: step for step in self.steps}
        visited: set = set()
        in_stack: set = set()

        def dfs(step_id: str) -> None:
            if step_id in in_stack:
                raise CircularDependencyError(
                    f"Circular dependency detected involving step: {step_id}"
                )
            if step_id in visited:
                return
            visited.add(step_id)
            in_stack.add(step_id)
            step = step_map.get(step_id)
            if step:
                for dep_id in step.dependencies:
                    dfs(dep_id)
            in_stack.discard(step_id)

        for step in self.steps:
            dfs(step.id)

    def get_progress_summary(self) -> Dict[str, Any]:
        """返回计划执行进度摘要"""
        total = len(self.steps)
        counts: Dict[str, int] = {status.value: 0 for status in StepStatus}
        for step in self.steps:
            counts[step.status.value] += 1

        completed = counts[StepStatus.COMPLETED.value]
        failed = counts[StepStatus.FAILED.value]
        skipped = counts[StepStatus.SKIPPED.value]
        done = completed + failed + skipped

        return {
            "total": total,
            "completed": completed,
            "failed": failed,
            "skipped": skipped,
            "pending": counts[StepStatus.PENDING.value],
            "in_progress": counts[StepStatus.IN_PROGRESS.value],
            "progress_pct": round(done / total * 100, 1) if total > 0 else 0.0,
        }

    def detect_deadlock(self, window: int = 3) -> bool:
        """检测连续 N 个步骤无进展（均为 PENDING 且依赖未满足）

        Args:
            window: 连续无进展步骤数阈值，默认 3

        Returns:
            True 表示检测到死锁（无进展），False 表示正常
        """
        if window <= 0:
            return False

        step_map = {step.id: step for step in self.steps}
        consecutive = 0

        for step in self.steps:
            if step.status != StepStatus.PENDING:
                consecutive = 0
                continue
            # 检查依赖是否存在未完成且已失败/跳过的情况（无法推进）
            blocked = any(
                step_map.get(dep_id) is not None
                and step_map[dep_id].status in (StepStatus.FAILED, StepStatus.SKIPPED)
                for dep_id in step.dependencies
            )
            # 依赖未满足（未完成且非已完成）
            deps_not_met = any(
                not self.is_step_completed(dep_id)
                for dep_id in step.dependencies
            )
            if deps_not_met or blocked:
                consecutive += 1
            else:
                consecutive = 0

            if consecutive >= window:
                return True

        return False
