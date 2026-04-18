"""
質心教育 XHS Studio · 研究层
直接调 xpzouying/xiaohongshu-mcp 的 search_feeds + get_feed_detail,
反向工程小红书爆款笔记结构, 输出可执行的 Brief 与 Draft.

本模块 (核心抽象):
- Pydantic schemas: NoteCard / NoteDetail / Brief / Draft
- XhsResearcher (高层 facade):
    * search_top_notes(keyword, ...)   -> List[NoteCard]
    * fetch_details(picks)             -> List[NoteDetail]
    * synthesize_brief(topic, ...)     -> Brief
    * generate_draft(brief, ...)       -> Draft

所有 LLM 调用强制 JSON 输出 + 質心教育 brand voice system prompt.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

import openai
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# =====================================================================
# Brand Voice — 質心教育科技有限公司
# =====================================================================

BRAND_NAME_FULL = "質心教育科技有限公司"
BRAND_NAME_SHORT = "質心教育"

# 这段 system prompt 会注入到所有 LLM 调用 (synthesize_brief / generate_draft) 顶部.
BRAND_VOICE_SYSTEM_PROMPT = f"""你是 {BRAND_NAME_FULL}({BRAND_NAME_SHORT}) 的小红书内容编辑.

【公司定位】
- 香港 HKDSE 全科补习/升学指导机构
- 全科覆盖: 中文、英文、数学(必修+M1/M2)、通识/公民、Phy/Chem/Bio/BAFS/Eco/ICT/Geog/CHist/WHist/Art/PE 等选修, JUPAS/Non-JUPAS 升学
- 客户: 香港中四到中六学生及家长, 也包括内地希望了解 DSE 路径的学生和家长

【人设(混合)】
- 在内容中提及自己时, 用「質心」/「我们老师」/「我們嘅 senior 學長」等口吻
- 语气: 像高分学长姐 — 真诚、亲切、稍微口语化, 偶尔用 emoji 但不刷屏
- 受众是港中三~中六生 + 家长, 内容用简体中文为主, 涉及考试名称/卷别可保留繁体或英文 (如 Paper 1, JUPAS, NSS)
- 绝不卖弄学术, 不端架子; 但所有提分技巧/数据/趋势必须真实, 不能浮夸

【必须遵守】
1. 不夸大保 5**, 不出现"包过"、"保升 JU"、"百分百拿 5**"等绝对化承诺
2. 不诋毁同行 (机构/补习社/老师), 不点名其他品牌
3. 涉及考试制度、HKEAA 公布的官方数据、大学入学要求时, 必须基于公开权威信息, 用 [source: URL] 标注
4. 涉及个人 (作者本人/老师/学生)的成绩、经历、转学等, 必须明确这是 "Senior 学长经验分享" 而不是质心的官方数据
5. 不涉政、不涉宗教、不涉 LGBTQ 立场
6. 不发布敏感/灰产/医疗保健/未成年违规内容

【内容产品矩阵 (供 Brief 选 angle)】
- 软干货 (优先, 风险低): 学习方法、笔记技巧、心态调节、时间管理、家长沟通、暑期规划、自学路径
- 硬干货 (谨慎, 必须事实可查): 卷别题型解析、评分准则解读、past paper 趋势、JUPAS 选科、放榜攻略
- 学长故事: 真实/化名学长的转 DSE/逆袭/选科心路 (但要注明 "学长经验, 仅供参考")
- 家长侧: 看懂学生成绩、和孩子沟通的 5 个误区、报班怎么挑

