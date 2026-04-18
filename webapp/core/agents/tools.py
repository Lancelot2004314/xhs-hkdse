"""ToolRegistry — agent 可调用的工具.

每个 Tool 是一个独立的 callable, 输入/输出都是 dict (JSON-serializable),
便于 LLM 调用 (将来可换成 native function-calling 接口).

当前提供:
- xhs.search_feeds      搜小红书
- xhs.get_feed_detail   拉笔记详情
- xhs.publish_content   发布
- xhs.check_login       登录态
- web.tavily_search     备选 web 检索 (供 Critic fact-check)
- util.now              当前时间
"""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client

logger = logging.getLogger(__name__)


# =====================================================================
# Tool 定义
# =====================================================================

ToolFn = Callable[[Dict[str, Any]], Awaitable[Any]]


@dataclass
class Tool:
    id: str                                                # 例: "xhs.search_feeds"
    name: str                                              # 中文展示
    description: str                                       # 给 LLM 看 (会注入 prompt)
    args_schema: Dict[str, Any] = field(default_factory=dict)  # JSON Schema-like
    fn: Optional[ToolFn] = None                            # async callable

    def describe_for_llm(self) -> str:
        lines = [f"### tool `{self.id}` — {self.name}", self.description]
        if self.args_schema:
            import json
            lines.append("参数 (JSON Schema):")
            lines.append(json.dumps(self.args_schema, ensure_ascii=False, indent=2))
        return "\n".join(lines)


class ToolRegistry:
    """简单注册表: id -> Tool."""

    def __init__(self):
        self._tools: Dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.id] = tool

    def get(self, tool_id: str) -> Optional[Tool]:
        return self._tools.get(tool_id)

    def list_ids(self) -> List[str]:
        return list(self._tools.keys())

    def list_tools(self) -> List[Tool]:
        return list(self._tools.values())

    def filter(self, ids: List[str]) -> List[Tool]:
        return [self._tools[i] for i in ids if i in self._tools]

    async def invoke(self, tool_id: str, args: Dict[str, Any]) -> Any:
        tool = self.get(tool_id)
        if not tool or not tool.fn:
            raise ValueError(f"未知工具或未实现: {tool_id}")
        return await tool.fn(args)


# =====================================================================
# xhs MCP 工具 — 复用 xhs_research 里的 _fresh_xhs_session
# =====================================================================

@asynccontextmanager
async def _fresh_xhs_session(xhs_mcp_url: str):
    async with streamablehttp_client(xhs_mcp_url) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            yield session


def _mcp_text(call_tool_result: Any) -> str:
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


def _unwrap_taskgroup(exc: BaseException) -> str:
    """asyncio.TaskGroup 把多个子异常包成 ExceptionGroup, 默认 str(exc) 只是 'unhandled
    errors in a TaskGroup (N sub-exceptions)', 完全没用. 这里平铺一遍找真实根因."""
    msgs = []
    seen = set()

    def _walk(e: BaseException, depth: int = 0) -> None:
        if id(e) in seen or depth > 5:
            return
        seen.add(id(e))
        sub = getattr(e, "exceptions", None)
        if sub:
            for s in sub:
                _walk(s, depth + 1)
        else:
            msgs.append(f"{type(e).__name__}: {e}")
        cause = getattr(e, "__cause__", None) or getattr(e, "__context__", None)
        if cause is not None:
            _walk(cause, depth + 1)

    _walk(exc)
    if not msgs:
        return f"{type(exc).__name__}: {exc}"
    return " | ".join(msgs)


async def _call_xhs_with_retry(
    xhs_mcp_url: str,
    tool_name: str,
    payload: Dict[str, Any],
    timeout: float,
    retries: int = 1,
) -> Any:
    """调 xhs-mcp 一个 tool, 自动 unwrap TaskGroup 错误 + 失败重试一次."""
    last_err: Optional[BaseException] = None
    for attempt in range(retries + 1):
        try:
            async with _fresh_xhs_session(xhs_mcp_url) as s:
                res = await asyncio.wait_for(
                    s.call_tool(tool_name, payload),
                    timeout=timeout,
                )
            return res
        except asyncio.TimeoutError:
            last_err = asyncio.TimeoutError(
                f"{tool_name} 超过 {timeout}s 没返回 (xhs-mcp 浏览器可能卡住或被风控). "
                f"重启服务: bash stop.sh && bash start.sh"
            )
        except BaseException as exc:  # noqa: BLE001 — 包含 ExceptionGroup
            last_err = RuntimeError(
                f"{tool_name} 失败 [尝试 {attempt + 1}/{retries + 1}]: "
                f"{_unwrap_taskgroup(exc)}"
            )
        if attempt < retries:
            await asyncio.sleep(1.5)
    assert last_err is not None
    raise last_err


