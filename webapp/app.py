"""
小红书内容自动生成与发布 - Web应用主程序 (FastAPI版本)
"""
import os
import logging
import asyncio
import time
import uuid
from datetime import datetime
from typing import Dict, Any, List, Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import requests

from core.content_generator import ContentGenerator
from core.server_manager import server_manager
from core.xhs_research import (
    XhsResearcher,
    NoteCard,
    NoteDetail,
    Brief,
    Draft,
    BRAND_NAME_FULL,
    BRAND_NAME_SHORT,
    _fresh_xhs_session,
    _mcp_text,
)
from core.agents import (
    AgentEvent,
    AgentSpec,
    Orchestrator,
    EventBus,
    build_default_registry,
    get_workflow,
    WORKFLOWS,
    load_agent_specs,
    save_agent_specs,
    DEFAULT_SPECS_PATH,
)
from core.agents.workflows import list_workflows
from config.config_manager import ConfigManager
from cache.cache_manager import CacheManager

# 小红书平台硬限制 (超出 publish API 会拒绝/截断)
XHS_TITLE_HARD_CAP = 20
XHS_CONTENT_HARD_CAP = 1000


def _enforce_xhs_caps(title: str, body: str) -> tuple[str, str]:
    """最后一道防线: LLM 偶尔超长时静默截断到平台硬上限, 避免 publish 被 reject.
    Writer/Critic prompt 已经把 soft cap 设成 18/900, 这里 20/1000 是 platform 物理上限."""
    if len(title) > XHS_TITLE_HARD_CAP:
        logger.warning(f"⚠️ title 超 {XHS_TITLE_HARD_CAP} 字 ({len(title)}), 截断: {title!r}")
        title = title[:XHS_TITLE_HARD_CAP]
    if len(body) > XHS_CONTENT_HARD_CAP:
        logger.warning(f"⚠️ content 超 {XHS_CONTENT_HARD_CAP} 字 ({len(body)}), 截断")
        body = body[:XHS_CONTENT_HARD_CAP]
    return title, body

# 获取当前文件的目录
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# 生命周期管理
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时执行
    os.makedirs(os.path.join(BASE_DIR, 'config'), exist_ok=True)
    logger.info("应用启动，目录初始化完成")

    # 尝试初始化全局 MCP 服务器(如果配置存在)
    try:
        config = config_manager.load_config(for_display=False)
        if config.get('llm_api_key') and config.get('openai_base_url'):
            logger.info("检测到配置文件,开始初始化全局 MCP 服务器...")
            await server_manager.initialize(config)
            logger.info("✅ 全局 MCP 服务器初始化完成,请求将直接使用已初始化的连接")
        else:
            logger.info("配置不完整,跳过 MCP 服务器初始化")
    except Exception as e:
        logger.warning(f"启动时初始化 MCP 服务器失败: {e}, 将在首次请求时初始化")

    yield

    # 关闭时执行
    logger.info("应用关闭,清理资源...")
    try:
        await server_manager.cleanup()
        logger.info("✅ 全局 MCP 服务器资源清理完成")
    except Exception as e:
        logger.error(f"清理资源失败: {e}")
    logger.info("应用关闭完成")


# 创建 FastAPI 应用
app = FastAPI(
    title="小红书内容自动生成与发布系统",
    description="智能生成高质量小红书内容，一键发布",
    version="1.0.0",
    lifespan=lifespan
)

# 配置 CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 配置模板
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# 初始化配置管理器和缓存管理器
config_manager = ConfigManager()
cache_manager = CacheManager()


# Pydantic 模型
class ConfigRequest(BaseModel):
    llm_api_key: Optional[str] = None
    openai_base_url: Optional[str] = None
    default_model: Optional[str] = None
    jina_api_key: Optional[str] = None
    tavily_api_key: Optional[str] = None
    xhs_mcp_url: Optional[str] = None


class TestLoginRequest(BaseModel):
    xhs_mcp_url: str


class ValidateModelRequest(BaseModel):
    model_config = {"protected_namespaces": ()}

    llm_api_key: str
    openai_base_url: str
    model_name: str


class GeneratePublishRequest(BaseModel):
    topic: str
    content_type: str = "general"  # "general" 或 "paper_analysis"
    task_id: Optional[str] = None  # 用于重试时更新现有任务


class PreviewRequest(BaseModel):
    topic: str
    content_type: str = "general"


class PublishNowRequest(BaseModel):
    title: str
    content: str
    images: List[str]
    tags: Optional[List[str]] = None


class TaskHistoryQueryRequest(BaseModel):
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    status: Optional[str] = None
    limit: int = 100


class BatchGeneratePublishRequest(BaseModel):
    topics: List[str]
    content_type: str = "general"  # "general" 或 "paper_analysis"


# ============ Studio v2: 研究 / 草稿 / 账号 ============

class ResearchSearchRequest(BaseModel):
    keyword: str
    sort_by: str = "最多点赞"   # 综合 / 最新 / 最多点赞 / 最多评论 / 最多收藏
    note_type: str = "图文"      # 不限 / 视频 / 图文
    publish_time: str = "不限"   # 不限 / 一天内 / 一周内 / 半年内
    top_n: int = 10


class ResearchDetailsRequest(BaseModel):
    picks: List[Dict[str, Any]]   # [{feed_id, xsec_token, title?, ...}, ...]


class ResearchBriefRequest(BaseModel):
    topic: str
    subject: str = ""
    angle: str = "soft_dry_goods"  # soft_dry_goods / hard_dry_goods / senior_story / parent
    details: List[Dict[str, Any]]  # NoteDetail 列表 (前端来回传)


class DraftGenerateRequest(BaseModel):
    brief: Dict[str, Any]
    extra_instructions: str = ""


class DraftSaveRequest(BaseModel):
    draft: Dict[str, Any]