【小红书平台调性】
- 标题: 12~22 字, 善用 emoji + 数字 + 反差/钩子, 例: "DSE中文😱我用3個月由Lv2衝上Lv5"
- 正文: 600~1200 字, 段落短小, 每 2~3 行一段, 用 emoji/分隔符做视觉锚点
- tag: 5~10 个, 必含 #DSE / #HKDSE / 科目 tag (#dse中文 / #dse英文 / #hkdse補習 etc.)
- 不可强行植入質心二维码/微信号 (违规), 引导关注用 "私信我哋" "睇主页" 等软引导
"""


# =====================================================================
# Pydantic schemas
# =====================================================================

class NoteCard(BaseModel):
    """搜索结果的一张卡片 (摘要级)."""
    feed_id: str
    xsec_token: str
    title: str = ""
    note_type: str = "normal"  # normal / video
    author: str = ""
    author_id: str = ""
    liked_count: int = 0
    collected_count: int = 0
    comment_count: int = 0
    shared_count: int = 0
    cover_url: str = ""
    raw: Optional[Dict[str, Any]] = None  # 保留原始 feed dict, 调试用


class NoteDetail(BaseModel):
    """笔记完整内容 (含正文 + 图片 + 高赞评论)."""
    feed_id: str
    xsec_token: str = ""
    title: str = ""
    desc: str = ""
    note_type: str = "normal"
    author: str = ""
    author_id: str = ""
    publish_time: Optional[str] = None
    liked_count: int = 0
    collected_count: int = 0
    comment_count: int = 0
    shared_count: int = 0
    images: List[str] = Field(default_factory=list)
    top_comments: List[Dict[str, Any]] = Field(default_factory=list)
    tags_inline: List[str] = Field(default_factory=list)  # desc 里的 #xxx
    word_count: int = 0


class Brief(BaseModel):
    """从 N 篇高赞笔记综合出的研究简报, 给 generate_draft 用."""
    topic: str
    subject: str = ""  # 中文 / 英文 / 数学 / 通识 / JUPAS / 其他
    angle: str = "soft_dry_goods"  # soft_dry_goods / hard_dry_goods / senior_story / parent
    title_patterns: List[str] = Field(default_factory=list)
    hooks: List[str] = Field(default_factory=list)
    structure_outline: str = ""
    recommended_word_count: str = "600-1000"
    recommended_image_count: int = 6
    recommended_tags: List[str] = Field(default_factory=list)
    viral_keywords: List[str] = Field(default_factory=list)
    facts_to_verify: List[str] = Field(default_factory=list)  # 必须查证的硬事实
    selling_points: List[str] = Field(default_factory=list)  # 質心可以借这个 angle 输出的差异化点
    avoid_list: List[str] = Field(default_factory=list)  # 这次必须避开的雷区
    source_note_ids: List[str] = Field(default_factory=list)
    source_titles: List[str] = Field(default_factory=list)
    raw_research_summary: str = ""
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())


class Draft(BaseModel):
    """LLM 生成的草稿, 待审稿/发布."""
    title: str
    content: str
    tags: List[str] = Field(default_factory=list)
    images: List[str] = Field(default_factory=list)  # 占位; v1 由人工补图
    cover_concept: str = ""  # 封面建议文案 (例: "白底 + 红字 '中文5** 拆解' + 学长头像")
    fact_lines: List[int] = Field(default_factory=list)  # content 中需要事实核查的"行号" (从 0 起)
    fact_citations: Dict[str, str] = Field(default_factory=dict)  # claim -> source URL
    brief_snapshot: Optional[Brief] = None
    subject: str = ""
    topic: str = ""
    word_count: int = 0
    notes_for_reviewer: str = ""
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())


# =====================================================================
# 解析工具 — 把 MCP 原始 JSON 抽取成 schema
# =====================================================================

def _to_int(v: Any) -> int:
    """小红书把数字 (含 '1.2万') 用 string 返, 这里尽力转 int."""
    if v is None:
        return 0
    if isinstance(v, int):
        return v
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return 0
        # 1.2万 / 12.3w
        if s.endswith("万") or s.lower().endswith("w"):
            try:
                return int(float(s[:-1]) * 10000)
            except ValueError:
                return 0
        try:
            return int(s.replace(",", ""))
        except ValueError:
            try:
                return int(float(s))
            except ValueError:
                return 0
    return 0


def _parse_search_feeds(raw_text: str) -> List[NoteCard]:
    """search_feeds 返回的 JSON 文本 -> List[NoteCard]."""
    cards: List[NoteCard] = []
    try:
        data = json.loads(raw_text)
    except Exception as e:
        logger.warning(f"search_feeds 返回非 JSON: {e}; head={raw_text[:200]}")
        return cards

    feeds = data.get("feeds") if isinstance(data, dict) else None
    if not feeds and isinstance(data, list):
        feeds = data

    for feed in feeds or []:
        try:
            note = feed.get("noteCard") or {}
            user = note.get("user") or {}
            interact = note.get("interactInfo") or {}
            cover = note.get("cover") or {}
            cards.append(NoteCard(
                feed_id=feed.get("id") or note.get("noteId") or "",
                xsec_token=feed.get("xsecToken") or note.get("xsecToken") or "",
                title=note.get("displayTitle") or note.get("title") or "",
                note_type=note.get("type") or "normal",
                author=user.get("nickname") or user.get("nickName") or "",
                author_id=user.get("userId") or "",
                liked_count=_to_int(interact.get("likedCount")),
                collected_count=_to_int(interact.get("collectedCount")),
                comment_count=_to_int(interact.get("commentCount")),
                shared_count=_to_int(interact.get("sharedCount")),
                cover_url=cover.get("urlDefault") or cover.get("urlPre") or "",
                raw=feed,
            ))
        except Exception as e:
            logger.warning(f"解析单条 feed 失败: {e}")
            continue
    return cards


_INLINE_TAG_RE = re.compile(r"#([^\s#\[]+?)(?:\[话题\])?#")


def _extract_inline_tags(desc: str) -> List[str]:
    return list(dict.fromkeys(t.strip() for t in _INLINE_TAG_RE.findall(desc or "") if t.strip()))


def _parse_get_feed_detail(raw_text: str) -> Optional[NoteDetail]:
    """get_feed_detail 返回的 JSON 文本 -> NoteDetail."""
    try:
        data = json.loads(raw_text)
    except Exception as e:
        logger.warning(f"get_feed_detail 返回非 JSON: {e}")
        return None

    inner = data.get("data") if isinstance(data, dict) else None
    note = (inner or {}).get("note") or {}
    user = note.get("user") or {}
    interact = note.get("interactInfo") or {}
    image_list = note.get("imageList") or []
    comments = ((inner or {}).get("comments") or {}).get("list") or []

    images = []
    for img in image_list:
        u = img.get("urlDefault") or img.get("urlPre")
        if u:
            images.append(u)

    top_comments = []
    for c in comments[:10]:
        top_comments.append({
            "content": c.get("content") or "",
            "like_count": _to_int(c.get("likeCount")),
            "user": (c.get("userInfo") or {}).get("nickname") or "",
        })

    desc = note.get("desc") or ""
    pub_ts = note.get("time")
    publish_time = None
    if pub_ts:
        try:
            publish_time = datetime.fromtimestamp(int(pub_ts) / 1000).isoformat()
        except Exception:
            publish_time = None

    return NoteDetail(
        feed_id=data.get("feed_id") or note.get("noteId") or "",
        xsec_token=note.get("xsecToken") or "",
        title=note.get("title") or "",
        desc=desc,
        note_type=note.get("type") or "normal",
        author=user.get("nickname") or user.get("nickName") or "",
        author_id=user.get("userId") or "",
        publish_time=publish_time,
        liked_count=_to_int(interact.get("likedCount")),
        collected_count=_to_int(interact.get("collectedCount")),
        comment_count=_to_int(interact.get("commentCount")),
        shared_count=_to_int(interact.get("sharedCount")),
        images=images,
        top_comments=top_comments,
        tags_inline=_extract_inline_tags(desc),
        word_count=len(desc),
    )


def _mcp_text(call_tool_result: Any) -> str:
    """从 MCP CallToolResult 抽出第一段文本."""
    if call_tool_result is None:
        return ""
    content = getattr(call_tool_result, "content", None)
    if not content:
        return str(call_tool_result)
    parts = []
    for c in content:
        t = getattr(c, "text", None)
        if t:
            parts.append(t)
    return "\n".join(parts) if parts else str(call_tool_result)


# =====================================================================
# JSON-only LLM 调用工具
# =====================================================================

_JSON_BLOCK_RE = re.compile(r"\{[\s\S]*\}")
_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)\s*```", re.IGNORECASE)


def _safe_json_loads(s: str) -> Optional[Any]:
    """尽力解析 LLM 输出 (可能含 markdown fence / 解释文本) 为 JSON."""
    if not s:
        return None
    s = s.strip()

    # 1) 直接试
    try:
        return json.loads(s)
    except Exception:
        pass

    # 2) 抽 fence 内的内容
    m = _FENCE_RE.search(s)
    if m:
        inner = m.group(1).strip()
        try:
            return json.loads(inner)
        except Exception:
            s = inner  # fallthrough 用 inner 继续找 {}

    # 3) 找第一个 { 到最后一个 } 之间
    first = s.find("{")
    last = s.rfind("}")
    if first != -1 and last != -1 and last > first:
        candidate = s[first:last + 1]
        try:
            return json.loads(candidate)
        except Exception:
            pass

    # 4) 正则贪婪兜底
    m2 = _JSON_BLOCK_RE.search(s)
    if m2:
        try:
            return json.loads(m2.group(0))
        except Exception:
            return None
    return None


# =====================================================================
# XhsResearcher — 主 facade
# =====================================================================

@asynccontextmanager
async def _fresh_xhs_session(xhs_mcp_url: str):
    """每次开一条新的 streamable_http MCP 连接.
    长连接会随时间退化 (xhs server 在做浏览器自动化, 单 search 可能跑 1-2 分钟,
    其间 SSE 流断会污染 session). 一次一连最稳.
    """
    async with streamablehttp_client(xhs_mcp_url) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            yield session


class XhsResearcher:
    """
    研究 facade.

    每次调 xhs MCP 都开一条新的 streamable_http 连接 (而不是用 server_manager 的长连接),
    避免 xhs 浏览器自动化拖死的 SSE stream 污染长会话.

    LLM 通过 openai.OpenAI 直连 (兼容 OpenRouter); model 用 config.default_model.
    """

    def __init__(self, xhs_mcp_url: str, llm_api_key: str, llm_base_url: str, llm_model: str):
        self.xhs_mcp_url = xhs_mcp_url
        self.llm = openai.OpenAI(api_key=llm_api_key, base_url=llm_base_url)
        self.model = llm_model

    # ------- search_top_notes -------
    async def search_top_notes(
        self,
        keyword: str,
        sort_by: str = "最多点赞",  # 客户端排序用; xhs-mcp 自带 filter 触发 500, 我们绕开
        note_type: str = "图文",     # 客户端过滤用
        publish_time: str = "不限",
        top_n: int = 10,
        use_server_filters: bool = False,  # ⚠️ 实测 xhs-mcp 任何 filter 都让浏览器自动化 hang→500, 默认关
    ) -> List[NoteCard]:
        """
        调 xhs MCP search_feeds, 返回前 top_n 张笔记卡片.

        重要: xpzouying/xiaohongshu-mcp 的 `filters` 参数会让 server 端的浏览器
        自动化卡死然后 500. 我们默认不传 filters, 让 server 用「综合」排序拉一批,
        然后在客户端按 `sort_by` 重新排序 + 按 `note_type` 过滤.

        sort_by: 综合 / 最新 / 最多点赞(默认) / 最多评论 / 最多收藏
        note_type: 不限 / 视频 / 图文(默认)
        """
        args: Dict[str, Any] = {"keyword": keyword}
        if use_server_filters:
            filters = {}
            if sort_by:
                filters["sort_by"] = sort_by
            if note_type:
                filters["note_type"] = note_type
            if publish_time and publish_time != "不限":
                filters["publish_time"] = publish_time
            if filters:
                args["filters"] = filters

        logger.info(f"🔍 search_feeds: {args}")
        try:
            async with _fresh_xhs_session(self.xhs_mcp_url) as session:
                result = await asyncio.wait_for(
                    session.call_tool("search_feeds", args),
                    timeout=90,
                )
        except asyncio.TimeoutError:
            logger.error("search_feeds 超时 (90s)")
            return []
        except Exception as e:
            logger.error(f"search_feeds 失败: {e}")
            return []

        text = _mcp_text(result)
        cards = _parse_search_feeds(text)
        logger.info(f"📦 server 返回 {len(cards)} 张原始卡片")

        # 客户端过滤 note_type
        if note_type and note_type != "不限":
            want_video = note_type == "视频"
            cards = [
                c for c in cards
                if (c.note_type == "video") == want_video
                or (c.note_type == "normal" and not want_video)
            ]

        # 客户端排序
        sort_key_map = {
            "最多点赞": lambda c: -c.liked_count,
            "最多评论": lambda c: -c.comment_count,
            "最多收藏": lambda c: -c.collected_count,
        }
        if sort_by in sort_key_map:
            cards.sort(key=sort_key_map[sort_by])

        cards = cards[:top_n]
        logger.info(f"✅ 客户端过滤+排序后 {len(cards)} 张卡片 (sort={sort_by}, type={note_type})")
        return cards

    # ------- fetch_details -------
    async def fetch_details(self, picks: List[NoteCard]) -> List[NoteDetail]:
        """对选中的卡片串行拉详情 (并发会触发 xhs 风控)."""
        details: List[NoteDetail] = []
        for i, card in enumerate(picks):
            if not card.feed_id or not card.xsec_token:
                logger.warning(f"跳过缺 token 的卡片: {card.feed_id}")
                continue
            logger.info(f"📖 [{i+1}/{len(picks)}] get_feed_detail: {card.feed_id} ({card.title[:30]})")
            try:
                async with _fresh_xhs_session(self.xhs_mcp_url) as session:
                    result = await asyncio.wait_for(
                        session.call_tool(
                            "get_feed_detail",
                            {"feed_id": card.feed_id, "xsec_token": card.xsec_token},
                        ),
                        timeout=180,
                    )
                text = _mcp_text(result)
                d = _parse_get_feed_detail(text)
                if d:
                    if not d.feed_id:
                        d.feed_id = card.feed_id
                    if not d.xsec_token:
                        d.xsec_token = card.xsec_token
                    details.append(d)
            except asyncio.TimeoutError:
                logger.warning(f"get_feed_detail 超时: {card.feed_id}")
            except Exception as e:
                logger.warning(f"get_feed_detail 失败: {card.feed_id}: {e}")
            await asyncio.sleep(1.2)  # 节流
        logger.info(f"✅ 拿到 {len(details)} 篇详情")
        return details

    # ------- synthesize_brief -------
    async def synthesize_brief(
        self,
        topic: str,
        subject: str,
        details: List[NoteDetail],
        angle: str = "soft_dry_goods",
    ) -> Brief:
        """LLM 阅读 N 篇详情, 输出结构化 Brief (强制 JSON)."""
        if not details:
            logger.warning("synthesize_brief: 没有 details, 返回空 brief")
            return Brief(topic=topic, subject=subject, angle=angle)

        # 把 details 拍成简短引用块, 避免 prompt 爆掉
        parts = []
        for i, d in enumerate(details, 1):
            desc_clip = (d.desc or "")[:1200]
            parts.append(
                f"\n--- 笔记 {i} ---\n"
                f"feed_id: {d.feed_id}\n"
                f"作者: {d.author}\n"
                f"标题: {d.title}\n"
                f"互动: 赞{d.liked_count} / 藏{d.collected_count} / 评{d.comment_count}\n"
                f"标签: {', '.join(d.tags_inline) or '(无)'}\n"
                f"正文 (节选 ~1200 字):\n{desc_clip}\n"
                f"高赞评论 (前 3):\n" + "\n".join(
                    f"  - 👍{c['like_count']} {c['content'][:120]}" for c in (d.top_comments or [])[:3]
                )
            )
        corpus = "\n".join(parts)

        user_prompt = f"""你正在为 {BRAND_NAME_SHORT} 写一篇小红书笔记, 主题: 「{topic}」(科目: {subject or '通用'}), 内容方向: {angle}.