async def _fallback_search_via_tavily(keyword: str, tavily_api_key: str) -> Dict[str, Any]:
    """xhs-mcp search_feeds 上游 bug 时的 fallback (issue #657 #647 #640).

    实测: Tavily 用 site:xiaohongshu.com 限定基本只返回重复的 user profile page,
    没有营养. 改成 '{keyword} 小红书' 不限站, 能拿到混合的新闻/攻略/知乎/小红书
    内容, 反而比单纯 xhs feeds 更有 "趋势研究" 价值.

    返回结构兼容 xhs.search_feeds: {feeds: [{id, noteCard:{displayTitle, summary}}]}
    """
    import httpx

    queries = [f"{keyword} 小红书", f"{keyword} 备考 经验"]
    seen_urls: set[str] = set()
    feeds = []

    async with httpx.AsyncClient(timeout=30) as client:
        for q in queries:
            try:
                r = await client.post(
                    "https://api.tavily.com/search",
                    json={
                        "api_key": tavily_api_key,
                        "query": q,
                        "search_depth": "basic",
                        "max_results": 8,
                        "include_answer": False,
                    },
                )
                r.raise_for_status()
                data = r.json()
            except Exception:
                continue

            for item in (data.get("results") or []):
                url = (item.get("url") or "").strip()
                # 去 query string 后再 dedupe, 避免同 page 多版本
                norm = url.split("?", 1)[0]
                if not norm or norm in seen_urls:
                    continue
                seen_urls.add(norm)
                feeds.append({
                    "id": norm.rsplit("/", 1)[-1] or norm[-20:],
                    "source": "tavily_fallback",
                    "noteCard": {
                        "displayTitle": (item.get("title") or "").strip(),
                        "summary": (item.get("content") or "").strip()[:400],
                        "url": url,
                    },
                })
                if len(feeds) >= 12:
                    break
            if len(feeds) >= 12:
                break

    return {
        "feeds": feeds,
        "_note": (
            "⚠️ 通过 Tavily web 搜索回退获得 (xhs-mcp search_feeds 上游 bug, "
            "见 issue #657). 字段比原生 search_feeds 少: 没有 likes/comments/"
            "xsec_token, 因此无法对结果调用 xhs.get_feed_detail. 来源混合 "
            "(新闻/攻略博客/知乎 等), 直接基于 title+summary 提炼趋势和选题即可."
        ),
    }


