"""预定义 workflow.

调用方:
    workflow = get_workflow("research_to_draft")
    rec = await orchestrator.run_workflow(workflow, inputs={...})
"""
from __future__ import annotations

import json
from typing import Dict, List

from .orchestrator import (
    CriticLoopStep,
    SequentialStep,
    Workflow,
)
from .types import AgentTask, RunContext


# =====================================================================
# research_to_draft — 5 agent 全流程
# =====================================================================

def _build_scout_task(ctx: RunContext) -> AgentTask:
    keyword = ctx.inputs.get("keyword") or ctx.inputs.get("topic") or ""
    top_n = int(ctx.inputs.get("top_n", 3))
    return AgentTask(
        user_prompt=f"""请用工具去小红书搜「{keyword}」, 选出 likedCount 最高的 {top_n} 篇图文笔记 (note type 不是 video), 并拉详情, 最后给 final 输出.

提示:
- search_feeds 返回的 JSON 结构: {{"feeds": [{{"id": "...", "xsecToken": "...", "noteCard": {{"displayTitle": "...", "interactInfo": {{"likedCount": "..."}}}}}}, ...]}}
- 选取时跳过 noteCard.type == "video"
- 调 get_feed_detail 时, feed_id 用 feeds[i].id, xsec_token 用 feeds[i].xsecToken
- 详情返回的结构: {{"data": {{"note": {{"desc": "...", "imageList": [...], "interactInfo": {{...}}}}, "comments": {{"list": [{{"content": "..."}}]}}}}}}
""",
        inputs={"keyword": keyword, "top_n": top_n},
    )


def _build_strategist_task(ctx: RunContext) -> AgentTask:
    pack = ctx.state.get("research_pack") or {}
    picks = pack.get("picks") or []
    topic = ctx.inputs.get("topic") or ctx.inputs.get("keyword") or ""
    subject = ctx.inputs.get("subject") or ""
    angle = ctx.inputs.get("angle") or "soft_dry_goods"
    return AgentTask(
        user_prompt=(
            f"请基于侦察兵带回的 {len(picks)} 篇高赞笔记, 反向工程出爆款 Brief.\n"
            f"主题: 「{topic}」(科目: {subject or '通用'}, 内容方向: {angle})"
        ),
        inputs={
            "topic": topic,
            "subject": subject,
            "angle": angle,
            "picks": picks,
        },
    )


def _build_writer_task(ctx: RunContext) -> AgentTask:
    brief = ctx.state.get("brief") or {}
    topic = ctx.inputs.get("topic") or ctx.inputs.get("keyword") or ""
    subject = ctx.inputs.get("subject") or ""
    angle = ctx.inputs.get("angle") or "soft_dry_goods"
    extra = ctx.inputs.get("extra_instructions") or ""
    return AgentTask(
        user_prompt=(
            f"请基于下面的 Brief 写一篇小红书草稿. 主题: 「{topic}」(科目: {subject}, 方向: {angle}).\n"
            + (f"额外指示: {extra}\n" if extra else "")
        ),
        inputs={"brief": brief, "topic": topic, "subject": subject, "angle": angle},
    )


def _build_critic_task(ctx: RunContext) -> AgentTask:
    draft = ctx.state.get("draft") or {}
    return AgentTask(
        user_prompt="请审下面这份草稿. 严格按 A/B/C 红线 + D/E 必查项审, 输出 verdict.",
        inputs={"draft": draft, "iteration": ctx.state.get("_critic_iteration", 1)},
    )


def _build_reviser_task(ctx: RunContext) -> AgentTask:
    draft = ctx.state.get("draft") or {}
    critic = ctx.state.get("critic_report") or {}
    return AgentTask(
        user_prompt="请按 critic_report 里的 issues 逐条修订下面这份原稿, 不要漏改, 不要改无问题的部分.",
        inputs={"original_draft": draft, "critic_report": critic},
    )


def _build_cover_designer_task(ctx: RunContext) -> AgentTask:
    draft = ctx.state.get("draft") or {}
    # 用 run_id 作为 draft_id 命名空间 (app.py 入队时会把图片信息合并到正式 draft)
    draft_id = ctx.run_id
    title = (draft.get("title") or "").strip()
    cover_concept = (draft.get("cover_concept") or "").strip()
    content_excerpt = (draft.get("content") or "")[:600]
    tags = draft.get("tags") or []
    return AgentTask(
        user_prompt=(
            f"请为这份草稿生成 1 张封面 (3:4) + 2 张正文配图 (1:1). draft_id 必须用「{draft_id}」.\n"
            f"title: {title}\ncover_concept: {cover_concept}\ntags: {tags}\n"
            f"正文摘要 (前 600 字):\n{content_excerpt}\n\n"
            "你必须并发调 3 次 image.generate (一次 tool_calls 列表里 3 个), 不要串行."
        ),
        inputs={
            "draft_id": draft_id,
            "title": title,
            "cover_concept": cover_concept,
            "tags": tags,
            "content_excerpt": content_excerpt,
        },
    )


