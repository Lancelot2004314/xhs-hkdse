"""質心教育 Multi-Agent Framework

参考 Anthropic orchestrator-workers / Claude Code subagents / OpenClaw 模式:
- 每个 Agent = 独立 system prompt + 工具集 + 模型配置
- Orchestrator 编排 Workflow (Critic 自纠环 + 并行/串行步骤)
- EventBus 实时推送 agent 步骤到前端 (SSE)
- 全部 agent 配置可在 agents.yaml 修改, 不动代码
"""

from .types import (
    AgentSpec,
    AgentEvent,
    AgentResult,
    AgentTask,
    RunContext,
    ToolCall,
    ToolResult,
    EventType,
)
from .tools import ToolRegistry, build_default_registry
from .agent import Agent
from .orchestrator import Orchestrator, EventBus, RunRecord
from .workflows import WORKFLOWS, get_workflow
from .config import load_agent_specs, save_agent_specs, DEFAULT_SPECS_PATH

__all__ = [
    "AgentSpec",
    "AgentEvent",
    "AgentResult",
    "AgentTask",
    "RunContext",
    "ToolCall",
    "ToolResult",
    "EventType",
    "ToolRegistry",
    "build_default_registry",
    "Agent",
    "Orchestrator",
    "EventBus",
    "RunRecord",
    "WORKFLOWS",
    "get_workflow",
    "load_agent_specs",
    "save_agent_specs",
    "DEFAULT_SPECS_PATH",
]
