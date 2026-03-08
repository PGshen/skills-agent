# Tools Runtime 设计

**状态**：Phase B 完整实现（Phase A 不执行真实脚本）

---

## 1. 设计目标

Tools Runtime 是"可控执行层"，把 Agent Core 的结构化动作（`run_script`）转换为受控的本地操作：

| 职责 | 说明 |
|------|------|
| 权限校验 | 三方合并（全局配置 ∩ 技能声明 ∩ 运行时策略） |
| 路径安全 | 脚本必须在技能目录内（realpath 前缀校验） |
| 受控执行 | 超时 / 工作目录隔离 / 环境变量清理 / 输出截断 |
| 审批机制 | 交互式（CLI 用户确认）/ 非交互式（CI 自动拒绝）|
| 审计落盘 | 执行结果写入 EventLogger |

**不在此处理**：
- 模型决策与 Plan 更新（Agent Core 负责）
- 技能发现与正文加载（Skill Registry / Skill Loader 负责）
- 结构化 Action 解析（Model Adapter 负责）

---

## 2. 工具集合与风险分级

### 2.1 MVP 工具集

| 工具 | 风险等级 | 是否需要审批 |
|------|----------|-------------|
| `read_file` | low（只读） | 否 |
| `list_dir` | low（只读） | 否 |
| `grep` | low（只读） | 否 |
| `run_script` | medium（本地执行） | 默认需要 |

### 2.2 高风险工具（默认关闭）

| 工具 | 风险等级 | 说明 |
|------|----------|------|
| `write_file` | high | 可能覆盖重要文件 |
| `delete_file` | high | 不可逆删除 |
| `network_request` | high | 数据外泄风险 |

高风险工具即使开启也必须强制审批，不允许在 run 级别自动授权。

---

## 3. 权限模型（三方合并）

### 3.1 合并规则

```
最终允许工具集 = 全局配置允许集 ∩ 技能声明集（若存在） ∩ 运行时策略
```

```python
# src/tools/permissions.py
from ..skills.metadata import SkillMetadata

class PermissionChecker:
    def __init__(self, global_allowed_tools: list[str]):
        self._global = set(global_allowed_tools)

    def check(self, tool_name: str, skill_meta: SkillMetadata) -> bool:
        """
        校验工具是否被允许。
        返回 True 表示允许执行（仍可能需要审批）。
        返回 False 表示直接拒绝（ToolNotAllowed）。
        """
        # 全局配置必须允许
        if tool_name not in self._global:
            return False

        # 技能有 allowed_tools 声明时进一步收紧
        if skill_meta.allowed_tools:
            if tool_name not in skill_meta.allowed_tools:
                return False

        return True

    def requires_approval(self, tool_name: str) -> bool:
        """medium 及以上风险等级的工具需要审批。"""
        return tool_name in {"run_script", "write_file", "delete_file", "network_request"}
```

### 3.2 授权粒度

| 粒度 | 说明 | 推荐场景 |
|------|------|----------|
| 单次调用 | 每次调用单独审批 | 高风险操作 |
| 本次 run | 同工具在同一 session 内复用授权 | run_script（开发场景）|
| 全局 | 不需要审批 | read_file 等只读工具 |

---

## 4. 审批机制

```python
# src/tools/approval.py
from enum import Enum

class ApprovalScope(str, Enum):
    ONCE = "once"           # 仅本次调用
    RUN = "run"             # 本次 run 内
    ALWAYS = "always"       # 不再询问（全局）

class ApprovalRequest:
    def __init__(self, tool: str, risk: str, params: dict, skill_name: str):
        self.tool = tool
        self.risk = risk
        self.params = params
        self.skill_name = skill_name

class ApprovalManager:
    """
    交互式：向用户展示审批请求，等待 y/n 输入。
    非交互式（CI）：自动拒绝所有需审批的工具。
    """

    def __init__(self, interactive: bool = True):
        self._interactive = interactive
        self._run_approvals: set[str] = set()  # 本次 run 已授权的 tool

    def request(self, req: ApprovalRequest) -> bool:
        """返回 True 表示用户批准，False 表示拒绝。"""
        # 已在本次 run 授权
        if req.tool in self._run_approvals:
            return True

        if not self._interactive:
            return False  # CI 模式：自动拒绝

        # 向终端展示审批请求
        print(f"\n[Approval Required]")
        print(f"  Tool:   {req.tool}")
        print(f"  Risk:   {req.risk}")
        print(f"  Skill:  {req.skill_name}")
        print(f"  Params: {req.params}")
        answer = input("Allow? [y/N/run(allow for this run)] ").strip().lower()

        if answer == "y":
            return True
        elif answer == "run":
            self._run_approvals.add(req.tool)
            return True
        else:
            return False
```

