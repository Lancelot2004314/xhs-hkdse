"""Agent 框架的核心类型 (data classes / pydantic)."""
from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from pydantic import BaseModel, Field


# =====================================================================
# AgentSpec — 一个 agent 的完整定义 (可序列化为 YAML, 用户可编辑)
# =====================================================================

class AgentSpec(BaseModel):
    """单个 agent 的可配置规格."""
    id: str                       # 内部唯一 id, 例: "trend_scout"
    name: str                     # 中文展示名, 例: "洞察侦察兵"
    role: str                     # 一句话角色描述
    system_prompt: str            # system message (注入 brand voice 后)
    model: Optional[str] = None   # None = 用全局默认模型
    temperature: float = 0.5
    max_tokens: int = 4000
    tools: List[str] = Field(default_factory=list)        # 工具 id 列表
    output_schema: Optional[Dict[str, Any]] = None        # JSON schema 描述 (供 prompt 用)
    output_must_be_json: bool = True                      # 强制 JSON 输出
    max_iterations: int = 5                               # tool-loop 最大轮次
    enabled: bool = True
    notes: str = ""                                       # 给写 spec 的人看的说明


# =====================================================================
# 事件 — Orchestrator 在每个步骤发出, 经 EventBus 推到前端
# =====================================================================

class EventType(str, Enum):
    RUN_STARTED = "run_started"
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"
    AGENT_STARTED = "agent_started"
    AGENT_COMPLETED = "agent_completed"
    AGENT_FAILED = "agent_failed"
    LLM_CALL = "llm_call"
    LLM_RESPONSE = "llm_response"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    CRITIC_VERDICT = "critic_verdict"
    REVISION_TRIGGERED = "revision_triggered"
    LOG = "log"


class AgentEvent(BaseModel):
    run_id: str
    seq: int                                               # 单次 run 内的序号
    ts: str = Field(default_factory=lambda: datetime.now().isoformat())
    type: EventType
    agent_id: Optional[str] = None
    agent_name: Optional[str] = None
    step_id: Optional[str] = None                          # workflow step id
    summary: str = ""                                      # 给 UI 的一句话
    data: Dict[str, Any] = Field(default_factory=dict)     # 详情 payload
    iteration: int = 0                                     # critic 修订第几轮


# =====================================================================
# 工具调用
# =====================================================================

class ToolCall(BaseModel):
    id: str = Field(default_factory=lambda: f"tc-{uuid.uuid4().hex[:8]}")
    tool: str
    args: Dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    tool_call_id: str
    tool: str
    ok: bool
    content: Any                                           # JSON-serializable
    error: Optional[str] = None
    elapsed_ms: int = 0


# =====================================================================
# Agent 任务 + 上下文 + 结果
# =====================================================================

class AgentTask(BaseModel):
    """喂给 Agent.run 的一次任务."""
    user_prompt: str                                       # 主要指令
    inputs: Dict[str, Any] = Field(default_factory=dict)   # 结构化输入 (会拼到 prompt 末尾)
    extra_system: str = ""                                 # 在 spec.system_prompt 之后追加
    iteration: int = 0                                     # 第几轮 (Critic 修订时 >0)


class RunContext(BaseModel):
    """跨 agent 共享的运行上下文 (workflow 范围)."""
    run_id: str = Field(default_factory=lambda: f"run-{uuid.uuid4().hex[:12]}")
    workflow: str = ""
    inputs: Dict[str, Any] = Field(default_factory=dict)   # workflow 入参
    state: Dict[str, Any] = Field(default_factory=dict)    # agent 之间传递的中间产物
    meta: Dict[str, Any] = Field(default_factory=dict)     # tenant_id / user / brand 等

    model_config = {"arbitrary_types_allowed": True}


class AgentResult(BaseModel):
    agent_id: str
    ok: bool
    output: Any = None                                     # 解析后的结构化输出 (优先 JSON)
    raw_text: str = ""                                     # 原始 LLM 文本
    tool_calls: List[ToolCall] = Field(default_factory=list)
    tool_results: List[ToolResult] = Field(default_factory=list)
    iterations: int = 1
    elapsed_ms: int = 0
    error: Optional[str] = None


# =====================================================================
# 回调签名
# =====================================================================

EventCallback = Callable[[AgentEvent], None]