class DraftPatchRequest(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None
    tags: Optional[List[str]] = None
    images: Optional[List[str]] = None


class DraftPublishRequest(BaseModel):
    images: Optional[List[str]] = None  # 允许审稿时上传/替换图片


# 路由
@app.get("/")
async def index(request: Request):
    """根路径自动跳转到新 Studio (旧 UI 在 /legacy)."""
    return RedirectResponse(url="/studio", status_code=302)


@app.get("/legacy", response_class=HTMLResponse)
async def legacy_index(request: Request):
    """旧版 Creator UI (保留兜底)."""
    return templates.TemplateResponse(request, "index.html")


@app.get("/api/config")
async def get_config() -> Dict[str, Any]:
    """获取配置信息（密钥已脱敏）"""
    try:
        # 使用脱敏模式加载配置
        config = config_manager.load_config(mask_sensitive=True)
        return {'success': True, 'config': config}
    except Exception as e:
        logger.error(f"获取配置失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/config")
async def save_config(config_data: ConfigRequest) -> Dict[str, Any]:
    """保存配置(支持部分更新)"""
    try:
        # 将请求数据转换为字典,排除未设置的字段
        config_dict = config_data.model_dump(exclude_unset=True)

        # 过滤掉脱敏的占位符(以*开头的值不更新)
        filtered_config = {
            k: v for k, v in config_dict.items()
            if v and not (isinstance(v, str) and '*' in v)
        }

        # 如果没有要更新的字段
        if not filtered_config:
            return {'success': True, 'message': '没有需要更新的配置'}

        # 保存配置(支持部分更新)
        config_manager.save_config(filtered_config)

        # 重新初始化全局 MCP 服务器
        try:
            logger.info("配置已更新,重新初始化全局 MCP 服务器...")
            # 先清理旧的服务器连接
            await server_manager.cleanup()
            # 初始化新的服务器连接
            await server_manager.initialize(config_dict)
            logger.info("✅ 全局 MCP 服务器重新初始化完成")
        except Exception as e:
            logger.error(f"重新初始化 MCP 服务器失败: {e}")
            # 不阻止配置保存,只记录错误

        return {'success': True, 'message': '配置保存成功'}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"保存配置失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/validate-model")
async def validate_model(request_data: ValidateModelRequest) -> Dict[str, Any]:
    """验证模型是否可用"""
    try:
        llm_api_key = request_data.llm_api_key
        openai_base_url = request_data.openai_base_url
        model_name = request_data.model_name

        if not llm_api_key or not openai_base_url or not model_name:
            raise HTTPException(status_code=400, detail="请检查LLM API key、Base URL和模型名称是否填写完整")

        # 尝试调用模型进行验证
        try:
            import openai

            client = openai.OpenAI(
                api_key=llm_api_key,
                base_url=openai_base_url
            )

            # 发送一个简单的测试请求
            response = client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "user", "content": "Hi"}
                ],
                stream=False
            )

            if response and response.choices:
                return {
                    'success': True,
                    'message': f'模型 {model_name} 验证成功',
                    'model': model_name
                }
            else:
                raise HTTPException(
                    status_code=500,
                    detail=f'模型 {model_name} 响应异常'
                )

        except Exception as e:
            error_msg = str(e)
            # 检查是否是模型不存在的错误
            if 'model_not_found' in error_msg.lower() or 'does not exist' in error_msg.lower() or 'invalid model' in error_msg.lower():
                raise HTTPException(
                    status_code=400,
                    detail=f'模型 {model_name} 不存在或不可用'
                )
            else:
                raise HTTPException(
                    status_code=500,
                    detail=f'模型验证失败: {error_msg}'
                )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"验证模型失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/test-login")