def make_xhs_tools(
    xhs_mcp_url: str,
    tavily_api_key: Optional[str] = None,
) -> List[Tool]:
    """构建调 xhs-mcp 的工具集. 每次调用都开新 session 避免长连接污染."""

    # 进程级 "mcp search 已坏" 标记 — 一次失败后 5 分钟内所有 search_feeds
    # 直接走 Tavily, 不再浪费时间在已知坏掉的 mcp 上.
    # (state 用 list 包一下避开 nonlocal 限制, 简单)
    _mcp_search_broken_until: List[float] = [0.0]
    SEARCH_BROKEN_TTL = 300.0  # 5 分钟

    async def _search_feeds(args: Dict[str, Any]) -> Any:
        keyword = args.get("keyword") or ""
        if not keyword:
            raise ValueError("keyword 必填")

        logger = logging.getLogger(__name__)
        now = time.time()

        # 5 分钟内已知 mcp search 坏掉 → 直接走 fallback, 不再尝试
        if tavily_api_key and now < _mcp_search_broken_until[0]:
            remain = int(_mcp_search_broken_until[0] - now)
            logger.info(
                "🔁 search_feeds 直接走 Tavily fallback (mcp search 标记为坏, "
                "还剩 %ds 才会重新尝试 mcp)", remain
            )
            import json as _json
            fb = await _fallback_search_via_tavily(keyword, tavily_api_key)
            return _json.dumps(fb, ensure_ascii=False)

        # 实测: filters 参数会让 xhs-mcp 浏览器自动化 hang. 不传, 客户端排序.
        # timeout 30s + 0 retry: 上游正常时 6-9s 返回, 坏时 30s 已经够判定,
        # 不浪费 3 分钟在重试上 (issue #657 评论说重试也几乎没用).
        try:
            res = await _call_xhs_with_retry(
                xhs_mcp_url, "search_feeds", {"keyword": keyword}, timeout=30, retries=0
            )
            return _mcp_text(res)
        except Exception as exc:
            if tavily_api_key:
                _mcp_search_broken_until[0] = time.time() + SEARCH_BROKEN_TTL
                logger.warning(
                    "🔁 search_feeds 失败, fallback 到 Tavily + 标记 mcp search "
                    "坏掉 %ds: %s",
                    int(SEARCH_BROKEN_TTL),
                    str(exc)[:200],
                )
                import json as _json
                fallback = await _fallback_search_via_tavily(keyword, tavily_api_key)
                return _json.dumps(fallback, ensure_ascii=False)
            raise

    async def _get_feed_detail(args: Dict[str, Any]) -> Any:
        feed_id = args.get("feed_id")
        token = args.get("xsec_token")
        if not feed_id or not token:
            raise ValueError("feed_id 和 xsec_token 必填")
        res = await _call_xhs_with_retry(
            xhs_mcp_url,
            "get_feed_detail",
            {"feed_id": feed_id, "xsec_token": token},
            timeout=120,
            retries=1,
        )
        return _mcp_text(res)

    async def _publish_content(args: Dict[str, Any]) -> Any:
        title = args.get("title") or ""
        content = args.get("content") or ""
        images = args.get("images") or []
        tags = args.get("tags") or []
        if not (title and content and images):
            raise ValueError("title/content/images 必填")
        publish_args = {
            "title": title,
            "content": content,
            "images": images,
            "tags": tags,
        }
        # 发布失败重试容易撞重复风控, 只跑一次
        res = await _call_xhs_with_retry(
            xhs_mcp_url, "publish_content", publish_args, timeout=180, retries=0
        )
        return _mcp_text(res)

    async def _check_login(_: Dict[str, Any]) -> Any:
        res = await _call_xhs_with_retry(
            xhs_mcp_url, "check_login_status", {}, timeout=30, retries=0
        )
        return _mcp_text(res)

    return [
        Tool(
            id="xhs.search_feeds",
            name="小红书搜索",
            description=(
                "按关键词搜索小红书笔记, 返回 JSON {feeds: [{id, xsecToken, noteCard:{...}}]}. "
                "默认按平台综合排序, 客户端再二次按点赞排. "
                "⚠️ xhs-mcp 上游有已知 bug 时, 自动 fallback 到 Tavily web 搜索, "
                "返回结构里会带 _note 字段说明, 字段会少 (没 likes/xsec_token), "
                "你直接基于 displayTitle+summary 做趋势判断即可, 不要再调 get_feed_detail."
            ),
            args_schema={"type": "object", "required": ["keyword"], "properties": {
                "keyword": {"type": "string", "description": "搜索词"}
            }},
            fn=_search_feeds,
        ),
        Tool(
            id="xhs.get_feed_detail",
            name="笔记详情",
            description="拉指定笔记的完整内容 + 图片 + 高赞评论.",
            args_schema={"type": "object", "required": ["feed_id", "xsec_token"], "properties": {
                "feed_id": {"type": "string"},
                "xsec_token": {"type": "string"},
            }},
            fn=_get_feed_detail,
        ),
        Tool(
            id="xhs.publish_content",
            name="发布到小红书",
            description="把图文笔记发到当前登录账号. 需 title/content/images(本地路径列表)/tags.",
            args_schema={"type": "object", "required": ["title", "content", "images"], "properties": {
                "title": {"type": "string"},
                "content": {"type": "string"},
                "images": {"type": "array", "items": {"type": "string"}},
                "tags": {"type": "array", "items": {"type": "string"}},
            }},
            fn=_publish_content,
        ),
        Tool(
            id="xhs.check_login",
            name="检查登录状态",
            description="确认 xhs 账号已登录.",
            args_schema={"type": "object"},
            fn=_check_login,
        ),
    ]