下面是 {len(details)} 篇 **同主题/相邻主题的高赞小红书笔记** (按点赞排序). 请反向工程出可复用的"爆款配方":

{corpus}

请严格按以下 JSON 格式输出 (只输出 JSON, 不要任何 markdown, 不要解释):
{{
  "title_patterns": ["从 N 篇笔记里提炼出的标题套路 5 条", ...],
  "hooks": ["开头 3-5 行的钩子套路 5 条 (例: '我以前也是 xx, 直到...', '不是吹, 这招让我...')", ...],
  "structure_outline": "用一段话写出最有效的正文结构 (例: '钩子(2行) → 自我背景(1段) → 3个分点带 emoji → 结尾 CTA')",
  "recommended_word_count": "如 600-900",
  "recommended_image_count": 6,
  "recommended_tags": ["#dse", "#hkdse", "#dse{subject or ''}", "#dse補習", ...],
  "viral_keywords": ["反复出现的词/口头禅, 6-10 个"],
  "facts_to_verify": ["该主题下需要 fact-check 的硬数据 (考试制度/分数线/官方政策), 0-5 条"],
  "selling_points": ["{BRAND_NAME_SHORT} 写这条时可以差异化突出的 3-5 个点 (例: '我们辅导 200+ DSE 学生发现...')"],
  "avoid_list": ["这次必须避开的雷区 (品牌违规/绝对化承诺/同行点名 等), 3-5 条"],
  "raw_research_summary": "300 字以内, 你对这批笔记整体规律的总结"
}}"""

        logger.info(f"🧠 synthesize_brief LLM (model={self.model})")
        resp = await asyncio.to_thread(
            self.llm.chat.completions.create,
            model=self.model,
            messages=[
                {"role": "system", "content": BRAND_VOICE_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.5,
            max_tokens=4000,
        )
        raw = resp.choices[0].message.content or ""
        parsed = _safe_json_loads(raw) or {}
        if not parsed:
            logger.warning(
                f"brief LLM 返回无法解析 JSON, len={len(raw)}, "
                f"head={raw[:500]!r}, tail={raw[-300:]!r}"
            )

        brief = Brief(
            topic=topic,
            subject=subject,
            angle=angle,
            title_patterns=parsed.get("title_patterns") or [],
            hooks=parsed.get("hooks") or [],
            structure_outline=parsed.get("structure_outline") or "",
            recommended_word_count=parsed.get("recommended_word_count") or "600-1000",
            recommended_image_count=int(parsed.get("recommended_image_count") or 6),
            recommended_tags=parsed.get("recommended_tags") or [],
            viral_keywords=parsed.get("viral_keywords") or [],
            facts_to_verify=parsed.get("facts_to_verify") or [],
            selling_points=parsed.get("selling_points") or [],
            avoid_list=parsed.get("avoid_list") or [],
            source_note_ids=[d.feed_id for d in details],
            source_titles=[d.title for d in details],
            raw_research_summary=parsed.get("raw_research_summary") or "",
        )
        return brief

    # ------- generate_draft -------
    async def generate_draft(
        self,
        brief: Brief,
        extra_instructions: str = "",
    ) -> Draft:
        """基于 brief 生成 Draft. 强制要求事实段加 [source: URL] 标注 (先空着 URL, 让人工补)."""
        # 拼 user prompt
        user_prompt = f"""请写一篇小红书笔记草稿, 用 {BRAND_NAME_SHORT} 的口吻 (混合人设: 公司编辑 + senior 学长).