async def test_login(request_data: TestLoginRequest) -> Dict[str, Any]:
    """测试小红书账号登录 (调 MCP check_login_status)."""
    try:
        xhs_url = request_data.xhs_mcp_url or config_manager.load_config(for_display=False).get("xhs_mcp_url")
        if not xhs_url:
            raise HTTPException(status_code=400, detail="请提供 xhs_mcp_url")
        async with _fresh_xhs_session(xhs_url) as session:
            result = await asyncio.wait_for(session.call_tool("check_login_status", {}), timeout=15)
        text = _mcp_text(result)
        logged_in = ("已登录" in text) or ("✅" in text) or ("logged" in text.lower())
        return {
            "success": True,
            "status": "connected" if logged_in else "not_logged_in",
            "logged_in": logged_in,
            "message": "小红书账号已登录" if logged_in else "MCP 已连通但未登录",
            "raw": text[:300],
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"测试登录失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/generate-and-publish")
async def generate_and_publish(request_data: GeneratePublishRequest) -> Dict[str, Any]:
    """生成内容并发布到小红书"""
    try:
        topic = request_data.topic
        content_type = request_data.content_type
        task_id = request_data.task_id

        if not topic:
            raise HTTPException(status_code=400, detail="请输入主题")

        # 验证内容类型
        if content_type not in ["general", "paper_analysis"]:
            raise HTTPException(status_code=400, detail="内容类型必须是 'general' 或 'paper_analysis'")

        # 检查配置是否完整
        config = config_manager.load_config()
        if not config.get('llm_api_key') or not config.get('xhs_mcp_url'):
            raise HTTPException(status_code=400, detail="请先完成配置")

        # 创建内容生成器
        generator = ContentGenerator(config)

        # 异步执行内容生成和发布
        result = await generator.generate_and_publish(topic, content_type)

        if result.get('success'):
            response_data = {
                'title': result.get('title', ''),
                'content': result.get('content', ''),
                'tags': result.get('tags', []),
                'images': result.get('images', []),
                'publish_status': result.get('publish_status', ''),
                'publish_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }

            # 保存到缓存
            task_record = {
                'topic': topic,
                'status': 'success',
                'progress': 100,
                'message': '发布成功',
                'content_type': content_type,
                **response_data
            }
            
            # 如果提供了task_id，则更新现有任务，否则添加新任务
            if task_id:
                cache_manager.update_task(task_id, task_record)
            else:
                cache_manager.add_task(task_record)

            return {
                'success': True,
                'message': '内容生成并发布成功',
                'data': response_data
            }
        else:
            # 保存失败记录到缓存
            error_record = {
                'topic': topic,
                'status': 'error',
                'progress': 0,
                'message': result.get('error', '生成失败'),
                'content_type': content_type
            }
            
            # 如果提供了task_id，则更新现有任务，否则添加新任务
            if task_id:
                cache_manager.update_task(task_id, error_record)
            else:
                cache_manager.add_task(error_record)

            raise HTTPException(
                status_code=500,
                detail=result.get('error', '生成失败')
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"生成和发布失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/preview")
async def preview_only(request_data: PreviewRequest) -> Dict[str, Any]:
    """生成内容但不发布 (dry-run), 返回草稿供审核"""
    try:
        topic = request_data.topic
        content_type = request_data.content_type

        if not topic:
            raise HTTPException(status_code=400, detail="请输入主题")
        if content_type not in ["general", "paper_analysis"]:
            raise HTTPException(status_code=400, detail="content_type 必须是 'general' 或 'paper_analysis'")

        config = config_manager.load_config()
        if not config.get('llm_api_key') or not config.get('xhs_mcp_url'):
            raise HTTPException(status_code=400, detail="请先完成配置")

        generator = ContentGenerator(config)
        generator.dry_run = True

        result = await generator.generate_and_publish(topic, content_type)

        captured = generator.captured_publish_args or {}
        draft = {
            'topic': topic,
            'title': captured.get('title') or result.get('title', ''),
            'content': captured.get('content') or result.get('content', ''),
            'images': captured.get('images') or result.get('images', []),
            'tags': captured.get('tags') or result.get('tags', []),
            'content_type': content_type,
        }

        # 写入审核队列 (status=draft)
        cache_manager.add_task({
            **draft,
            'status': 'draft',
            'progress': 100,
            'message': '草稿待审核',
        })

        return {
            'success': True,
            'message': '草稿生成完成, 请审核后发布',
            'data': draft
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"预览生成失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/publish-now")
async def publish_now(request_data: PublishNowRequest) -> Dict[str, Any]:
    """直接调 xhs MCP publish_content 把审核过的稿子发出去"""
    try:
        if not request_data.title or not request_data.content or not request_data.images:
            raise HTTPException(status_code=400, detail="title / content / images 必填")

        config = config_manager.load_config(for_display=False)
        xhs_url = config.get('xhs_mcp_url')
        if not xhs_url:
            raise HTTPException(status_code=400, detail="未配置 xhs_mcp_url")

        body = request_data.content
        if request_data.tags:
            tag_line = ' '.join(f'#{t.lstrip("#").strip()}' for t in request_data.tags if t.strip())
            if tag_line:
                body = f"{body}\n\n{tag_line}"

        safe_title, safe_body = _enforce_xhs_caps(request_data.title, body)
        args = {
            'title': safe_title,
            'content': safe_body,
            'images': request_data.images,
        }
        logger.info(f"📤 直接发布: title={safe_title} ({len(safe_title)}字), content {len(safe_body)}字, {len(request_data.images)} 张图")
        async with _fresh_xhs_session(xhs_url) as session:
            tool_result = await asyncio.wait_for(
                session.call_tool('publish_content', args),
                timeout=240,
            )
        result_str = _mcp_text(tool_result) or str(tool_result)
        ok = ('success' in result_str.lower()) or ('成功' in result_str) or ('published' in result_str.lower())

        # 记到历史
        cache_manager.add_task({
            'topic': request_data.title,
            'title': request_data.title,
            'content': request_data.content,
            'images': request_data.images,
            'tags': request_data.tags or [],
            'status': 'success' if ok else 'error',
            'progress': 100 if ok else 0,
            'message': '发布成功' if ok else f'发布失败: {result_str[:200]}',
            'content_type': 'general',
            'publish_status': '已成功发布' if ok else '失败',
            'publish_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        })

        if not ok:
            raise HTTPException(status_code=500, detail=f"小红书 MCP 返回失败: {result_str[:300]}")

        return {'success': True, 'message': '已发布到小红书', 'mcp_result': result_str[:500]}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"直接发布失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/review", response_class=HTMLResponse)
async def review_page(request: Request):
    """极简审核台: 选题 → 预览 → 一键发布 (旧 UI, 保留)"""
    return templates.TemplateResponse(request, "review.html")


# ====================================================================
# Studio v2: 統一 SPA + 研究/草稿/账号 API
# ====================================================================

@app.get("/studio", response_class=HTMLResponse)
async def studio_page(request: Request):
    """質心教育 XHS Studio (主入口 SPA)."""
    return templates.TemplateResponse(
        request,
        "studio.html",
        {
            "brand_full": BRAND_NAME_FULL,
            "brand_short": BRAND_NAME_SHORT,
        },
    )


def _make_researcher() -> XhsResearcher:
    """构造 XhsResearcher; 内部会按需开新 MCP 连接."""
    config = config_manager.load_config(for_display=False)
    if not config.get('llm_api_key') or not config.get('openai_base_url'):
        raise HTTPException(status_code=400, detail="请先在 配置 tab 填写 LLM API key / Base URL")
    if not config.get('xhs_mcp_url'):
        raise HTTPException(status_code=400, detail="请先在 配置 tab 填写 xhs_mcp_url")
    return XhsResearcher(
        xhs_mcp_url=config['xhs_mcp_url'],
        llm_api_key=config['llm_api_key'],
        llm_base_url=config['openai_base_url'],
        llm_model=config.get('default_model') or 'anthropic/claude-sonnet-4.5',
    )


@app.post("/api/research/search")
async def api_research_search(req: ResearchSearchRequest) -> Dict[str, Any]:
    """直接搜小红书, 返回 NoteCard 列表 (按最多点赞)."""
    if not req.keyword.strip():
        raise HTTPException(status_code=400, detail="keyword 不能为空")
    researcher = _make_researcher()
    cards = await researcher.search_top_notes(
        keyword=req.keyword.strip(),
        sort_by=req.sort_by,
        note_type=req.note_type,
        publish_time=req.publish_time,
        top_n=req.top_n,
    )
    return {
        "success": True,
        "count": len(cards),
        "cards": [c.model_dump(exclude={"raw"}) for c in cards],
    }


@app.post("/api/research/details")
async def api_research_details(req: ResearchDetailsRequest) -> Dict[str, Any]:
    """对前端勾选的 picks 拉详情 (含正文 + 高赞评论)."""
    if not req.picks:
        raise HTTPException(status_code=400, detail="picks 不能为空")
    researcher = _make_researcher()
    note_cards = [
        NoteCard(
            feed_id=p.get("feed_id", ""),
            xsec_token=p.get("xsec_token", ""),
            title=p.get("title", ""),
        )
        for p in req.picks
        if p.get("feed_id") and p.get("xsec_token")
    ]
    if not note_cards:
        raise HTTPException(status_code=400, detail="picks 缺 feed_id / xsec_token")
    details = await researcher.fetch_details(note_cards)
    return {
        "success": True,
        "count": len(details),
        "details": [d.model_dump() for d in details],
    }


@app.post("/api/research/brief")
async def api_research_brief(req: ResearchBriefRequest) -> Dict[str, Any]:
    """LLM 综合 details, 输出爆款配方 Brief."""
    if not req.topic.strip():
        raise HTTPException(status_code=400, detail="topic 不能为空")
    if not req.details:
        raise HTTPException(status_code=400, detail="details 不能为空, 请先研究")
    researcher = _make_researcher()
    details_objs = [NoteDetail(**d) for d in req.details]
    brief = await researcher.synthesize_brief(
        topic=req.topic.strip(),
        subject=req.subject,
        details=details_objs,
        angle=req.angle,
    )
    return {"success": True, "brief": brief.model_dump()}


@app.post("/api/draft/generate")
async def api_draft_generate(req: DraftGenerateRequest) -> Dict[str, Any]:
    """基于 Brief 生成 Draft, 自动入草稿队列 (status=draft)."""
    researcher = _make_researcher()
    brief = Brief(**req.brief)
    draft = await researcher.generate_draft(brief, extra_instructions=req.extra_instructions)
    # 入草稿队列
    draft_dict = draft.model_dump()
    task_id = cache_manager.add_task({
        "topic": draft.topic or brief.topic,
        "title": draft.title,
        "content": draft.content,
        "tags": draft.tags,
        "images": draft.images,
        "fact_lines": draft.fact_lines,
        "subject": draft.subject or brief.subject,
        "status": "draft",
        "progress": 100,
        "message": "草稿待审核",
        "content_type": brief.angle,
        "brief": brief.model_dump(),
    })
    draft_dict["id"] = task_id
    return {"success": True, "draft": draft_dict}


@app.get("/api/drafts")
async def api_list_drafts(limit: int = 100) -> Dict[str, Any]:
    """列出所有 status=draft 的草稿."""
    drafts = cache_manager.list_by_status("draft", limit=limit)
    return {"success": True, "count": len(drafts), "drafts": drafts}


@app.get("/api/draft/{draft_id}")
async def api_get_draft(draft_id: str) -> Dict[str, Any]:
    d = cache_manager.get_task_by_id(draft_id)
    if not d:
        raise HTTPException(status_code=404, detail="草稿不存在")
    return {"success": True, "draft": d}


@app.patch("/api/draft/{draft_id}")
async def api_patch_draft(draft_id: str, patch: DraftPatchRequest) -> Dict[str, Any]:
    """审稿时编辑草稿 (title/content/tags/images)."""
    existing = cache_manager.get_task_by_id(draft_id)
    if not existing:
        raise HTTPException(status_code=404, detail="草稿不存在")
    updates = {k: v for k, v in patch.model_dump(exclude_unset=True).items() if v is not None}
    ok = cache_manager.update_task(draft_id, updates)
    if not ok:
        raise HTTPException(status_code=500, detail="更新失败")
    return {"success": True, "draft": cache_manager.get_task_by_id(draft_id)}


@app.delete("/api/draft/{draft_id}")
async def api_delete_draft(draft_id: str) -> Dict[str, Any]:
    ok = cache_manager.delete_task(draft_id)
    if not ok:
        raise HTTPException(status_code=404, detail="草稿不存在")
    return {"success": True}


@app.post("/api/draft/{draft_id}/publish")
async def api_publish_draft(draft_id: str, req: DraftPublishRequest) -> Dict[str, Any]:
    """把草稿发到小红书 (要求 images 至少 1 张)."""
    d = cache_manager.get_task_by_id(draft_id)
    if not d:
        raise HTTPException(status_code=404, detail="草稿不存在")

    images = req.images if req.images else (d.get("images") or [])
    if not images:
        raise HTTPException(status_code=400, detail="请上传至少 1 张图片再发布")

    title = d.get("title", "")
    content = d.get("content", "")
    tags = d.get("tags") or []
    if not title or not content:
        raise HTTPException(status_code=400, detail="草稿缺 title/content")

    config = config_manager.load_config(for_display=False)
    xhs_url = config.get("xhs_mcp_url")
    if not xhs_url:
        raise HTTPException(status_code=400, detail="未配置 xhs_mcp_url")

    body = content
    if tags:
        tag_line = ' '.join(f'#{str(t).lstrip("#").strip()}' for t in tags if str(t).strip())
        if tag_line:
            body = f"{body}\n\n{tag_line}"

    safe_title, safe_body = _enforce_xhs_caps(title, body)
    args = {"title": safe_title, "content": safe_body, "images": images}
    logger.info(f"📤 发布草稿 {draft_id}: {safe_title} ({len(safe_title)}字), content {len(safe_body)}字, {len(images)} 张图")
    try:
        async with _fresh_xhs_session(xhs_url) as session:
            tool_result = await asyncio.wait_for(
                session.call_tool("publish_content", args),
                timeout=240,
            )
    except asyncio.TimeoutError:
        cache_manager.update_task_status(draft_id, "error", {
            "message": "发布超时 (240s)",
        })
        raise HTTPException(status_code=504, detail="发布超时, 请重试")

    result_str = _mcp_text(tool_result) or str(tool_result)
    ok = ('success' in result_str.lower()) or ('成功' in result_str) or ('published' in result_str.lower())

    if ok:
        cache_manager.update_task_status(draft_id, "success", {
            "message": "发布成功",
            "images": images,
            "publish_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            "publish_status": "已成功发布",
        })
        return {"success": True, "message": "已发布", "mcp_result": result_str[:500]}
    else:
        cache_manager.update_task_status(draft_id, "error", {
            "message": f"发布失败: {result_str[:300]}",
        })
        raise HTTPException(status_code=500, detail=f"发布失败: {result_str[:300]}")


# 5 分钟内存缓存 — 避免前端轮询触发 xhs-mcp 重复 spawn Chromium
_LOGIN_CACHE: Dict[str, Any] = {"ts": 0.0, "key": "", "result": None}
_LOGIN_CACHE_TTL = 300  # 秒


@app.get("/api/account/status")
async def api_account_status(force: bool = False) -> Dict[str, Any]:
    """检查 xhs MCP 登录状态 (供 Studio header 显示账号绿点/红点).

    带 5 分钟缓存; force=true 强制重查.
    """
    import time as _time
    config = config_manager.load_config(for_display=False)
    xhs_url = config.get("xhs_mcp_url")
    if not xhs_url:
        return {"success": False, "logged_in": False, "message": "未配置 xhs_mcp_url"}

    cache_key = xhs_url
    now = _time.time()
    if (
        not force
        and _LOGIN_CACHE["result"]
        and _LOGIN_CACHE["key"] == cache_key
        and now - _LOGIN_CACHE["ts"] < _LOGIN_CACHE_TTL
    ):
        cached = dict(_LOGIN_CACHE["result"])
        cached["cached"] = True
        cached["age"] = int(now - _LOGIN_CACHE["ts"])
        return cached

    try:
        async with _fresh_xhs_session(xhs_url) as session:
            result = await asyncio.wait_for(session.call_tool("check_login_status", {}), timeout=15)
        text = _mcp_text(result)
        logged_in = ("已登录" in text) or ("✅" in text) or ("logged" in text.lower())
        username = ""
        for line in text.splitlines():
            if "用户名" in line or "username" in line.lower():
                username = line.split(":", 1)[-1].strip()
                break
        resp = {
            "success": True,
            "logged_in": logged_in,
            "username": username,
            "raw": text[:300],
        }
        _LOGIN_CACHE.update(ts=now, key=cache_key, result=resp)
        return resp
    except Exception as e:
        resp = {"success": False, "logged_in": False, "message": str(e)[:200]}
        # 失败也缓存 60 秒, 避免雪崩
        _LOGIN_CACHE.update(ts=now - (_LOGIN_CACHE_TTL - 60), key=cache_key, result=resp)
        return resp


@app.get("/api/status/{task_id}")
async def get_task_status(task_id: str) -> Dict[str, Any]:
    """获取任务状态（用于后续扩展异步任务）"""
    return {
        'success': True,
        'task_id': task_id,
        'status': 'completed',
        'progress': 100
    }


@app.get("/api/history")
async def get_task_history(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 100
) -> Dict[str, Any]:
    """获取任务历史记录"""
    try:
        tasks = cache_manager.get_tasks(
            start_date=start_date,
            end_date=end_date,
            status=status,
            limit=limit
        )
        return {
            'success': True,
            'data': tasks,
            'count': len(tasks)
        }
    except Exception as e:
        logger.error(f"获取历史记录失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/history/{task_id}")
async def delete_task_history(task_id: str) -> Dict[str, Any]:
    """删除指定的任务历史记录"""
    try:
        success = cache_manager.delete_task(task_id)
        if success:
            return {
                'success': True,
                'message': '任务已删除'
            }
        else:
            raise HTTPException(status_code=404, detail='任务不存在')
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"删除任务失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/history/statistics")
async def get_statistics() -> Dict[str, Any]:
    """获取任务统计信息"""
    try:
        stats = cache_manager.get_statistics()
        return {
            'success': True,
            'data': stats
        }
    except Exception as e:
        logger.error(f"获取统计信息失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


class FetchTrendingTopicsRequest(BaseModel):
    domain: str = ""


class FetchTopicsFromUrlRequest(BaseModel):
    url: str


@app.post("/api/fetch-trending-topics")
async def fetch_trending_topics(request_data: FetchTrendingTopicsRequest = None) -> Dict[str, Any]:
    """获取今日热点新闻主题"""
    try:
        # 检查配置是否完整
        config = config_manager.load_config()
        if not config.get('llm_api_key'):
            raise HTTPException(status_code=400, detail="请先完成配置")

        # 如果全局服务器未初始化,先初始化
        if not server_manager.is_initialized():
            logger.info("全局服务器未初始化,开始初始化...")
            await server_manager.initialize(config)

        # 获取领域参数
        domain = request_data.domain if request_data else ""

        # 创建内容生成器
        generator = ContentGenerator(config)

        # 获取热点主题(会自动使用全局服务器管理器)
        topics = await generator.fetch_trending_topics(domain=domain)

        if topics:
            return {
                'success': True,
                'topics': topics
            }
        else:
            raise HTTPException(status_code=500, detail='未能获取热点主题')

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"获取热点主题失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/fetch-topics-from-url")
async def fetch_topics_from_url(request_data: FetchTopicsFromUrlRequest) -> Dict[str, Any]:
    """从URL爬取内容并提取主题"""
    try:
        url = request_data.url

        if not url:
            raise HTTPException(status_code=400, detail="请提供URL")

        # 检查配置是否完整
        config = config_manager.load_config()
        if not config.get('llm_api_key'):
            raise HTTPException(status_code=400, detail="请先完成配置")

        # 如果全局服务器未初始化,先初始化
        if not server_manager.is_initialized():
            logger.info("全局服务器未初始化,开始初始化...")
            await server_manager.initialize(config)

        # 创建内容生成器
        generator = ContentGenerator(config)

        # 从URL提取主题(会自动使用全局服务器管理器)
        topics = await generator.fetch_topics_from_url(url)

        if topics:
            return {
                'success': True,
                'topics': topics
            }
        else:
            raise HTTPException(status_code=500, detail='未能从该URL提取主题')

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"从URL提取主题失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/batch-generate-and-publish")
async def batch_generate_and_publish(request_data: BatchGeneratePublishRequest) -> Dict[str, Any]:
    """批量生成内容并发布到小红书（并发控制：最多同时5个任务）"""
    try:
        topics = request_data.topics
        content_type = request_data.content_type

        if not topics or len(topics) == 0:
            raise HTTPException(status_code=400, detail="请选择至少一个主题")

        # 验证内容类型
        if content_type not in ["general", "paper_analysis"]:
            raise HTTPException(status_code=400, detail="内容类型必须是 'general' 或 'paper_analysis'")

        # 检查配置是否完整
        config = config_manager.load_config()
        if not config.get('llm_api_key') or not config.get('xhs_mcp_url'):
            raise HTTPException(status_code=400, detail="请先完成配置")

        # 创建信号量，限制最多同时运行5个任务
        semaphore = asyncio.Semaphore(5)

        async def process_single_topic(topic: str):
            """处理单个主题（带信号量控制）"""
            async with semaphore:
                try:
                    logger.info(f"开始处理主题: {topic}")

                    # 创建内容生成器
                    generator = ContentGenerator(config)

                    # 执行内容生成和发布
                    result = await generator.generate_and_publish(topic, content_type)

                    if result.get('success'):
                        response_data = {
                            'topic': topic,
                            'title': result.get('title', ''),
                            'content': result.get('content', ''),
                            'tags': result.get('tags', []),
                            'images': result.get('images', []),
                            'publish_status': result.get('publish_status', ''),
                            'publish_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                            'status': 'success'
                        }

                        # 保存到缓存
                        task_record = {
                            'topic': topic,
                            'status': 'success',
                            'progress': 100,
                            'message': '发布成功',
                            'content_type': content_type,
                            **response_data
                        }
                        cache_manager.add_task(task_record)

                        logger.info(f"主题处理成功: {topic}")
                        return response_data
                    else:
                        error_msg = result.get('error', '生成失败')

                        logger.error(f"主题处理失败: {topic} - {error_msg}")

                        # 保存失败记录到缓存
                        cache_manager.add_task({
                            'topic': topic,
                            'status': 'error',
                            'progress': 0,
                            'message': error_msg,
                            'content_type': content_type
                        })

                        return {
                            'topic': topic,
                            'status': 'error',
                            'error': error_msg
                        }

                except Exception as e:
                    logger.error(f"处理主题 '{topic}' 失败: {e}", exc_info=True)

                    # 保存失败记录到缓存
                    cache_manager.add_task({
                        'topic': topic,
                        'status': 'error',
                        'progress': 0,
                        'progress': 0,
                        'message': str(e),
                        'content_type': content_type
                    })

                    return {
                        'topic': topic,
                        'status': 'error',
                        'error': str(e)
                    }

        # 并发处理所有主题（最多同时5个）
        logger.info(f"开始批量处理 {len(topics)} 个主题，最多同时运行5个任务")
        results = await asyncio.gather(*[process_single_topic(topic) for topic in topics])

        # 统计结果
        success_count = sum(1 for r in results if r.get('status') == 'success')
        failed_count = len(results) - success_count

        return {
            'success': True,
            'message': f'批量处理完成：成功 {success_count} 个，失败 {failed_count} 个',
            'summary': {
                'total': len(topics),
                'success': success_count,
                'failed': failed_count
            },
            'results': results
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"批量生成和发布失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ====================================================================
# Multi-Agent Framework: Workflow + SSE + Specs CRUD
# ====================================================================

# 进程内 EventBus + Orchestrator 缓存 (按 config 指纹 invalidate)
_AGENT_EVENT_BUS = EventBus(max_history=2000)
_ORCH_CACHE: Dict[str, Orchestrator] = {}


def _orch_cache_key(config: Dict[str, Any]) -> str:
    """根据关键 config 字段生成 cache key. 任一变就重建."""
    return "|".join([
        config.get("xhs_mcp_url") or "",
        config.get("openai_base_url") or "",
        (config.get("default_model") or "")[:64],
        # api key 不入 key (避免泄露), 用 hash 末位区分
        str(hash(config.get("llm_api_key") or "") % 100000),
        str(DEFAULT_SPECS_PATH.stat().st_mtime if DEFAULT_SPECS_PATH.exists() else 0),
    ])


def _get_orchestrator() -> Orchestrator:
    """构造或返回缓存的 Orchestrator (整个 webapp 共享一份 EventBus)."""
    config = config_manager.load_config(for_display=False)
    if not config.get("llm_api_key") or not config.get("openai_base_url"):
        raise HTTPException(status_code=400, detail="请先在 配置 tab 填写 LLM API key / Base URL")
    if not config.get("xhs_mcp_url"):
        raise HTTPException(status_code=400, detail="请先在 配置 tab 填写 xhs_mcp_url")

    key = _orch_cache_key(config)
    if key in _ORCH_CACHE:
        return _ORCH_CACHE[key]

    loaded = load_agent_specs()
    # 图像生成走 OpenRouter 的 Seedream 4.5 (复用 LLM key 如果是 OpenRouter)
    base_url = config.get("openai_base_url") or ""
    image_key = (
        config.get("openrouter_api_key")
        or (config.get("llm_api_key") if "openrouter.ai" in base_url else None)
    )
    image_base = "https://openrouter.ai/api/v1"
    registry = build_default_registry(
        xhs_mcp_url=config["xhs_mcp_url"],
        tavily_api_key=config.get("tavily_api_key"),
        openrouter_api_key=image_key,
        image_base_url=image_base,
        image_model=config.get("image_model") or "bytedance-seed/seedream-4.5",
    )
    orch = Orchestrator(
        specs=loaded["specs"],
        registry=registry,
        llm_api_key=config["llm_api_key"],
        llm_base_url=config["openai_base_url"],
        default_model=config.get("default_model") or "anthropic/claude-sonnet-4.5",
        brand_prefix=loaded["brand_prefix"],
        event_bus=_AGENT_EVENT_BUS,
    )
    _ORCH_CACHE[key] = orch
    return orch


def _normalize_images_state(images_state: Any) -> Dict[str, Any]:
    """容错解析 cover_designer 的 images state.

    LLM 经常把 final output 包成 ```json ... ``` 字符串塞进来, 这里全部拍平成
    {"cover": {...}, "body": [...]} 的 dict.
    """
    if isinstance(images_state, dict):
        # 如果是 {"action": "final", "output": {...}} 的形式, 取里面的 output
        if "output" in images_state and isinstance(images_state["output"], dict):
            return images_state["output"]
        return images_state
    if isinstance(images_state, str):
        s = images_state.strip()
        # 去掉 markdown 代码围栏
        if s.startswith("```"):
            s = s.split("\n", 1)[1] if "\n" in s else s[3:]
            if s.endswith("```"):
                s = s[: -3]
            elif "```" in s:
                s = s.rsplit("```", 1)[0]
        try:
            import json as _json
            obj = _json.loads(s)
            if isinstance(obj, dict):
                if "output" in obj and isinstance(obj["output"], dict):
                    return obj["output"]
                return obj
        except Exception:
            return {}
    return {}


def _collect_image_paths(images_state: Any) -> List[str]:
    """从 cover_designer 输出的 images dict 中提取 cover + body 的本地路径列表."""
    paths: List[str] = []
    images_state = _normalize_images_state(images_state)
    if not isinstance(images_state, dict):
        return paths
    cover = images_state.get("cover")
    if isinstance(cover, dict) and cover.get("path"):
        paths.append(str(cover["path"]))
    elif isinstance(cover, str):
        paths.append(cover)
    body = images_state.get("body")
    if isinstance(body, list):
        for b in body:
            if isinstance(b, dict) and b.get("path"):
                paths.append(str(b["path"]))
            elif isinstance(b, str):
                paths.append(b)
    return paths


def _collect_image_urls(images_state: Any) -> List[str]:
    """同 _collect_image_paths, 但取 url 字段 (供前端预览)."""
    urls: List[str] = []
    images_state = _normalize_images_state(images_state)
    if not isinstance(images_state, dict):
        return urls
    cover = images_state.get("cover")
    if isinstance(cover, dict) and cover.get("url"):
        urls.append(str(cover["url"]))
    body = images_state.get("body")
    if isinstance(body, list):
        for b in body:
            if isinstance(b, dict) and b.get("url"):
                urls.append(str(b["url"]))
    return urls


def _save_workflow_draft(rec, workflow_id: str, inputs: Dict[str, Any]) -> Optional[str]:
    """把 workflow 跑完的 state 落到 draft 队列, 自动合并图片. 返回 task_id 或 None."""
    draft = rec.state.get("draft") or {}
    if not (draft and isinstance(draft, dict)):
        return None
    brief = rec.state.get("brief") or {}
    images_state = rec.state.get("images") or {}
    image_paths = _collect_image_paths(images_state)
    image_urls = _collect_image_urls(images_state)
    # 合并: 已有 draft.images (LLM 自填的) + cover_designer 生成的
    merged_images = list(draft.get("images") or [])
    for p in image_paths:
        if p and p not in merged_images:
            merged_images.append(p)

    topic = inputs.get("topic") or inputs.get("keyword") or "未知"
    payload = {
        "topic": topic,
        "title": draft.get("title") or "",
        "content": draft.get("content") or "",
        "tags": draft.get("tags") or [],
        "images": merged_images,
        "image_urls": image_urls,
        "fact_lines": draft.get("fact_lines") or [],
        "fact_citations": draft.get("fact_citations") or {},
        "subject": inputs.get("subject") or "",
        "status": "draft",
        "progress": 100,
        "message": f"workflow={workflow_id} run={rec.run_id} 自动入草稿"
                   + (f" (含 {len(image_paths)} 张图)" if image_paths else " (无图)"),
        "content_type": inputs.get("angle") or "soft_dry_goods",
        "brief": brief,
        "run_id": rec.run_id,
    }
    return cache_manager.add_task(payload)


class WorkflowRunRequest(BaseModel):
    workflow: str            # research_to_draft / quick_draft / rewrite
    inputs: Dict[str, Any]   # workflow 入参 (keyword/topic/subject/angle/...)
    save_as_draft: bool = True   # 跑完自动入 cache_manager 的 draft 队列


@app.get("/api/agents/workflows")
async def api_list_workflows() -> Dict[str, Any]:
    return {"success": True, "workflows": list_workflows()}


@app.get("/api/agents/specs")
async def api_get_specs() -> Dict[str, Any]:
    """返回当前 agent specs (供配置 tab 编辑)."""
    loaded = load_agent_specs()
    return {
        "success": True,
        "brand_prefix": loaded["brand_prefix"],
        "specs": [s.model_dump() for s in loaded["specs"]],
        "specs_path": str(DEFAULT_SPECS_PATH),
    }


class SaveSpecsRequest(BaseModel):
    specs: List[Dict[str, Any]]
    brand_prefix: Optional[str] = None


@app.post("/api/agents/specs")
async def api_save_specs(req: SaveSpecsRequest) -> Dict[str, Any]:
    """覆盖保存 agent specs (含可选 brand_prefix). 改完即生效 (orchestrator cache 会按 mtime 失效)."""
    try:
        specs = [AgentSpec(**d) for d in req.specs]
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"specs 校验失败: {e}")
    if not specs:
        raise HTTPException(status_code=400, detail="specs 不能为空")

    # 如果传了 brand_prefix, 写到 yaml
    from core.agents.config import save_agent_specs as _save
    import yaml as _yaml
    payload = {
        "version": 1,
        "brand_prefix": req.brand_prefix or load_agent_specs()["brand_prefix"],
        "agents": [s.model_dump() for s in specs],
    }
    DEFAULT_SPECS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(DEFAULT_SPECS_PATH, "w", encoding="utf-8") as f:
        _yaml.safe_dump(payload, f, allow_unicode=True, sort_keys=False, width=120)

    _ORCH_CACHE.clear()  # 全部重建
    return {"success": True, "n_specs": len(specs), "path": str(DEFAULT_SPECS_PATH)}