# =====================================================================
# Web search 工具 — 给 Critic 做 fact-check 用 (可选)
# =====================================================================

def make_web_tools(tavily_api_key: Optional[str] = None) -> List[Tool]:
    """目前用 Jina Reader 替代 Tavily 也行; 没 key 时工具仍注册但调用会报错."""

    async def _tavily_search(args: Dict[str, Any]) -> Any:
        if not tavily_api_key:
            raise RuntimeError("TAVILY_API_KEY 未配置, 无法做 web 检索")
        import httpx
        query = args.get("query") or ""
        max_results = int(args.get("max_results") or 5)
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                "https://api.tavily.com/search",
                json={
                    "api_key": tavily_api_key,
                    "query": query,
                    "search_depth": "basic",
                    "max_results": max_results,
                    "include_answer": True,
                },
            )
            r.raise_for_status()
            return r.json()

    return [
        Tool(
            id="web.search",
            name="Web 检索",
            description="用 Tavily 做 web 检索 (适合 Critic 给 [source: ] 找权威 URL).",
            args_schema={"type": "object", "required": ["query"], "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer", "default": 5},
            }},
            fn=_tavily_search,
        )
    ]


# =====================================================================
# 图像生成工具 — OpenRouter / ByteDance Seedream 4.5
# (中文小字渲染 / 封面文字准确度优于 Gemini 2.5 Flash Image)
# =====================================================================

# 默认输出根目录 (相对 webapp/cache/images/<draft_id>/<file>.png)
import os
import pathlib
import uuid as _uuid

_IMAGES_ROOT = pathlib.Path(__file__).resolve().parents[2] / "cache" / "images"


