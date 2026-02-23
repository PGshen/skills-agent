import pytest
from datetime import datetime
from src.agent.state import ToolBudget, Observation, RunStatus, RunState


# ──────────────────────────────────────────
# ToolBudget
# ──────────────────────────────────────────

def test_budget_defaults():
    budget = ToolBudget()
    assert budget.max_turns == 12
    assert budget.max_tool_calls == 30
    assert budget.max_script_executions == 10
    assert budget.turns_used == 0


def test_budget_can_continue_true():
    budget = ToolBudget(max_turns=5, max_tool_calls=10)
    assert budget.can_continue() is True


def test_budget_consumption():
    """测试预算消耗"""
    budget = ToolBudget(max_turns=5, max_tool_calls=10)
    assert budget.can_continue() is True

    for _ in range(5):
        budget.turns_used += 1

    assert budget.can_continue() is False


def test_budget_consume_turn():
    budget = ToolBudget(max_turns=3)
    budget.consume_turn()
    budget.consume_turn()
    assert budget.turns_used == 2
    assert budget.can_continue() is True
    budget.consume_turn()
    assert budget.can_continue() is False


def test_budget_consume_tool_call():
    budget = ToolBudget(max_tool_calls=2)
    budget.consume_tool_call()
    assert budget.tool_calls_used == 1
    assert budget.can_continue() is True
    budget.consume_tool_call()
    assert budget.can_continue() is False


def test_budget_consume_script_execution():
    budget = ToolBudget(max_script_executions=1)
    budget.consume_script_execution()
    assert budget.script_executions_used == 1
    assert budget.can_continue() is False


def test_budget_consume_context_tokens():
    budget = ToolBudget()
    budget.consume_context_tokens(500)
    budget.consume_context_tokens(300)
    assert budget.context_tokens_used == 800


def test_budget_near_limit():
    """测试接近限制检测"""
    budget = ToolBudget(max_turns=10)
    budget.turns_used = 8

    assert budget.is_near_limit(threshold=0.8) is True
    assert budget.is_near_limit(threshold=0.9) is False


def test_budget_near_limit_not_reached():
    budget = ToolBudget(max_turns=10, max_tool_calls=10, max_script_executions=10)
    budget.turns_used = 5
    assert budget.is_near_limit(threshold=0.8) is False


def test_budget_near_limit_tool_calls():
    budget = ToolBudget(max_turns=10, max_tool_calls=10)
    budget.tool_calls_used = 9
    assert budget.is_near_limit(threshold=0.8) is True


def test_budget_serialization():
    budget = ToolBudget(max_turns=5, max_tool_calls=20)
    budget.consume_turn()
    budget.consume_tool_call()
    data = budget.to_dict()
    restored = ToolBudget.from_dict(data)
    assert restored.max_turns == 5
    assert restored.max_tool_calls == 20
    assert restored.turns_used == 1
    assert restored.tool_calls_used == 1


def test_budget_from_dict_defaults():
    restored = ToolBudget.from_dict({})
    assert restored.max_turns == 12
    assert restored.turns_used == 0


# ──────────────────────────────────────────
# Observation
# ──────────────────────────────────────────

def test_observation_has_timestamp():
    obs = Observation(action_type="run_script", success=True, output="ok")
    assert isinstance(obs.timestamp, datetime)


def test_observation_to_dict():
    obs = Observation(
        action_type="read_file",
        success=True,
        output="file content",
        error=None,
        metadata={"path": "/tmp/f.txt"},
        turn=2,
    )
    data = obs.to_dict()
    assert data["action_type"] == "read_file"
    assert data["success"] is True
    assert data["output"] == "file content"
    assert data["turn"] == 2
    assert "timestamp" in data
    assert data["metadata"] == {"path": "/tmp/f.txt"}


def test_observation_with_error():
    obs = Observation(action_type="run_script", success=False, output="", error="timeout")
    data = obs.to_dict()
    assert data["error"] == "timeout"