@app.post("/api/workflow/run")
async def api_workflow_run(req: WorkflowRunRequest) -> Dict[str, Any]:
    """启动一次 workflow, 异步在后台跑, 立刻返回 run_id (前端再订 SSE 看进度)."""
    try:
        wf = get_workflow(req.workflow)
    except KeyError as e:
        raise HTTPException(status_code=400, detail=str(e))

    orch = _get_orchestrator()
    ctx = orch.prepare_run(wf, req.inputs)
    run_id = ctx.run_id

    async def _run():
        try:
            rec = await orch.run_workflow(wf, req.inputs, ctx=ctx)
            if req.save_as_draft and rec.status == "completed":
                _save_workflow_draft(rec, req.workflow, req.inputs)
        except Exception as e:
            logger.exception(f"workflow {req.workflow} 后台执行异常: {e}")

    asyncio.create_task(_run())
    return {"success": True, "run_id": run_id, "workflow": req.workflow}


# ====================================================================
# Batch workflow — 一次提交 N 个选题, 用 Semaphore 限并发
# ====================================================================

class BatchRunRequest(BaseModel):
    workflow: str = "research_to_draft"
    items: List[Dict[str, Any]]            # [{topic, keyword, subject, angle, ...}, ...]
    max_parallel: int = 3
    save_as_draft: bool = True