---

## 5. ToolsRuntime 接口

```python
# src/tools/runtime.py
from pathlib import Path
from ..skills.metadata import SkillMetadata
from ..agent.events import EventLogger, EventType
from .permissions import PermissionChecker
from .approval import ApprovalManager, ApprovalRequest
from .executor import ScriptExecutor, ReadFileExecutor, ListDirExecutor, GrepExecutor

class ToolsRuntime:
    """
    工具运行时主类。
    Agent Core 通过此类执行所有工具操作。
    """

    def __init__(
        self,
        event_logger: EventLogger,
        global_allowed_tools: list[str] = None,
        interactive: bool = True,
    ):
        self._logger = event_logger
        self._permission = PermissionChecker(
            global_allowed_tools or ["read_file", "list_dir", "grep", "run_script"]
        )
        self._approval = ApprovalManager(interactive=interactive)
        self._script_executor = ScriptExecutor()

    def run_script(
        self,
        skill_meta: SkillMetadata,
        script: str,
        args: list[str] = None,
    ) -> dict:
        """
        执行技能脚本。
        返回 {"exit_code": int, "stdout": str, "stderr": str, "timeout": bool}
        """
        # 1. 权限校验
        if not self._permission.check("run_script", skill_meta):
            raise ToolNotAllowedError(f"run_script not allowed for skill '{skill_meta.name}'")

        # 2. 审批
        req = ApprovalRequest(
            tool="run_script",
            risk="medium",
            params={"script": script, "args": args},
            skill_name=skill_meta.name,
        )
        if self._permission.requires_approval("run_script"):
            if not self._approval.request(req):
                raise ApprovalDeniedError("User denied script execution approval")

        # 3. 路径校验
        skill_dir = Path(skill_meta.skill_path).parent
        script_path = self._resolve_script_path(skill_dir, script)

        # 4. 执行
        self._logger.emit(EventType.SCRIPT_STARTED, {
            "skill": skill_meta.name, "script": script, "args": args
        })

        result = self._script_executor.run(
            script_path=script_path,
            args=args or [],
            timeout=skill_meta.resource_limits.max_script_time_sec,
            cwd=str(skill_dir),
        )

        self._logger.emit(EventType.SCRIPT_COMPLETED, {
            "exit_code": result["exit_code"],
            "stdout_chars": len(result["stdout"]),
            "timeout": result["timeout"],
        })

        return result

    def read_file(self, path: str, max_bytes: int = 100_000) -> dict:
        """读取文件内容（只读，无需审批）。"""
        return ReadFileExecutor().run(Path(path), max_bytes=max_bytes)

    def list_dir(self, path: str, max_entries: int = 100) -> dict:
        """列出目录内容（只读，无需审批）。"""
        return ListDirExecutor().run(Path(path), max_entries=max_entries)

    def grep(self, pattern: str, path: str, max_results: int = 50) -> dict:
        """文本搜索（只读，无需审批）。"""
        return GrepExecutor().run(pattern=pattern, root=Path(path), max_results=max_results)

    def _resolve_script_path(self, skill_dir: Path, script: str) -> Path:
        """校验脚本路径在技能目录内。"""
        if ".." in script or script.startswith("/"):
            raise PathTraversalError(f"Invalid script path: {script!r}")

        resolved = (skill_dir / script).resolve()
        if not str(resolved).startswith(str(skill_dir.resolve())):
            raise PathTraversalError(f"Script path escapes skill directory: {script!r}")

        if not resolved.exists():
            raise ScriptNotFoundError(f"Script not found: {script}")

        return resolved
```

---

## 6. ScriptExecutor（受控执行）