def test_observation_serialization_roundtrip():
    obs = Observation(
        action_type="grep",
        success=True,
        output="match found",
        turn=3,
        metadata={"count": 1},
    )
    data = obs.to_dict()
    restored = Observation.from_dict(data)
    assert restored.action_type == obs.action_type
    assert restored.success == obs.success
    assert restored.output == obs.output
    assert restored.turn == obs.turn
    assert restored.metadata == obs.metadata
    assert isinstance(restored.timestamp, datetime)


def test_observation_defaults():
    obs = Observation(action_type="list_dir", success=True, output=".")
    assert obs.turn == 0
    assert obs.error is None
    assert obs.metadata == {}


# ──────────────────────────────────────────
# RunStatus
# ──────────────────────────────────────────

def test_run_status_values():
    assert RunStatus.INITIALIZING.value == "initializing"
    assert RunStatus.RUNNING.value == "running"
    assert RunStatus.COMPLETED.value == "completed"
    assert RunStatus.FAILED.value == "failed"
    assert RunStatus.PAUSED.value == "paused"


# ──────────────────────────────────────────
# RunState
# ──────────────────────────────────────────

def test_run_state_serialization():
    """测试运行状态序列化"""
    state = RunState(run_id="test-123", request="Do something")
    data = state.to_dict()

    assert data["run_id"] == "test-123"
    assert data["request"] == "Do something"
    assert "created_at" in data


def test_run_state_defaults():
    state = RunState(run_id="r1", request="hello")
    assert state.status == RunStatus.INITIALIZING
    assert state.current_turn == 0
    assert state.observations == []
    assert state.plan is None
    assert state.error is None


def test_run_state_to_dict_structure():
    state = RunState(run_id="r1", request="test")
    data = state.to_dict()
    assert data["status"] == "initializing"
    assert data["current_turn"] == 0
    assert data["observations_count"] == 0
    assert data["loaded_skills"] == []
    assert "budget" in data
    assert "updated_at" in data


def test_run_state_add_observation():
    state = RunState(run_id="r1", request="test")
    obs = Observation(action_type="read_file", success=True, output="data")
    state.add_observation(obs)
    assert len(state.observations) == 1
    assert state.observations[0].action_type == "read_file"


def test_run_state_add_observation_updates_timestamp():
    state = RunState(run_id="r1", request="test")
    before = state.updated_at
    obs = Observation(action_type="grep", success=True, output="hit")
    state.add_observation(obs)
    assert state.updated_at >= before


def test_run_state_add_multiple_observations():
    state = RunState(run_id="r1", request="test")
    for i in range(3):
        state.add_observation(
            Observation(action_type="tool", success=True, output=str(i), turn=i)
        )
    assert len(state.observations) == 3
    data = state.to_dict()
    assert data["observations_count"] == 3


def test_run_state_estimate_context_tokens():
    state = RunState(run_id="r1", request="test")
    tokens = state.estimate_context_tokens("a" * 400)
    assert tokens == 100


def test_run_state_estimate_context_tokens_empty():
    state = RunState(run_id="r1", request="test")
    assert state.estimate_context_tokens("") == 0


def test_run_state_estimate_context_tokens_rounding():
    state = RunState(run_id="r1", request="test")
    # 10 chars → 10//4 = 2
    assert state.estimate_context_tokens("a" * 10) == 2


def test_run_state_loaded_skills_in_dict():
    state = RunState(run_id="r1", request="test")
    state.loaded_skills["skill-a"] = object()
    state.loaded_skills["skill-b"] = object()
    data = state.to_dict()
    assert set(data["loaded_skills"]) == {"skill-a", "skill-b"}


def test_run_state_error_fields():
    state = RunState(run_id="r1", request="test", error="oops", error_trace="traceback...")
    data = state.to_dict()
    assert data["error"] == "oops"
    assert data["error_trace"] == "traceback..."