# batch_id -> {workflow, items, run_ids, started_at, ended_at, status}
_BATCHES: Dict[str, Dict[str, Any]] = {}


@app.post("/api/workflow/batch/run")
async def api_workflow_batch_run(req: BatchRunRequest) -> Dict[str, Any]:
    """一次跑多个 workflow run. 立即返回 batch_id + run_ids 列表."""
    try:
        wf = get_workflow(req.workflow)
    except KeyError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not req.items:
        raise HTTPException(status_code=400, detail="items 不能为空")
    if len(req.items) > 50:
        raise HTTPException(status_code=400, detail="单批最多 50 个 (避免账号被限流)")

    orch = _get_orchestrator()

    # 立即给所有 item 预分配 run_id (前端可以马上订 SSE)
    contexts: List[Any] = []
    for it in req.items:
        # 兜底: 如果没传 keyword 但有 topic, 把 topic 当 keyword
        if "keyword" not in it and "topic" in it:
            it["keyword"] = it["topic"]
        contexts.append(orch.prepare_run(wf, it))

    batch_id = f"batch-{uuid.uuid4().hex[:10]}"
    run_ids = [ctx.run_id for ctx in contexts]
    _BATCHES[batch_id] = {
        "batch_id": batch_id,
        "workflow": req.workflow,
        "items": req.items,
        "run_ids": run_ids,
        "max_parallel": req.max_parallel,
        "started_at": time.time(),
        "ended_at": None,
        "status": "running",
        "draft_ids": [],
    }

    sem = asyncio.Semaphore(max(1, min(int(req.max_parallel), 10)))

    async def _one(ctx, item):
        async with sem:
            try:
                rec = await orch.run_workflow(wf, item, ctx=ctx)
                draft_id = None
                if req.save_as_draft and rec.status == "completed":
                    draft_id = _save_workflow_draft(rec, req.workflow, item)
                if draft_id:
                    _BATCHES[batch_id]["draft_ids"].append(draft_id)
                return rec
            except Exception as e:
                logger.exception(f"batch {batch_id} run {ctx.run_id} 异常: {e}")

    async def _drive():
        try:
            await asyncio.gather(
                *(_one(ctx, item) for ctx, item in zip(contexts, req.items)),
                return_exceptions=True,
            )
        finally:
            b = _BATCHES.get(batch_id)
            if b is not None:
                b["ended_at"] = time.time()
                b["status"] = "completed"

    asyncio.create_task(_drive())

    return {
        "success": True,
        "batch_id": batch_id,
        "n_items": len(req.items),
        "run_ids": run_ids,
        "max_parallel": sem._value if hasattr(sem, "_value") else req.max_parallel,
    }


