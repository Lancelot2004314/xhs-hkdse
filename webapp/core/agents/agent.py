"""Agent — 单个 LLM 工人.

执行 loop:
1. 把 spec.system_prompt + task.extra_system 拼成 system message
2. 把 task.user_prompt + inputs 拼成 user message
3. 如果 spec.tools 非空, 在 system 里注入工具描述 + JSON 调用约定
4. 进入 reason-act loop:
   while iteration < max_iterations:
       - 调 LLM
       - 解析输出: 看是 {"tool_calls": [{tool, args}, ...]} 还是 {"final": <output>}
       - tool_calls 走 ToolRegistry, 把结果回灌给 LLM 再调
       - final 退出 loop
5. 返回 AgentResult
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional

import openai

from .types import (
    AgentEvent,
    AgentResult,
    AgentSpec,
    AgentTask,
    EventCallback,
    EventType,
    RunContext,
    ToolCall,
    ToolResult,
)
from .tools import ToolRegistry

logger = logging.getLogger(__name__)


_TOOL_PROTOCOL = """
【工具调用协议 — 严格遵守】

如果你需要调工具, 输出严格 JSON:
{"action": "tool_calls", "calls": [{"id": "tc1", "tool": "<tool_id>", "args": {...}}, ...]}
- 一次可以并发多个 calls (互不依赖时)
- 不要在 JSON 外加任何解释、markdown、注释