【主题】 {brief.topic}
【科目】 {brief.subject or '通用'}
【方向】 {brief.angle}

【从 {len(brief.source_note_ids)} 篇高赞笔记总结的爆款配方】
- 标题套路: {brief.title_patterns}
- 开头钩子: {brief.hooks}
- 正文结构: {brief.structure_outline}
- 推荐字数: {brief.recommended_word_count}
- 推荐 tag: {brief.recommended_tags}
- 高频词: {brief.viral_keywords}

【必须 fact-check 的硬事实】 (写到正文里时, 句末加 [source: ] 留空让审稿人补 URL)
{json.dumps(brief.facts_to_verify, ensure_ascii=False, indent=2)}

【質心可以差异化突出的卖点 (软植入, 不硬广)】
{json.dumps(brief.selling_points, ensure_ascii=False, indent=2)}

【必须避开的雷区】
{json.dumps(brief.avoid_list, ensure_ascii=False, indent=2)}

{('【额外指示】 ' + extra_instructions) if extra_instructions else ''}

输出严格 JSON, 不要任何 markdown 包裹, 不要任何解释:
{{
  "title": "12-22 字, 带 emoji + 数字/反差",
  "content": "正文, 600-1200 字, 段落短小, 每 2-3 行一段; 涉及硬事实的句子末尾加 [source: ] (留空); 不要在正文末尾加 #tag, tag 用 tags 字段",
  "tags": ["#DSE", "#HKDSE", "#dse{brief.subject or ''}", ...],
  "cover_concept": "一句话描述封面建议 (例: '白底海报, 红字「中文 5** 拆解」, 右下角小学长头像')",
  "fact_lines": [3, 7, 12],
  "fact_citations": {{"原句关键短语": ""}},
  "notes_for_reviewer": "给审稿同事 1-3 句 review 提示 (哪里需要核实, 哪里需要质心实际数据)"
}}