@app.get("/api/workflow/batch/{batch_id}")
async def api_workflow_batch_status(batch_id: str) -> Dict[str, Any]:
    """聚合 batch 中各 run 的状态."""
    b = _BATCHES.get(batch_id)
    if not b:
        raise HTTPException(status_code=404, detail="batch 不存在 (可能 webapp 已重启)")
    orch = _get_orchestrator()
    runs = []
    n_done = n_running = n_failed = 0
    for rid in b["run_ids"]:
        rec = orch.get_record(rid)
        if rec is None:
            runs.append({"run_id": rid, "status": "missing"})
            continue
        d = rec.to_dict()
        # 聚合产物预览: title + 是否带图
        draft = (rec.state or {}).get("draft") or {}
        images_state = (rec.state or {}).get("images") or {}
        d["draft_title"] = draft.get("title") if isinstance(draft, dict) else None
        d["n_images"] = len(_collect_image_paths(images_state)) if images_state else 0
        d["critic_passed"] = (
            (rec.state or {}).get("critic_report", {}).get("passed")
            if isinstance(rec.state, dict) else None
        )
        runs.append(d)
        if rec.status == "completed":
            n_done += 1
        elif rec.status == "failed":
            n_failed += 1
        else:
            n_running += 1
    return {
        "success": True,
        "batch": {
            **{k: v for k, v in b.items() if k != "items"},
            "n_done": n_done,
            "n_running": n_running,
            "n_failed": n_failed,
            "n_total": len(b["run_ids"]),
        },
        "runs": runs,
    }