```python
# src/tools/executor.py
import subprocess
from pathlib import Path

# 输出截断阈值
MAX_OUTPUT_CHARS = 10_000

class ScriptExecutor:
    """
    受控执行脚本（subprocess，非 shell=True）。
    约束：超时 / 输出截断 / 禁止 shell 注入 / 环境变量清理。
    """

    # 最小白名单环境变量
    _ENV_WHITELIST = {"PATH", "HOME", "USER", "LANG", "LC_ALL"}

    def run(
        self,
        script_path: Path,
        args: list[str],
        timeout: int = 30,
        cwd: str = None,
    ) -> dict:
        """
        执行脚本，返回：
        {"exit_code": int, "stdout": str, "stderr": str, "timeout": bool}
        """
        # 仅保留白名单环境变量
        import os
        clean_env = {k: v for k, v in os.environ.items() if k in self._ENV_WHITELIST}

        cmd = [str(script_path)] + [str(a) for a in args]

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=cwd,
                env=clean_env,
                # 禁止 shell=True，避免注入
            )
            stdout = proc.stdout[:MAX_OUTPUT_CHARS]
            stderr = proc.stderr[:MAX_OUTPUT_CHARS]
            return {
                "exit_code": proc.returncode,
                "stdout": stdout,
                "stderr": stderr,
                "timeout": False,
            }
        except subprocess.TimeoutExpired:
            return {
                "exit_code": -1,
                "stdout": "",
                "stderr": f"Script timed out after {timeout}s",
                "timeout": True,
            }
```

---

## 7. 执行结果格式（供 Agent Core 注入上下文）

成功执行：
```
[Script: scripts/fill.py | skill=pdf-form-filler]
exit_code=0
stdout:
...（最多 10000 字符）...
```

执行失败：
```
[Script: scripts/fill.py | skill=pdf-form-filler]
exit_code=1  stderr: "ModuleNotFoundError: No module named 'pdfplumber'"
stdout: (empty)
```

超时：
```
[Script: scripts/fill.py | skill=pdf-form-filler]
timeout=True (limit=30s)
stdout: (empty)
```

---

## 8. 错误类型

```python
class ToolsRuntimeError(Exception): pass
class ToolNotAllowedError(ToolsRuntimeError): pass
class ApprovalDeniedError(ToolsRuntimeError): pass
class PathTraversalError(ToolsRuntimeError): pass
class ScriptNotFoundError(ToolsRuntimeError): pass
```

| 错误 | Agent Core 处理 |
|------|-----------------|
| `ToolNotAllowedError` | 返回 error observation，模型选择只读替代方案 |
| `ApprovalDeniedError` | 返回 "User denied" observation，模型更新 Plan |
| `PathTraversalError` | 返回 error observation，模型修正路径 |
| `ScriptNotFoundError` | 返回 error observation，模型检查技能结构 |
| `exit_code != 0` | 返回 exit_code + stderr，模型走失败分支 |
| `timeout=True` | 返回 timeout observation，模型更新 Plan |

---

## 9. 验收标准

### Phase B

- [ ] `run_script` 执行测试脚本（`echo hello`），返回 `exit_code=0, stdout="hello\n"`
- [ ] 超时测试：`sleep 60` 在 5s timeout 后返回 `timeout=True`
- [ ] 路径越界：`script="../secret.py"` 抛出 `PathTraversalError`
- [ ] 权限拒绝：技能 `allowed_tools=["read_file"]` 时 `run_script` 抛出 `ToolNotAllowedError`
- [ ] 非交互模式（CI）：需审批的工具自动返回 `ApprovalDeniedError`
- [ ] 执行结果超 10000 字符时截断，不崩溃

---

## 10. 设计权衡说明

| 决策点 | 选择 | 替代方案 | 选择理由 |
|--------|------|----------|----------|
| 脚本执行方式 | `subprocess.run(shell=False)` | `shell=True` | 防止命令注入；直接 exec 无 shell 介入 |
| OS 级沙箱 | 不引入 | Docker / seccomp | MVP 阶段复杂度过高；路径约束 + 超时已覆盖核心风险 |
| 环境变量 | 白名单清理 | 全量继承 | 防止脚本意外读取 ANTHROPIC_API_KEY 等敏感变量 |
| 输出截断 | 截断至 10000 chars | 全量落盘 | 上下文注入有大小限制；完整输出可另行落盘 |
| 审批粒度 | 单次 or run 级 | 全局授权 | 全局授权风险过高；run 级覆盖开发场景的便利需求 |
