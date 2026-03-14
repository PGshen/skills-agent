"""Plan, Step, Action data structures."""

import time
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class StepStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"


class Step(BaseModel):
    id: str                         # 唯一标识，如 "step-1"
    description: str                # 子目标描述
    status: StepStatus = StepStatus.PENDING
    notes: Optional[str] = None     # 执行过程中的补充说明


class Plan(BaseModel):
    goal: str                       # 整体任务目标（来自用户输入）
    steps: list[Step] = Field(default_factory=list)
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)

    def replace(self, new_plan: "Plan") -> "Plan":
        """全量替换：用新 Plan 覆盖旧 Plan，保留 goal 和 created_at。"""
        return Plan(
            goal=self.goal,
            steps=new_plan.steps,
            created_at=self.created_at,
            updated_at=time.time(),
        )

    def current_step(self) -> Optional[Step]:
        """返回第一个非 done/failed 的 step。"""
        for step in self.steps:
            if step.status not in (StepStatus.DONE, StepStatus.FAILED):
                return step
        return None


class ActionType(str, Enum):
    LOAD_SKILL = "load_skill"        # 触发技能，加载 SKILL.md 正文
    LOAD_RESOURCE = "load_resource"  # 加载技能 resource 文件
    RUN_SCRIPT = "run_script"        # 执行技能脚本
    READ_FILE = "read_file"          # 读取文件内容
    LIST_DIR = "list_dir"            # 列举目录内容
    GREP = "grep"                    # 正则搜索文件
    WRITE_FILE = "write_file"        # 写入文件（需审批）
    DELETE_FILE = "delete_file"      # 删除文件（需审批）
    WEB_SEARCH = "web_search"        # 联网搜索
    UPDATE_PLAN = "update_plan"      # 全量替换当前计划
    FINAL_ANSWER = "final_answer"    # 结束输出最终答案


class Action(BaseModel):
    type: ActionType
    params: dict = Field(default_factory=dict)
    # 常用 params：
    # load_skill:    {"skill_name": "xxx"}
    # load_resource: {"skill_name": "xxx", "resource": "path/to/file"}
    # run_script:    {"skill_name": "xxx", "script": "run.sh", "args": [...]}
    # update_plan:   {"plan": {...}}  # 完整 Plan JSON
    # final_answer:  {"content": "..."}