@app.get("/api/workflow/batches")
async def api_workflow_batches() -> Dict[str, Any]:
    items = []
    for b in sorted(_BATCHES.values(), key=lambda x: x["started_at"], reverse=True)[:30]:
        items.append({k: v for k, v in b.items() if k != "items"})
    return {"success": True, "batches": items}


@app.get("/api/workflow/run/{run_id}")
async def api_workflow_run_status(run_id: str) -> Dict[str, Any]:
    """查询 run 当前状态 + state 快照."""
    orch = _get_orchestrator()
    rec = orch.get_record(run_id)
    if not rec:
        raise HTTPException(status_code=404, detail="run 不存在 (可能 webapp 已重启)")
    return {
        "success": True,
        "record": rec.to_dict(),
        "state": rec.state,
    }


@app.get("/api/workflow/runs")
async def api_workflow_runs(limit: int = 50) -> Dict[str, Any]:
    orch = _get_orchestrator()
    return {
        "success": True,
        "runs": [r.to_dict() for r in orch.list_records(limit=limit)],
    }


@app.get("/api/workflow/stream/{run_id}")
async def api_workflow_stream(run_id: str):
    """Server-Sent Events: 实时推 agent 事件 (供前端 timeline 显示)."""
    from fastapi.responses import StreamingResponse
    import json as _json

    async def event_generator():
        bus = _AGENT_EVENT_BUS
        q = await bus.subscribe(run_id)
        try:
            # 先把已有 history 发完 (subscribe 已 enqueue)
            while True:
                ev: AgentEvent = await asyncio.wait_for(q.get(), timeout=300)
                payload = _json.dumps(ev.model_dump(), ensure_ascii=False, default=str)
                yield f"event: {ev.type.value}\ndata: {payload}\n\n"
                if ev.type.value in ("run_completed", "run_failed"):
                    # 再 sleep 一秒让后续事件 flush, 然后关流
                    await asyncio.sleep(1.0)
                    return
        except asyncio.TimeoutError:
            yield "event: timeout\ndata: {}\n\n"
        finally:
            bus.unsubscribe(run_id, q)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",     # nginx
            "Connection": "keep-alive",
        },
    )


# 挂载静态文件 - 必须在所有路由定义之后
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")

# 挂载 cache/images 让前端可以预览生成的封面/正文图
_CACHE_IMG_DIR = os.path.join(BASE_DIR, "cache", "images")
os.makedirs(_CACHE_IMG_DIR, exist_ok=True)
app.mount("/cache/images", StaticFiles(directory=_CACHE_IMG_DIR), name="cache_images")


if __name__ == '__main__':
    import uvicorn
    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=8080,
        reload=True,
        log_level="info"
    )