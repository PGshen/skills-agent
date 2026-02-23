import pytest
from src.agent.plan import Plan, PlanStep, StepStatus, CircularDependencyError


# ──────────────────────────────────────────
# PlanStep 序列化 / 反序列化
# ──────────────────────────────────────────

def test_plan_step_serialization():
    """测试步骤序列化"""
    step = PlanStep(id="s1", title="Test Step")
    data = step.to_dict()
    restored = PlanStep.from_dict(data)
    assert restored.id == step.id
    assert restored.title == step.title
    assert restored.status == step.status
    assert restored.dependencies == step.dependencies
    assert restored.reason == step.reason
    assert restored.started_at == step.started_at
    assert restored.completed_at == step.completed_at


def test_plan_step_serialization_with_all_fields():
    """测试带完整字段的步骤序列化"""
    step = PlanStep(
        id="s2",
        title="Full Step",
        status=StepStatus.COMPLETED,
        reason="Just because",
        dependencies=["s1"],
        started_at="2024-01-01T00:00:00",
        completed_at="2024-01-01T01:00:00",
    )
    data = step.to_dict()
    restored = PlanStep.from_dict(data)
    assert restored.id == "s2"
    assert restored.status == StepStatus.COMPLETED
    assert restored.reason == "Just because"
    assert restored.dependencies == ["s1"]
    assert restored.started_at == "2024-01-01T00:00:00"
    assert restored.completed_at == "2024-01-01T01:00:00"


def test_plan_step_from_dict_defaults():
    """测试 from_dict 缺省值"""
    data = {"id": "s1", "title": "Minimal"}
    step = PlanStep.from_dict(data)
    assert step.status == StepStatus.PENDING
    assert step.dependencies == []
    assert step.reason is None


# ──────────────────────────────────────────
# Plan 序列化 / 反序列化
# ──────────────────────────────────────────

def test_plan_serialization():
    """测试 Plan 序列化"""
    plan = Plan(
        goal="Test goal",
        steps=[
            PlanStep(id="s1", title="Step 1"),
            PlanStep(id="s2", title="Step 2", dependencies=["s1"]),
        ],
        assumptions=["Assumption A"],
        constraints={"max_time": 60},
        version=2,
    )
    data = plan.to_dict()
    restored = Plan.from_dict(data)
    assert restored.goal == plan.goal
    assert len(restored.steps) == 2
    assert restored.steps[0].id == "s1"
    assert restored.steps[1].dependencies == ["s1"]
    assert restored.assumptions == ["Assumption A"]
    assert restored.constraints == {"max_time": 60}
    assert restored.version == 2


def test_plan_from_dict_defaults():
    """测试 Plan from_dict 缺省值"""
    data = {"goal": "Simple", "steps": []}
    plan = Plan.from_dict(data)
    assert plan.assumptions == []
    assert plan.constraints == {}
    assert plan.version == 1


# ──────────────────────────────────────────
# update_step_status
# ──────────────────────────────────────────

def test_update_step_status_success():
    """测试成功更新步骤状态"""
    plan = Plan(goal="Test", steps=[PlanStep(id="s1", title="Step 1")])
    result = plan.update_step_status("s1", StepStatus.COMPLETED)
    assert result is True
    assert plan.steps[0].status == StepStatus.COMPLETED


def test_update_step_status_not_found():
    """测试更新不存在的步骤返回 False"""
    plan = Plan(goal="Test", steps=[PlanStep(id="s1", title="Step 1")])
    result = plan.update_step_status("nonexistent", StepStatus.COMPLETED)
    assert result is False


# ──────────────────────────────────────────
# get_next_pending_step
# ──────────────────────────────────────────

def test_plan_next_step_with_dependencies():
    """测试依赖处理"""
    plan = Plan(
        goal="Test",
        steps=[
            PlanStep(id="s1", title="First"),
            PlanStep(id="s2", title="Second", dependencies=["s1"]),
        ],
    )

    # s1 未完成，应返回 s1
    next_step = plan.get_next_pending_step()
    assert next_step is not None
    assert next_step.id == "s1"

    # s1 完成后，应返回 s2
    plan.update_step_status("s1", StepStatus.COMPLETED)
    next_step = plan.get_next_pending_step()
    assert next_step is not None
    assert next_step.id == "s2"


def test_get_next_pending_step_all_completed():
    """所有步骤完成后返回 None"""
    plan = Plan(
        goal="Test",
        steps=[PlanStep(id="s1", title="Step 1", status=StepStatus.COMPLETED)],
    )
    assert plan.get_next_pending_step() is None


def test_get_next_pending_step_empty():
    """空步骤列表返回 None"""
    plan = Plan(goal="Test", steps=[])
    assert plan.get_next_pending_step() is None


def test_get_next_pending_step_skips_non_pending():
    """非 PENDING 步骤被跳过"""
    plan = Plan(
        goal="Test",
        steps=[
            PlanStep(id="s1", title="In Progress", status=StepStatus.IN_PROGRESS),
            PlanStep(id="s2", title="Pending"),
        ],
    )
    next_step = plan.get_next_pending_step()
    assert next_step is not None
    assert next_step.id == "s2"


# ──────────────────────────────────────────
# is_step_completed
# ──────────────────────────────────────────

def test_is_step_completed_true():
    plan = Plan(
        goal="Test",
        steps=[PlanStep(id="s1", title="Done", status=StepStatus.COMPLETED)],
    )
    assert plan.is_step_completed("s1") is True