要求:
1. title / content 不能含 "包过/保 5**/绝对/100%" 这类绝对化词
2. content 里如出现具体分数、考试占比、政策、大学要求, 必须末尾 [source: ]
3. fact_lines 是 content 按 \\n 切行后, 含硬事实的行的 0-based index 列表
4. tags 5-10 个, 必须包含 #DSE 或 #HKDSE
5. 不要点名其他补习社; 不要敏感话题
"""
        logger.info(f"🧠 generate_draft LLM (model={self.model})")
        resp = await asyncio.to_thread(
            self.llm.chat.completions.create,
            model=self.model,
            messages=[
                {"role": "system", "content": BRAND_VOICE_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.85,
            max_tokens=4000,
        )
        raw = resp.choices[0].message.content or ""
        parsed = _safe_json_loads(raw) or {}
        if not parsed:
            logger.warning(
                f"draft LLM 返回无法解析 JSON, len={len(raw)}, "
                f"head={raw[:500]!r}, tail={raw[-300:]!r}"
            )

        title = (parsed.get("title") or "").strip()
        content = (parsed.get("content") or "").strip()
        tags = parsed.get("tags") or list(brief.recommended_tags or [])
        # 标准化 tags: 去 # 前缀重复, 限制数量
        norm_tags = []
        seen = set()
        for t in tags:
            if not isinstance(t, str):
                continue
            tt = t.strip().lstrip("#").strip()
            if not tt or tt in seen:
                continue
            seen.add(tt)
            norm_tags.append(tt)
        norm_tags = norm_tags[:10]

        return Draft(
            title=title,
            content=content,
            tags=norm_tags,
            images=[],
            cover_concept=(parsed.get("cover_concept") or "").strip(),
            fact_lines=[int(i) for i in (parsed.get("fact_lines") or []) if isinstance(i, (int, float, str)) and str(i).strip().isdigit()],
            fact_citations=parsed.get("fact_citations") or {},
            brief_snapshot=brief,
            subject=brief.subject,
            topic=brief.topic,
            word_count=len(content),
            notes_for_reviewer=(parsed.get("notes_for_reviewer") or "").strip(),
        )

    # ------- end-to-end helper -------
    async def research_to_draft(
        self,
        topic: str,
        subject: str,
        keyword: Optional[str] = None,
        top_n: int = 8,
        pick_n: int = 3,
        angle: str = "soft_dry_goods",
        extra_instructions: str = "",
    ) -> Dict[str, Any]:
        """便捷链路: 搜索 → 选 top → 拉详情 → brief → draft. 一次性给完."""
        kw = keyword or topic
        cards = await self.search_top_notes(kw, top_n=top_n)
        picks = cards[:pick_n]
        details = await self.fetch_details(picks) if picks else []
        brief = await self.synthesize_brief(topic, subject, details, angle=angle)
        draft = await self.generate_draft(brief, extra_instructions=extra_instructions)
        return {
            "cards": [c.model_dump() for c in cards],
            "picks": [c.model_dump() for c in picks],
            "details": [d.model_dump() for d in details],
            "brief": brief.model_dump(),
            "draft": draft.model_dump(),
        }