如果你已经有最终答案, 输出严格 JSON:
{"action": "final", "output": <你的最终结构化结果>}
- 这里的 <output> 必须严格匹配下面要求的输出 schema
"""


class Agent:
    """单个 agent: 一个 LLM 实例 + 一组工具."""

    def __init__(
        self,
        spec: AgentSpec,
        registry: ToolRegistry,
        llm_api_key: str,
        llm_base_url: str,
        default_model: str,
        prefix_system: str = "",   # brand voice 等全局 system, 在 spec 之前
    ):
        self.spec = spec
        self.registry = registry
        self.llm = openai.OpenAI(api_key=llm_api_key, base_url=llm_base_url)
        self.default_model = default_model
        self.prefix_system = prefix_system

    @property
    def model(self) -> str:
        return self.spec.model or self.default_model

    # ---------------------------------------------------------------
    # public
    # ---------------------------------------------------------------

    async def run(
        self,
        task: AgentTask,
        ctx: RunContext,
        emit: Optional[EventCallback] = None,
        step_id: Optional[str] = None,
        seq_counter: Optional[Any] = None,
    ) -> AgentResult:
        """执行单次任务."""
        t0 = time.time()
        result = AgentResult(agent_id=self.spec.id, ok=False, iterations=0)

        def _emit(et: EventType, summary: str = "", data: Optional[Dict[str, Any]] = None):
            if emit is None:
                return
            seq = seq_counter() if callable(seq_counter) else 0
            emit(AgentEvent(
                run_id=ctx.run_id,
                seq=seq,
                type=et,
                agent_id=self.spec.id,
                agent_name=self.spec.name,
                step_id=step_id,
                summary=summary,
                data=data or {},
                iteration=task.iteration,
            ))

        _emit(EventType.AGENT_STARTED, f"[{self.spec.name}] 开工: {task.user_prompt[:80]}")

        # 拼 system message
        sys_parts: List[str] = []
        if self.prefix_system:
            sys_parts.append(self.prefix_system)
        sys_parts.append(self.spec.system_prompt)
        if task.extra_system:
            sys_parts.append(task.extra_system)
        if self.spec.tools:
            tool_descs = []
            for tid in self.spec.tools:
                t = self.registry.get(tid)
                if t:
                    tool_descs.append(t.describe_for_llm())
            if tool_descs:
                sys_parts.append("\n【可用工具】\n" + "\n\n".join(tool_descs))
                sys_parts.append(_TOOL_PROTOCOL)
        if self.spec.output_must_be_json and not self.spec.tools:
            sys_parts.append(
                "\n【输出协议】 严格 JSON, 不要任何 markdown 包裹, 不要解释."
            )
            if self.spec.output_schema:
                sys_parts.append(
                    "你的输出必须匹配下面的 JSON schema:\n"
                    + json.dumps(self.spec.output_schema, ensure_ascii=False, indent=2)
                )

        system_msg = "\n\n".join(sys_parts)

        # 拼 user message
        user_msg = task.user_prompt
        if task.inputs:
            user_msg += "\n\n【上下文输入 (JSON)】\n" + json.dumps(
                task.inputs, ensure_ascii=False, indent=2, default=_safe_default
            )
        if self.spec.tools and self.spec.output_schema:
            user_msg += (
                "\n\n当你已有最终答案时, "
                f'输出 {{"action":"final","output": <匹配下面 schema 的 JSON>}}, '
                "schema:\n"
                + json.dumps(self.spec.output_schema, ensure_ascii=False, indent=2)
            )

        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_msg},
        ]

        for it in range(self.spec.max_iterations):
            result.iterations = it + 1
            _emit(EventType.LLM_CALL, f"[{self.spec.name}] 调 LLM ({self.model}) 第 {it+1} 轮")
            try:
                resp = await asyncio.to_thread(
                    self.llm.chat.completions.create,
                    model=self.model,
                    messages=messages,
                    temperature=self.spec.temperature,
                    max_tokens=self.spec.max_tokens,
                )
                raw = (resp.choices[0].message.content or "").strip()
            except Exception as e:
                logger.exception("LLM 调用失败")
                result.error = f"LLM 调用失败: {e}"
                _emit(EventType.AGENT_FAILED, str(e))
                result.elapsed_ms = int((time.time() - t0) * 1000)
                return result

            result.raw_text = raw
            _emit(
                EventType.LLM_RESPONSE,
                f"[{self.spec.name}] LLM 返回 {len(raw)} 字符",
                {"preview": raw[:400]},
            )

            # 不带工具的 agent: 直接当最终输出处理
            if not self.spec.tools:
                parsed = _safe_json_loads(raw) if self.spec.output_must_be_json else raw
                if self.spec.output_must_be_json and parsed is None:
                    result.error = "LLM 返回无法解析为 JSON"
                    _emit(EventType.AGENT_FAILED, result.error, {"raw_head": raw[:300]})
                    result.elapsed_ms = int((time.time() - t0) * 1000)
                    return result
                result.output = parsed if parsed is not None else raw
                result.ok = True
                _emit(EventType.AGENT_COMPLETED, f"[{self.spec.name}] 完成")
                result.elapsed_ms = int((time.time() - t0) * 1000)
                return result

            # 带工具的 agent: 解析 action
            parsed = _safe_json_loads(raw) or {}
            action = parsed.get("action")

            if action == "final":
                output = parsed.get("output")
                # output 也可能是字符串里嵌 JSON
                if isinstance(output, str) and self.spec.output_schema:
                    output_parsed = _safe_json_loads(output)
                    if output_parsed is not None:
                        output = output_parsed
                result.output = output
                result.ok = True
                _emit(EventType.AGENT_COMPLETED, f"[{self.spec.name}] 完成 (最终)")
                result.elapsed_ms = int((time.time() - t0) * 1000)
                return result

            if action == "tool_calls":
                calls_raw = parsed.get("calls") or []
                calls = []
                for c in calls_raw:
                    try:
                        calls.append(ToolCall(
                            id=c.get("id") or f"tc-{len(calls)+1}",
                            tool=c.get("tool"),
                            args=c.get("args") or {},
                        ))
                    except Exception as e:
                        logger.warning(f"忽略畸形 tool call: {c} ({e})")
                if not calls:
                    # 没有 calls 又不是 final → 当 final 兜底
                    result.output = raw
                    result.ok = True
                    _emit(EventType.AGENT_COMPLETED, f"[{self.spec.name}] 兜底完成")
                    result.elapsed_ms = int((time.time() - t0) * 1000)
                    return result

                result.tool_calls.extend(calls)
                # 并发调工具
                tasks = [self._run_tool(c, _emit) for c in calls]
                tool_results = await asyncio.gather(*tasks)
                result.tool_results.extend(tool_results)

                # 把 LLM 的回复 + 工具结果回灌
                messages.append({"role": "assistant", "content": raw})
                messages.append({
                    "role": "user",
                    "content": "【工具执行结果】\n"
                    + json.dumps(
                        [tr.model_dump() for tr in tool_results],
                        ensure_ascii=False,
                        indent=2,
                        default=_safe_default,
                    )
                    + "\n\n请基于工具结果继续推进 (再调工具或给 final).",
                })
                continue

            # 未知 action: 当文本输出处理
            logger.warning(f"agent {self.spec.id} 输出无效 action={action}, 当 raw 处理")
            result.output = parsed if parsed else raw
            result.ok = True
            _emit(EventType.AGENT_COMPLETED, f"[{self.spec.name}] 兜底完成")
            result.elapsed_ms = int((time.time() - t0) * 1000)
            return result

        # 跑满 iteration
        result.error = f"超过 max_iterations={self.spec.max_iterations} 仍未 final"
        _emit(EventType.AGENT_FAILED, result.error)
        result.elapsed_ms = int((time.time() - t0) * 1000)
        return result

    async def _run_tool(self, call: ToolCall, emit_fn) -> ToolResult:
        emit_fn(EventType.TOOL_CALL, f"⚙️  调工具 {call.tool}", {"args": call.args, "id": call.id})
        t = time.time()
        try:
            content = await self.registry.invoke(call.tool, call.args)
            elapsed = int((time.time() - t) * 1000)
            tr = ToolResult(
                tool_call_id=call.id, tool=call.tool, ok=True,
                content=_truncate(content, 8000), elapsed_ms=elapsed,
            )
            emit_fn(EventType.TOOL_RESULT, f"✅ {call.tool} {elapsed}ms",
                    {"id": call.id, "preview": str(tr.content)[:300]})
        except Exception as e:
            elapsed = int((time.time() - t) * 1000)
            tr = ToolResult(
                tool_call_id=call.id, tool=call.tool, ok=False,
                content=None, error=str(e), elapsed_ms=elapsed,
            )
            emit_fn(EventType.TOOL_RESULT, f"❌ {call.tool} 失败: {e}", {"id": call.id})
        return tr


# =====================================================================
# 工具函数
# =====================================================================

_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)\s*```", re.IGNORECASE)


def _safe_json_loads(s: str) -> Optional[Any]:
    if not s:
        return None
    s = s.strip()
    try:
        return json.loads(s)
    except Exception:
        pass
    m = _FENCE_RE.search(s)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except Exception:
            s = m.group(1).strip()
    first = s.find("{")
    last = s.rfind("}")
    if first != -1 and last != -1 and last > first:
        try:
            return json.loads(s[first:last + 1])
        except Exception:
            pass
    return None


def _truncate(obj: Any, max_chars: int) -> Any:
    """对超长字符串截断 (LLM 回灌 prompt 时避免爆炸)."""
    if isinstance(obj, str):
        return obj if len(obj) <= max_chars else obj[:max_chars] + f"\n…(截断, 原 {len(obj)} 字符)"
    return obj


def _safe_default(o: Any) -> Any:
    try:
        return str(o)
    except Exception:
        return repr(o)