RESEARCH_TO_DRAFT = Workflow(
    id="research_to_draft",
    name="研究→策略→撰写→审稿→修订→封面",
    description="完整 6-agent 自动出稿+出图流程, Critic 自纠最多 3 轮; 封面失败不阻断",
    steps=[
        SequentialStep(
            id="scout",
            agent_id="trend_scout",
            build_task=_build_scout_task,
            save_as="research_pack",
        ),
        SequentialStep(
            id="strategist",
            agent_id="strategist",
            build_task=_build_strategist_task,
            save_as="brief",
        ),
        CriticLoopStep(
            id="write_critic_loop",
            writer_agent_id="writer",
            critic_agent_id="critic",
            reviser_agent_id="reviser",
            build_writer_task=_build_writer_task,
            build_reviser_task=_build_reviser_task,
            build_critic_task=_build_critic_task,
            save_draft_as="draft",
            save_critic_as="critic_report",
            max_iterations=3,
        ),
        SequentialStep(
            id="cover",
            agent_id="cover_designer",
            build_task=_build_cover_designer_task,
            save_as="images",
            optional=True,
        ),
    ],
)


# =====================================================================
# quick_draft — 已有 brief 直接走 writer + critic loop
# =====================================================================

QUICK_DRAFT = Workflow(
    id="quick_draft",
    name="快速出稿 (已有 Brief) + 封面",
    description="跳过研究和策略, 用已有 brief 直接写 + 审 + 修, 自动出 1 封面+2 正文图",
    steps=[
        CriticLoopStep(
            id="write_critic_loop",
            writer_agent_id="writer",
            critic_agent_id="critic",
            reviser_agent_id="reviser",
            build_writer_task=_build_writer_task,
            build_reviser_task=_build_reviser_task,
            build_critic_task=_build_critic_task,
            save_draft_as="draft",
            save_critic_as="critic_report",
            max_iterations=3,
        ),
        SequentialStep(
            id="cover",
            agent_id="cover_designer",
            build_task=_build_cover_designer_task,
            save_as="images",
            optional=True,
        ),
    ],
)


# =====================================================================
# rewrite — 已有 draft, 仅过 critic + reviser
# =====================================================================

def _passthrough_writer(ctx: RunContext) -> AgentTask:
    """rewrite 模式下"writer"是 noop: 把已有 draft 直接当起稿输出."""
    return AgentTask(
        user_prompt=(
            "下面是已有的 draft, 你不需要重写, 只需要把它原样以 final action 输出 (output 必须是 draft JSON, 字段不变)."
        ),
        inputs={"draft": ctx.inputs.get("draft") or {}},
    )


REWRITE = Workflow(
    id="rewrite",
    name="重审 + 修订 (已有 Draft)",
    description="拿现成 draft 过一遍 Critic 自纠环",
    steps=[
        SequentialStep(
            id="seed_draft",
            agent_id="writer",
            build_task=_passthrough_writer,
            save_as="draft",
        ),
        # 单独跑 critic + reviser 一轮
        CriticLoopStep(
            id="critic_only",
            writer_agent_id="writer",
            critic_agent_id="critic",
            reviser_agent_id="reviser",
            # 第 1 轮"writer"再吐一遍 (passthrough)
            build_writer_task=_passthrough_writer,
            build_reviser_task=_build_reviser_task,
            build_critic_task=_build_critic_task,
            save_draft_as="draft",
            save_critic_as="critic_report",
            max_iterations=3,
        ),
    ],
)


WORKFLOWS: Dict[str, Workflow] = {
    "research_to_draft": RESEARCH_TO_DRAFT,
    "quick_draft": QUICK_DRAFT,
    "rewrite": REWRITE,
}


def get_workflow(wid: str) -> Workflow:
    if wid not in WORKFLOWS:
        raise KeyError(f"未知 workflow: {wid}; 可选: {list(WORKFLOWS)}")
    return WORKFLOWS[wid]


def list_workflows() -> List[Dict]:
    return [
        {"id": w.id, "name": w.name, "description": w.description, "n_steps": len(w.steps)}
        for w in WORKFLOWS.values()
    ]