def make_image_tools(
    openrouter_api_key: Optional[str] = None,
    base_url: str = "https://openrouter.ai/api/v1",
    model: str = "bytedance-seed/seedream-4.5",
) -> List[Tool]:
    """图像生成工具. 默认使用 ByteDance Seedream 4.5 (小字/中文文字渲染更准)."""

    async def _image_generate(args: Dict[str, Any]) -> Any:
        if not openrouter_api_key:
            raise RuntimeError("OpenRouter API key 未配置, 无法生成图片")
        import base64
        import httpx

        prompt = (args.get("prompt") or "").strip()
        if not prompt:
            raise ValueError("prompt 必填")
        # 输出路径: 优先用调用方提供, 否则按 draft_id/role 自动生成
        out_path = args.get("output_path")
        if not out_path:
            draft_id = args.get("draft_id") or f"adhoc-{_uuid.uuid4().hex[:8]}"
            role = args.get("role") or f"img_{_uuid.uuid4().hex[:6]}"
            out_path = str(_IMAGES_ROOT / str(draft_id) / f"{role}.png")
        out_path = str(out_path)
        aspect_ratio = args.get("aspect_ratio") or "3:4"

        # OpenRouter 图像生成: messages + modalities=["image"]
        # (Seedream 4.5 是 image-only, 传 ["image","text"] 会 404)
        # 把宽高比塞到 prompt 里
        full_prompt = f"{prompt}\n\nAspect ratio: {aspect_ratio}. High quality, social media cover style."

        # 模型当前用的: Seedream / Gemini 都接受 modalities=["image"]
        mdl = (args.get("model") or model).strip()

        payload = {
            "model": mdl,
            "messages": [{"role": "user", "content": full_prompt}],
            "modalities": ["image"],
        }
        headers = {
            "Authorization": f"Bearer {openrouter_api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://zhixin-edu.local/",
            "X-Title": "ZhiXin XHS Studio",
        }
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(f"{base_url.rstrip('/')}/chat/completions",
                                  headers=headers, json=payload)
        if r.status_code >= 400:
            raise RuntimeError(f"OpenRouter image gen 失败 {r.status_code}: {r.text[:300]}")
        try:
            data = r.json()
        except Exception as e:
            raise RuntimeError(f"OpenRouter 响应非 JSON: {r.text[:200]}") from e

        # 兼容多种返回格式: choices[0].message.images[0].image_url.url 是 data:image/png;base64,xxx
        img_url = None
        try:
            msg = data["choices"][0]["message"]
            imgs = msg.get("images") or []
            if imgs:
                first = imgs[0]
                if isinstance(first, dict):
                    iu = first.get("image_url")
                    if isinstance(iu, dict):
                        img_url = iu.get("url")
                    elif isinstance(iu, str):
                        img_url = iu
                    else:
                        img_url = first.get("url") or first.get("b64_json")
                elif isinstance(first, str):
                    img_url = first
        except Exception:
            pass
        if not img_url:
            raise RuntimeError(f"未能从 OpenRouter 响应解析出图片: {str(data)[:300]}")

        # 解码: 支持 data:image/png;base64,xxx 也支持纯 base64 也支持 https:// (下载)
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        if img_url.startswith("data:"):
            b64 = img_url.split(",", 1)[1]
            with open(out_path, "wb") as f:
                f.write(base64.b64decode(b64))
        elif img_url.startswith("http"):
            async with httpx.AsyncClient(timeout=60) as client:
                rr = await client.get(img_url)
                rr.raise_for_status()
                with open(out_path, "wb") as f:
                    f.write(rr.content)
        else:
            with open(out_path, "wb") as f:
                f.write(base64.b64decode(img_url))

        size = os.path.getsize(out_path)
        # 给前端用的 URL (对应 app.py 的 app.mount("/cache/images", ...))
        try:
            rel = pathlib.Path(out_path).relative_to(_IMAGES_ROOT)  # e2e-test/cover.png
            url = "/cache/images/" + str(rel).replace(os.sep, "/")
        except Exception:
            url = out_path
        return {"path": out_path, "url": url, "bytes": size, "model": mdl}

    return [
        Tool(
            id="image.generate",
            name="生成图片",
            description=(
                "用 ByteDance Seedream 4.5 (OpenRouter) 生成单张图片. "
                "封面文字 / 中文小字渲染比 Gemini Flash Image 更准. "
                "适合小红书封面 (aspect_ratio 3:4) 和正文配图 (1:1). "
                "支持中文 prompt; 避免真实人像 (政策风险). "
                "返回 {path, url, bytes}; 同一 draft_id 的多张图会归到同一目录."
            ),
            args_schema={
                "type": "object",
                "required": ["prompt"],
                "properties": {
                    "prompt": {"type": "string", "description": "中文/英文 prompt; 包含主题、风格、文字"},
                    "draft_id": {"type": "string", "description": "草稿 id, 用于归档目录"},
                    "role": {"type": "string", "description": "cover / body_1 / body_2"},
                    "output_path": {"type": "string", "description": "可选完整磁盘路径; 未传则自动按 draft_id+role 生成"},
                    "aspect_ratio": {"type": "string", "enum": ["3:4", "1:1", "16:9", "9:16"], "default": "3:4"},
                    "model": {"type": "string", "description": "覆盖默认模型 (默认 bytedance-seed/seedream-4.5)"},
                },
            },
            fn=_image_generate,
        )
    ]


# =====================================================================
# 工具集装配
# =====================================================================

def build_default_registry(
    xhs_mcp_url: str,
    tavily_api_key: Optional[str] = None,
    openrouter_api_key: Optional[str] = None,
    image_base_url: str = "https://openrouter.ai/api/v1",
    image_model: str = "bytedance-seed/seedream-4.5",
) -> ToolRegistry:
    reg = ToolRegistry()
    for t in make_xhs_tools(xhs_mcp_url, tavily_api_key=tavily_api_key):
        reg.register(t)
    for t in make_web_tools(tavily_api_key):
        reg.register(t)
    for t in make_image_tools(openrouter_api_key, image_base_url, image_model):
        reg.register(t)

    # util tools
    async def _now(_: Dict[str, Any]) -> Any:
        from datetime import datetime
        return {"iso": datetime.now().isoformat()}

    reg.register(Tool(
        id="util.now",
        name="当前时间",
        description="返回当前 ISO 时间戳.",
        args_schema={"type": "object"},
        fn=_now,
    ))
    return reg
