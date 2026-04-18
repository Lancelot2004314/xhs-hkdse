"""Orchestrator — 编排 agent 跑 workflow.

核心:
- EventBus: 跨 run 的进程内 pubsub, 给 SSE / 历史回放用
- RunRecord: 一次 workflow 执行的完整快照 (events + state)
- Orchestrator.run_workflow(): 根据 workflow.steps 调度 agent
   - SequentialStep: 串行
   - ParallelStep: 并行 (一个 step 内多个 agent 同时跑)
   - CriticLoopStep: writer → critic → 不过则 reviser → 再 critic, 最多 N 轮
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .agent import Agent
from .types import (
    AgentEvent,
    AgentResult,
    AgentSpec,
    AgentTask,
    EventCallback,
    EventType,
    RunContext,
)

logger = logging.getLogger(__name__)


# =====================================================================
# Workflow 步骤定义 (data classes, 不需要序列化, workflows.py 里手写)
# =====================================================================

@dataclass
class StepBase:
    id: str


@dataclass
class SequentialStep(StepBase):
    """单 agent 串行 step.

    build_task: 接 (ctx) -> AgentTask. 上一步的输出在 ctx.state[<id>] 里.
    save_as: 该 agent 输出存到 ctx.state[save_as]
    optional: True 时该 step 失败不会让整个 workflow 失败 (仅 emit 一条 LOG)
    """
    agent_id: str
    build_task: Any  # Callable[[RunContext], AgentTask]
    save_as: str
    optional: bool = False


@dataclass
class ParallelStep(StepBase):
    """同时跑多个 agent (用 list of (agent_id, build_task, save_as))."""
    branches: List[tuple]  # [(agent_id, build_task, save_as)]


@dataclass
class CriticLoopStep(StepBase):
    """Writer → Critic 自纠环.

    writer_agent_id: 第一轮跑的 agent (例: writer)
    reviser_agent_id: 第 2+ 轮跑的 agent (例: reviser, 也可与 writer 同)
    critic_agent_id:  审稿 agent (例: critic)
    build_writer_task / build_reviser_task / build_critic_task: 函数
    save_draft_as / save_critic_as: 保存键
    max_iterations: 最多审几轮 (含第 1 次)
    """
    writer_agent_id: str
    critic_agent_id: str
    reviser_agent_id: str
    build_writer_task: Any
    build_reviser_task: Any
    build_critic_task: Any
    save_draft_as: str
    save_critic_as: str
    max_iterations: int = 3


@dataclass
class Workflow:
    id: str
    name: str
    description: str
    steps: List[Any]


# =====================================================================
# EventBus — 进程内 pubsub. 单 run 单 queue, SSE 端点订阅
# =====================================================================

class EventBus:
    """简单进程内 pubsub. 每个 run_id 持有一个 deque (历史) + 多个 asyncio.Queue (订阅者)."""

    def __init__(self, max_history: int = 500):
        self._history: Dict[str, deque] = defaultdict(lambda: deque(maxlen=max_history))
        self._subscribers: Dict[str, List[asyncio.Queue]] = defaultdict(list)
        self._counters: Dict[str, itertools.count] = defaultdict(lambda: itertools.count(1))
        self._lock = asyncio.Lock()

    def next_seq(self, run_id: str) -> int:
        return next(self._counters[run_id])

    def emit_sync(self, ev: AgentEvent) -> None:
        """同步发事件 (供 agent.run 的回调用)."""
        self._history[ev.run_id].append(ev)
        for q in list(self._subscribers.get(ev.run_id, [])):
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                logger.warning(f"event queue full for run {ev.run_id}, dropping")

    def history(self, run_id: str) -> List[AgentEvent]:
        return list(self._history.get(run_id, []))

    async def subscribe(self, run_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        # 把已有 history 灌进去
        for ev in self.history(run_id):
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                break
        self._subscribers[run_id].append(q)
        return q

    def unsubscribe(self, run_id: str, q: asyncio.Queue) -> None:
        if run_id in self._subscribers and q in self._subscribers[run_id]:
            self._subscribers[run_id].remove(q)


# =====================================================================
# RunRecord — 一次 workflow 跑完的总结 (供 history 查询)
# =====================================================================

@dataclass
class RunRecord:
    run_id: str
    workflow_id: str
    status: str = "running"   # running / completed / failed
    started_at: float = field(default_factory=time.time)
    ended_at: Optional[float] = None
    inputs: Dict[str, Any] = field(default_factory=dict)
    state: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "workflow_id": self.workflow_id,
            "status": self.status,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "elapsed_sec": (self.ended_at - self.started_at) if self.ended_at else None,
            "inputs": self.inputs,
            "state_keys": list(self.state.keys()),
            "error": self.error,
        }


# =====================================================================
# Orchestrator
# =====================================================================

class Orchestrator:
    """组装 agents (按 spec) 并跑 workflow."""

    def __init__(
        self,
        specs: List[AgentSpec],
        registry,
        llm_api_key: str,
        llm_base_url: str,
        default_model: str,
        brand_prefix: str = "",
        event_bus: Optional[EventBus] = None,
    ):
        self.registry = registry
        self.event_bus = event_bus or EventBus()
        self._records: Dict[str, RunRecord] = {}
        self.agents: Dict[str, Agent] = {}
        for spec in specs:
            if not spec.enabled:
                continue
            self.agents[spec.id] = Agent(
                spec=spec,
                registry=registry,
                llm_api_key=llm_api_key,
                llm_base_url=llm_base_url,
                default_model=default_model,
                prefix_system=brand_prefix,
            )

    def get_record(self, run_id: str) -> Optional[RunRecord]:
        return self._records.get(run_id)

    def list_records(self, limit: int = 50) -> List[RunRecord]:
        return sorted(self._records.values(), key=lambda r: r.started_at, reverse=True)[:limit]

    def prepare_run(self, workflow: Workflow, inputs: Dict[str, Any]) -> "RunContext":
        """提前分配 run_id (供 batch 端点同步获取 id)."""
        ctx = RunContext(workflow=workflow.id, inputs=dict(inputs))
        rec = RunRecord(run_id=ctx.run_id, workflow_id=workflow.id, inputs=dict(inputs))
        self._records[ctx.run_id] = rec
        return ctx

    async def run_workflow(
        self,
        workflow: Workflow,
        inputs: Dict[str, Any],
        ctx: Optional["RunContext"] = None,
    ) -> RunRecord:
        if ctx is None:
            ctx = RunContext(workflow=workflow.id, inputs=dict(inputs))
            rec = RunRecord(run_id=ctx.run_id, workflow_id=workflow.id, inputs=dict(inputs))
            self._records[ctx.run_id] = rec
        else:
            rec = self._records.get(ctx.run_id)
            if rec is None:
                rec = RunRecord(run_id=ctx.run_id, workflow_id=workflow.id, inputs=dict(inputs))
                self._records[ctx.run_id] = rec

        bus = self.event_bus
        emit = bus.emit_sync
        seq = lambda: bus.next_seq(ctx.run_id)

        emit(AgentEvent(
            run_id=ctx.run_id, seq=seq(), type=EventType.RUN_STARTED,
            summary=f"🚀 启动 workflow: {workflow.name}",
            data={"inputs": inputs, "workflow": workflow.id},
        ))

        try:
            for step in workflow.steps:
                await self._run_step(step, ctx, emit, seq)
            rec.status = "completed"
            rec.state = dict(ctx.state)
            emit(AgentEvent(
                run_id=ctx.run_id, seq=seq(), type=EventType.RUN_COMPLETED,
                summary="✅ workflow 完成",
                data={"state_keys": list(ctx.state.keys())},
            ))
        except Exception as e:
            logger.exception("workflow 执行失败")
            rec.status = "failed"
            rec.error = str(e)
            rec.state = dict(ctx.state)
            emit(AgentEvent(
                run_id=ctx.run_id, seq=seq(), type=EventType.RUN_FAILED,
                summary=f"❌ workflow 失败: {e}",
            ))
        finally:
            rec.ended_at = time.time()

        return rec

    # ------------------------------------------------------------------
    # step dispatchers
    # ------------------------------------------------------------------

    async def _run_step(self, step: Any, ctx: RunContext, emit, seq):
        if isinstance(step, SequentialStep):
            await self._run_sequential(step, ctx, emit, seq)
        elif isinstance(step, ParallelStep):
            await self._run_parallel(step, ctx, emit, seq)
        elif isinstance(step, CriticLoopStep):
            await self._run_critic_loop(step, ctx, emit, seq)
        else:
            raise ValueError(f"未知 step 类型: {type(step)}")

    async def _run_sequential(self, step: SequentialStep, ctx: RunContext, emit, seq):
        agent = self.agents.get(step.agent_id)
        if not agent:
            if step.optional:
                emit(AgentEvent(
                    run_id=ctx.run_id, seq=seq(), type=EventType.LOG,
                    step_id=step.id,
                    summary=f"⚠️ optional step {step.id} 跳过: agent {step.agent_id} 未注册/禁用",
                ))
                return
            raise ValueError(f"agent 未注册或被禁用: {step.agent_id}")
        try:
            task: AgentTask = step.build_task(ctx)
            result = await agent.run(task, ctx, emit=emit, step_id=step.id, seq_counter=seq)
        except Exception as e:
            if step.optional:
                emit(AgentEvent(
                    run_id=ctx.run_id, seq=seq(), type=EventType.LOG,
                    step_id=step.id,
                    summary=f"⚠️ optional step {step.id} 出错但继续: {e}",
                ))
                return
            raise
        if not result.ok:
            if step.optional:
                emit(AgentEvent(
                    run_id=ctx.run_id, seq=seq(), type=EventType.LOG,
                    step_id=step.id,
                    summary=f"⚠️ optional step {step.id} 失败但继续: {result.error}",
                ))
                return
            raise RuntimeError(f"step {step.id} ({step.agent_id}) 失败: {result.error}")
        ctx.state[step.save_as] = result.output
        ctx.state.setdefault("_results", {})[step.id] = result.model_dump()

    async def _run_parallel(self, step: ParallelStep, ctx: RunContext, emit, seq):
        async def _one(agent_id, build_task, save_as):
            agent = self.agents.get(agent_id)
            if not agent:
                raise ValueError(f"agent 未注册: {agent_id}")
            task = build_task(ctx)
            r = await agent.run(task, ctx, emit=emit, step_id=step.id, seq_counter=seq)
            return save_as, r

        results = await asyncio.gather(*(_one(*b) for b in step.branches), return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):
                raise r
            save_as, ar = r
            if not ar.ok:
                raise RuntimeError(f"parallel branch ({save_as}) 失败: {ar.error}")
            ctx.state[save_as] = ar.output

    async def _run_critic_loop(self, step: CriticLoopStep, ctx: RunContext, emit, seq):
        writer = self.agents[step.writer_agent_id]
        critic = self.agents[step.critic_agent_id]
        reviser = self.agents.get(step.reviser_agent_id) or writer

        # 第 1 轮: writer
        emit(AgentEvent(
            run_id=ctx.run_id, seq=seq(), type=EventType.LOG,
            agent_id=step.writer_agent_id,
            summary=f"📝 第 1 轮: {writer.spec.name} 起稿",
            step_id=step.id,
        ))
        wt: AgentTask = step.build_writer_task(ctx)
        wt.iteration = 0
        wr = await writer.run(wt, ctx, emit=emit, step_id=step.id, seq_counter=seq)
        if not wr.ok:
            raise RuntimeError(f"writer 起稿失败: {wr.error}")
        ctx.state[step.save_draft_as] = wr.output

        for it in range(1, step.max_iterations):
            ct: AgentTask = step.build_critic_task(ctx)
            ct.iteration = it
            cr = await critic.run(ct, ctx, emit=emit, step_id=step.id, seq_counter=seq)
            if not cr.ok:
                raise RuntimeError(f"critic 审稿失败: {cr.error}")
            ctx.state[step.save_critic_as] = cr.output

            verdict = cr.output if isinstance(cr.output, dict) else {}
            passed = bool(verdict.get("passed"))
            n_issues = len(verdict.get("issues") or [])
            emit(AgentEvent(
                run_id=ctx.run_id, seq=seq(), type=EventType.CRITIC_VERDICT,
                agent_id=step.critic_agent_id, agent_name=critic.spec.name,
                step_id=step.id, iteration=it,
                summary=f"🧐 Critic 第 {it} 轮: {'✅ PASS' if passed else f'❌ FAIL ({n_issues} 个 issue)'}",
                data={"passed": passed, "n_issues": n_issues, "verdict": verdict},
            ))
            if passed:
                self._backfill_citations(ctx, step, emit, seq)
                return

            # 失败 → reviser
            if it >= step.max_iterations - 1:
                emit(AgentEvent(
                    run_id=ctx.run_id, seq=seq(), type=EventType.LOG,
                    summary=f"⚠️ 已达最大修订轮次 ({step.max_iterations}), 仍未 PASS, 进 review 队列",
                    step_id=step.id, iteration=it,
                ))
                self._backfill_citations(ctx, step, emit, seq)
                return
            emit(AgentEvent(
                run_id=ctx.run_id, seq=seq(), type=EventType.REVISION_TRIGGERED,
                agent_id=step.reviser_agent_id, agent_name=reviser.spec.name,
                step_id=step.id, iteration=it,
                summary=f"🔁 第 {it+1} 轮: {reviser.spec.name} 按 Critic 修订",
            ))
            rt: AgentTask = step.build_reviser_task(ctx)
            rt.iteration = it
            rr = await reviser.run(rt, ctx, emit=emit, step_id=step.id, seq_counter=seq)
            if not rr.ok:
                raise RuntimeError(f"reviser 修订失败: {rr.error}")
            # 修订器输出可能比 writer 多个 changes_made 字段, 统一存
            new_draft = rr.output if isinstance(rr.output, dict) else {}
            ctx.state[step.save_draft_as] = new_draft

    # ------------------------------------------------------------------
    # post-process: 自动回填 [source: ] 占位
    # ------------------------------------------------------------------

    def _backfill_citations(self, ctx: RunContext, step: CriticLoopStep, emit, seq) -> None:
        """把 critic_report.fact_sources_found 的 url 回填到 draft.content 的 [source: ] 占位.

        策略:
        - 优先按 key_phrase 在该行的字面匹配 (最长前缀)
        - 没匹配上的占位按顺序消费剩余 url (确保不漏)
        - 仍未填的高亮交给前端
        """
        draft = ctx.state.get(step.save_draft_as)
        critic_report = ctx.state.get(step.save_critic_as) or {}
        if not isinstance(draft, dict):
            return
        sources = critic_report.get("fact_sources_found") or {}
        # 兼容 list[{key, url}] 或 list[str]
        if isinstance(sources, list):
            tmp: Dict[str, str] = {}
            for i, item in enumerate(sources):
                if isinstance(item, dict):
                    k = str(item.get("key") or item.get("phrase") or item.get("claim") or f"_anon_{i}")
                    u = str(item.get("url") or item.get("source") or "")
                    if u:
                        tmp[k] = u
                elif isinstance(item, str) and item.startswith("http"):
                    tmp[f"_anon_{i}"] = item
            sources = tmp
        if not isinstance(sources, dict) or not sources:
            return

        content = draft.get("content") or ""
        if "[source:" not in content:
            return

        # 候选池: (key_phrase, url)
        pool = [(str(k or ""), str(v or "")) for k, v in sources.items() if v]
        used_urls: set = set()
        filled = 0
        unfilled = 0

        lines = content.split("\n")
        for i, line in enumerate(lines):
            if "[source: ]" not in line and "[source:]" not in line:
                continue
            chosen_url = None
            chosen_key = None
            line_lc = line.lower()
            # 1) 按 key_phrase 子串匹配
            best_len = 0
            for key, url in pool:
                if not key or url in used_urls:
                    continue
                kk = key.strip()
                if not kk:
                    continue
                # 用前 6 个非空字符做指纹
                fp = "".join(ch for ch in kk if not ch.isspace())[:6].lower()
                if fp and fp in line_lc and len(kk) > best_len:
                    chosen_url, chosen_key = url, key
                    best_len = len(kk)
            # 2) fallback: 拿任意未使用的 url
            if not chosen_url:
                for key, url in pool:
                    if url not in used_urls:
                        chosen_url, chosen_key = url, key
                        break
            if chosen_url:
                lines[i] = line.replace("[source: ]", f"[source: {chosen_url}]", 1) \
                               .replace("[source:]", f"[source: {chosen_url}]", 1)
                used_urls.add(chosen_url)
                filled += 1
            else:
                # 标记需要人工补 (前端识别这个标记并高亮)
                lines[i] = line.replace("[source: ]", "[source: TODO_MANUAL]", 1) \
                               .replace("[source:]", "[source: TODO_MANUAL]", 1)
                unfilled += 1

        draft["content"] = "\n".join(lines)
        cite = draft.setdefault("fact_citations", {})
        for k, v in sources.items():
            cite[k] = v
        ctx.state[step.save_draft_as] = draft

        if filled or unfilled:
            emit(AgentEvent(
                run_id=ctx.run_id, seq=seq(), type=EventType.LOG,
                step_id=step.id,
                summary=f"🔗 自动回填 source: 成功 {filled} 条" + (f", 待人工 {unfilled} 条" if unfilled else ""),
                data={"filled": filled, "unfilled": unfilled, "n_sources": len(pool)},
            ))