def test_is_step_completed_false():
    plan = Plan(
        goal="Test",
        steps=[PlanStep(id="s1", title="Not done")],
    )
    assert plan.is_step_completed("s1") is False


def test_is_step_completed_missing():
    plan = Plan(goal="Test", steps=[])
    assert plan.is_step_completed("unknown") is False


# ──────────────────────────────────────────
# 循环依赖检测
# ──────────────────────────────────────────

def test_circular_dependency_raises():
    """检测到循环依赖时抛出异常"""
    plan = Plan(
        goal="Test",
        steps=[
            PlanStep(id="s1", title="A", dependencies=["s2"]),
            PlanStep(id="s2", title="B", dependencies=["s1"]),
        ],
    )
    with pytest.raises(CircularDependencyError):
        plan.get_next_pending_step()


def test_self_dependency_raises():
    """步骤依赖自身时抛出异常"""
    plan = Plan(
        goal="Test",
        steps=[PlanStep(id="s1", title="Self", dependencies=["s1"])],
    )
    with pytest.raises(CircularDependencyError):
        plan.get_next_pending_step()


def test_no_circular_dependency():
    """无循环依赖时正常工作"""
    plan = Plan(
        goal="Test",
        steps=[
            PlanStep(id="s1", title="A"),
            PlanStep(id="s2", title="B", dependencies=["s1"]),
            PlanStep(id="s3", title="C", dependencies=["s2"]),
        ],
    )
    next_step = plan.get_next_pending_step()
    assert next_step is not None
    assert next_step.id == "s1"


# ──────────────────────────────────────────
# get_progress_summary
# ──────────────────────────────────────────

def test_get_progress_summary_empty():
    """空计划摘要"""
    plan = Plan(goal="Test", steps=[])
    summary = plan.get_progress_summary()
    assert summary["total"] == 0
    assert summary["progress_pct"] == 0.0


def test_get_progress_summary_mixed():
    """混合状态的进度摘要"""
    plan = Plan(
        goal="Test",
        steps=[
            PlanStep(id="s1", title="Done", status=StepStatus.COMPLETED),
            PlanStep(id="s2", title="Failed", status=StepStatus.FAILED),
            PlanStep(id="s3", title="Skipped", status=StepStatus.SKIPPED),
            PlanStep(id="s4", title="Running", status=StepStatus.IN_PROGRESS),
            PlanStep(id="s5", title="Waiting"),
        ],
    )
    summary = plan.get_progress_summary()
    assert summary["total"] == 5
    assert summary["completed"] == 1
    assert summary["failed"] == 1
    assert summary["skipped"] == 1
    assert summary["in_progress"] == 1
    assert summary["pending"] == 1
    # done = completed + failed + skipped = 3 / 5 = 60%
    assert summary["progress_pct"] == 60.0


def test_get_progress_summary_all_completed():
    """全部完成时进度 100%"""
    plan = Plan(
        goal="Test",
        steps=[
            PlanStep(id="s1", title="S1", status=StepStatus.COMPLETED),
            PlanStep(id="s2", title="S2", status=StepStatus.COMPLETED),
        ],
    )
    summary = plan.get_progress_summary()
    assert summary["progress_pct"] == 100.0


# ──────────────────────────────────────────
# detect_deadlock
# ──────────────────────────────────────────

def test_detect_deadlock_no_deadlock():
    """无死锁情况"""
    plan = Plan(
        goal="Test",
        steps=[
            PlanStep(id="s1", title="Runnable"),
            PlanStep(id="s2", title="Depends", dependencies=["s1"]),
        ],
    )
    # s1 没有未满足的依赖，不是卡死状态
    assert plan.detect_deadlock(window=2) is False


def test_detect_deadlock_detected():
    """连续步骤依赖已失败步骤，触发死锁检测"""
    plan = Plan(
        goal="Test",
        steps=[
            PlanStep(id="s0", title="Root", status=StepStatus.FAILED),
            PlanStep(id="s1", title="Blocked 1", dependencies=["s0"]),
            PlanStep(id="s2", title="Blocked 2", dependencies=["s0"]),
            PlanStep(id="s3", title="Blocked 3", dependencies=["s0"]),
        ],
    )
    assert plan.detect_deadlock(window=3) is True


def test_detect_deadlock_window_not_reached():
    """连续卡死步骤数未达到阈值"""
    plan = Plan(
        goal="Test",
        steps=[
            PlanStep(id="s0", title="Root", status=StepStatus.FAILED),
            PlanStep(id="s1", title="Blocked 1", dependencies=["s0"]),
            PlanStep(id="s2", title="Blocked 2", dependencies=["s0"]),
        ],
    )
    # window=3, 只有 2 个卡死步骤
    assert plan.detect_deadlock(window=3) is False


def test_detect_deadlock_empty_plan():
    """空计划不死锁"""
    plan = Plan(goal="Test", steps=[])
    assert plan.detect_deadlock() is False


def test_detect_deadlock_zero_window():
    """window=0 始终不报死锁"""
    plan = Plan(
        goal="Test",
        steps=[
            PlanStep(id="s0", title="Root", status=StepStatus.FAILED),
            PlanStep(id="s1", title="Blocked", dependencies=["s0"]),
        ],
    )
    assert plan.detect_deadlock(window=0) is False
