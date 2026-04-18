"""
内容生成器模块
基于原有的RealToolExecutor重构，用于生成和发布小红书内容
"""
import json
import logging
import os
import tempfile
import shutil
import asyncio
import httpx
from typing import Any, Dict, List, Optional
from core.xhs_llm_client import Configuration, Server, LLMClient, Tool
from core.server_manager import server_manager

logger = logging.getLogger(__name__)


class TavilyQuotaExceeded(Exception):
    """Tavily API配额已用尽异常"""
    pass


class ContentGenerator:
    """内容生成器 - 负责生成小红书内容并发布"""

    def __init__(self, config: Dict[str, Any]):
        """初始化内容生成器

        Args:
            config: 应用配置字典
        """
        self.config = config
        self.servers = []
        self.llm_client = None
        self.context = None
        self.context_file = None
        self._owns_context_file = False
        self.dry_run = False
        self.captured_publish_args: Optional[Dict[str, Any]] = None

        # 初始化Configuration
        self.mcp_config = self._create_mcp_config()

    def _create_mcp_config(self) -> Configuration:
        """创建MCP配置对象"""
        # 临时设置环境变量供Configuration使用
        os.environ['LLM_API_KEY'] = self.config.get('llm_api_key', '')
        os.environ['OPENAI_BASE_URL'] = self.config.get('openai_base_url', '')
        os.environ['DEFAULT_MODEL'] = self.config.get('default_model', 'claude-sonnet-4-20250514')

        return Configuration()

    def _prepare_context_file(self, context_file: Optional[str] = None) -> tuple[str, bool]:
        """准备上下文文件"""
        if context_file:
            return context_file, False

        # 使用原项目的模板文件
        script_dir = str(parent_dir)
        template_candidates = [
            os.path.join(script_dir, "agent_context_temple.xml"),
            os.path.join(script_dir, "agent_context.xml"),
        ]

        template_path = None
        for candidate in template_candidates:
            if os.path.exists(candidate):
                template_path = candidate
                break

        if template_path is None:
            raise FileNotFoundError("未找到agent context XML模板文件")

        # 创建临时目录
        temp_dir = tempfile.gettempdir()
        fd, temp_path = tempfile.mkstemp(prefix="agent_context_", suffix=".xml", dir=temp_dir)
        os.close(fd)

        try:
            shutil.copyfile(template_path, temp_path)
        except Exception:
            try:
                os.remove(temp_path)
            except OSError:
                pass
            raise

        return temp_path, True

    async def validate_image_urls(self, image_urls: List[str], timeout: float = 20.0) -> List[str]:
        """验证图片 URL 的有效性,返回可访问的图片 URL 列表

        Args:
            image_urls: 待验证的图片 URL 列表
            timeout: 每个 URL 的超时时间(秒，默认20s)

        Returns:
            List[str]: 有效的图片 URL 列表
        """
        if not image_urls:
            return []

        valid_urls = []

        async def check_url(url: str) -> Optional[str]:
            """检查单个 URL 是否可访问且为图片，支持重试和多种验证方法"""
            # 跳过明显无效的 URL
            if not url or not url.startswith(('http://', 'https://')):
                logger.warning(f"跳过无效URL格式: {url}")
                return None

            # 检查是否为占位符
            if any(placeholder in url.lower() for placeholder in ['example.com', 'placeholder', 'image1.jpg', 'image2.jpg', 'image3.jpg', 'test.jpg']):
                logger.warning(f"跳过占位符URL: {url}")
                return None

            # 重试机制：最多尝试2次
            for attempt in range(2):
                try:
                    # 判断是否需要禁用SSL验证（针对已知有证书问题的CDN）
                    verify_ssl = True
                    # 已知证书问题的域名列表
                    problematic_domains = ['9to5google.com', 'techkv.com', 'cdn.example.com']
                    if any(domain in url for domain in problematic_domains):
                        verify_ssl = False
                        logger.info(f"对已知证书问题域名禁用SSL验证: {url}")

                    # 更完善的User-Agent，模拟真实浏览器
                    headers = {
                        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                        'Accept': 'image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8',
                        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
                        'Referer': 'https://www.google.com/'
                    }

                    async with httpx.AsyncClient(
                        timeout=timeout,
                        follow_redirects=True,
                        verify=verify_ssl
                    ) as client:
                        # 首先尝试 HEAD 请求
                        try:
                            response = await client.head(url, headers=headers)

                            # 检查状态码
                            if response.status_code == 200:
                                # 检查 Content-Type
                                content_type = response.headers.get('content-type', '').lower()
                                if content_type.startswith('image/'):
                                    logger.info(f"✓ 图片URL有效(HEAD): {url}")
                                    return url
                                else:
                                    logger.warning(f"URL不是图片类型(HEAD) (Content-Type: {content_type}): {url}")

                            # 如果HEAD失败，尝试GET请求（只获取少量字节）
                            elif response.status_code in [403, 405, 404]:
                                logger.info(f"HEAD请求失败(状态码{response.status_code})，尝试GET请求: {url}")
                                raise httpx.HTTPStatusError(f"HEAD failed with {response.status_code}", request=None, response=response)
                            else:
                                logger.warning(f"图片URL返回非200状态码 {response.status_code}: {url}")

                        except (httpx.HTTPStatusError, httpx.RequestError):
                            # HEAD失败，尝试GET请求（只读取前1KB来验证）
                            logger.info(f"尝试GET请求验证(前1KB): {url}")
                            headers['Range'] = 'bytes=0-1023'  # 只请求前1KB

                            response = await client.get(url, headers=headers)

                            if response.status_code in [200, 206]:  # 206 = Partial Content
                                # 检查 Content-Type
                                content_type = response.headers.get('content-type', '').lower()
                                if content_type.startswith('image/'):
                                    logger.info(f"✓ 图片URL有效(GET): {url}")
                                    return url
                                else:
                                    # 即使Content-Type不对，如果URL看起来像图片，也接受
                                    image_extensions = ['.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.svg', '.ico']
                                    if any(url.lower().endswith(ext) or ext in url.lower() for ext in image_extensions):
                                        logger.info(f"✓ 图片URL有效(按扩展名): {url}")
                                        return url
                                    logger.warning(f"URL不是图片类型(GET) (Content-Type: {content_type}): {url}")
                            else:
                                logger.warning(f"图片URL返回非200/206状态码 {response.status_code}: {url}")

                    # 如果到这里都没返回，说明验证失败，进入重试
                    if attempt < 1:  # 还有重试机会
                        await asyncio.sleep(1 * (attempt + 1))  # 指数退避: 1s, 2s
                        logger.info(f"重试验证URL (第{attempt + 2}次): {url}")
                        continue
                    else:
                        return None

                except httpx.TimeoutException:
                    if attempt < 1:
                        logger.warning(f"图片URL访问超时(第{attempt + 1}次)，准备重试: {url}")
                        await asyncio.sleep(1 * (attempt + 1))
                        continue
                    else:
                        logger.warning(f"图片URL访问超时(已重试): {url}")
                        return None
                except Exception as e:
                    if attempt < 1:
                        logger.warning(f"图片URL验证失败(第{attempt + 1}次) {url}: {e}，准备重试")
                        await asyncio.sleep(1 * (attempt + 1))
                        continue
                    else:
                        logger.warning(f"图片URL验证失败(已重试) {url}: {e}")
                        return None

            return None

        # 并发检查所有 URL
        tasks = [check_url(url) for url in image_urls]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # 收集有效的 URL
        for result in results:
            if isinstance(result, str) and result:
                valid_urls.append(result)

        logger.info(f"图片URL验证完成: {len(valid_urls)}/{len(image_urls)} 个有效")
        return valid_urls

    def get_research_plan(self, user_topic: str, content_type: str = "general") -> List[Dict[str, Any]]:
        """根据用户主题和内容类型生成研究计划"""

        if content_type == "paper_analysis":
            return self.get_paper_analysis_plan(user_topic)
        return [
            {
                "id": "step1",
                "title": f"针对「{user_topic}」主题信息检索",
                "description": (
                    f"1. 使用网络搜索工具，专门检索与「{user_topic}」相关的最新信息（过去7-30天内）。\n"
                    f"2. 重点搜索关键词：{user_topic}、相关技术名词、主要厂商动态。\n"
                    f"3. 收集权威来源的文章，包括：官方发布、技术博客、新闻报道、研究论文等。\n"
                    f"4. 每条信息必须包含：标题、摘要、发布时间、来源链接、相关的真实图片链接。\n"
                    f"5. 筛选出5-8条最新、最有价值的信息，为深度分析做准备。"
                    f"6. 必须检索出与「{user_topic}」相关3-4张图片，并且要保障这个图片是真实存在的网络图片链接（HTTPS地址）"
                ),
                "depends on": []
            },
            {
                "id": "step2",
                "title": f"撰写「{user_topic}」专题文章",
                "description": (
                    f"1. 基于前面的分析，撰写一篇关于「{user_topic}」的专业文章：\n"
                    f"   - 标题可以夸张的手法来描述（≤20字）标题要有吸引力和话题性\n"
                    f"   - 开头吸引眼球，快速切入主题\n"
                    f"   - 正文逻辑清晰：背景→核心技术→应用价值→发展趋势，适当使用emoji表情符号增加趣味性\n"
                    f"   - 禁止: 避免 AI 腔调、模板化表达和'姐妹们'等传统词藻\n"
                    f"   - 结合具体数据、案例和专家观点增强可信度\n"
                    f"   - 语言通俗易懂，避免过于技术化的表述，使用年轻化、亲切的语言风格\n"
                    f"2. 文章长度控制在800-1200字，适合社交媒体阅读。\n"
                    f"3. 准备3-4张高质量配图，必须是真实的网络图片链接（HTTPS地址）。"
                ),
                "depends on": ["step2"]
            },
            {
                "id": "step3",
                "title": "小红书格式适配与发布",
                "description": (
                    "1. 将文章调整为适合小红书的格式：\n"
                    "   - 标题控制在20字以内，突出亮点和价值，如果是「论文分享要保留这几个字」\n"
                    "   - 正文移除所有#开头的标签，改为自然语言表达，正文不超过1000字, 禁止使用“#”\n"
                    "   - 提取5个精准的话题标签到tags数组\n"
                    "   - 确保提供3-4张图片，所有链接都是内容为图片的可访问的HTTPS地址\n"
                    "2. 整理成标准的JSON格式（仅在内部使用，不输出）：\n"
                    "   {\n"
                    "     \"title\": \"吸引人的标题（20字以内）\",\n"
                    "     \"content\": \"正文内容（800-1000字，包含emoji和相关链接）\",\n"
                    "     \"images\": [\n"
                    "       \"https://example.com/image1.jpg\",\n"
                    "       \"https://example.com/image2.jpg\",\n"
                    "       \"https://example.com/image3.jpg\"\n"
                    "     ],\n"
                    "     \"tags\": [\"标签1\", \"标签2\", \"标签3\", \"标签4\", \"标签5\"]\n"
                    "   }\n"
                    "3. 验证内容的完整性和格式的正确性，确保符合发布要求。\n"
                    "4. 直接使用publish_content工具发布到小红书：\n"
                    "   - 使用整理好的title、content、images、tags参数\n"
                    "   - 一次性完成格式化和发布操作\n"
                    "**注意**: 前面的步骤已经完成了详细的信息收集，这一步只需要整理格式并直接发布即可，不需要做额外的查询工作"
                ),
                "depends on": ["step1", "step2"]
            }
        ]

    async def initialize_servers(self):
        """初始化MCP服务器连接"""
        try:
            # 动态构建服务器配置（使用 self.config，不从文件读取）
            server_config = {
                "mcpServers": {
                    "jina-mcp-tools": {
                        "args": ["jina-mcp-tools"],
                        "command": "npx",
                        "env": {
                            "JINA_API_KEY": self.config.get('jina_api_key', '')
                        }
                    },
                    "tavily-remote": {
                        "command": "npx",
                        "args": [
                            "-y",
                            "mcp-remote",
                            f"https://mcp.tavily.com/mcp/?tavilyApiKey={self.config.get('tavily_api_key', '')}"
                        ]
                    },
                    "xhs": {
                        "type": "streamable_http",
                        "url": self.config.get('xhs_mcp_url', 'http://localhost:18060/mcp')
                    }
                }
            }

            # 创建服务器实例
            self.servers = [
                Server(name, srv_config)
                for name, srv_config in server_config["mcpServers"].items()
            ]

            # 初始化LLM客户端
            self.llm_client = LLMClient(
                self.config.get('llm_api_key'),
                self.config.get('openai_base_url'),
                self.config.get('default_model', 'claude-sonnet-4-20250514')
            )

            # 初始化所有服务器
            for server in self.servers:
                try:
                    await server.initialize()
                    logger.info(f"成功初始化服务器: {server.name}")
                except Exception as e:
                    logger.error(f"初始化服务器 {server.name} 失败: {e}")

        except Exception as e:
            logger.error(f"初始化服务器失败: {e}")
            raise

    async def get_available_tools(self) -> List[Tool]:
        """获取所有可用的工具"""
        all_tools = []
        for server in self.servers:
            try:
                tools = await server.list_tools()
                all_tools.extend(tools)
                logger.info(f"服务器 {server.name} 提供 {len(tools)} 个工具")
            except Exception as e:
                logger.error(f"从服务器 {server.name} 获取工具失败: {e}")

        return all_tools

    async def fetch_trending_topics(self, domain: str = "") -> List[Dict[str, str]]:
        """获取今日热点新闻主题

        Args:
            domain: 指定的领域（如：AI、融资、论文、机器人等）

        Returns:
            List[Dict[str, str]]: 热点主题列表，每个主题包含 title 和 summary
        """
        try:
            logger.info(f"开始获取今日热点新闻主题{f'（{domain}领域）' if domain else ''}...")

            # 优先使用全局服务器管理器
            if server_manager.is_initialized():
                logger.info("使用全局服务器管理器")
                self.servers = server_manager.get_servers()
                self.llm_client = server_manager.get_llm_client()
                available_tools = await server_manager.get_available_tools()
            else:
                logger.info("全局服务器未初始化,使用本地获取")
                # 获取可用工具
                available_tools = await self.get_available_tools()

            if not available_tools:
                logger.error("没有可用的工具")
                return []

            # 将工具转换为OpenAI格式
            openai_tools = [tool.to_openai_tool() for tool in available_tools]

            # 获取当前时间
            from datetime import datetime, timezone, timedelta
            # 使用中国时区 (UTC+8)
            china_tz = timezone(timedelta(hours=8))
            current_time = datetime.now(china_tz)
            current_date_str = current_time.strftime('%Y年%m月%d日')
            current_datetime_str = current_time.strftime('%Y年%m月%d日 %H:%M')

            logger.info(f"当前时间: {current_datetime_str}")

            # 根据是否指定领域构建不同的提示词
            if domain:
                # 构建针对不同领域的搜索策略
                domain_search_config = {
                    "AI": {
                        "keywords": ["AI", "人工智能", "大模型", "深度学习", "机器学习", "AGI"],
                        "focus": "AI技术突破、AI应用、AI公司动态"
                    },
                    "融资": {
                        "keywords": ["AI融资", "人工智能投资", "AI公司融资", "AI领域投资"],
                        "focus": "AI领域的融资事件、投资动态、AI初创公司"
                    },
                    "论文": {
                        "keywords": ["arXiv AI论文", "arXiv 人工智能", "arXiv machine learning", "arXiv deep learning", "最新AI论文"],
                        "focus": "arXiv上AI领域的最新学术论文、研究成果、技术创新"
                    },
                    "机器人": {
                        "keywords": ["AI机器人", "智能机器人", "机器人技术", "人形机器人", "工业机器人"],
                        "focus": "AI驱动的机器人技术、机器人应用、机器人公司动态"
                    }
                }

                # 获取领域配置,如果没有则使用通用AI搜索
                config = domain_search_config.get(domain, {
                    "keywords": [f"AI {domain}", f"人工智能 {domain}"],
                    "focus": f"AI {domain}领域的最新动态"
                })

                keywords_str = "、".join(config["keywords"])

                system_prompt = f"""你是一个专业的AI行业新闻分析师，擅长发现和总结AI领域的热点话题。

【当前时间】{current_datetime_str}

【领域定位】「{domain}」是人工智能(AI)大领域下的一个重要分支

请使用网络搜索工具查找「{domain}」在过去24小时内（{current_date_str}）最热门的新闻话题。

**搜索范围**：
- 主题：{config["focus"]}
- 关键词：{keywords_str}
- 时间：{current_date_str}（最近24小时）

**搜索要求**：
1. 必须使用搜索工具获取最新信息
2. 关注AI领域的{domain}相关内容
3. 优先选择{current_date_str}发布的权威内容
4. 确保信息的准确性和时效性
"""

                # 针对论文领域的特殊提示
                if domain == "论文":
                    user_prompt = f"""请搜索并列出arXiv上{current_date_str}最新发布的10篇AI相关论文。

**搜索策略**：
- 推荐关键词：{keywords_str}
- 可以组合搜索：如"{config['keywords'][0]} {current_date_str}"、"arXiv AI 最新论文"
- **重点**：优先搜索 arxiv.org 网站上的最新论文
- 关注分类：cs.AI, cs.LG, cs.CV, cs.CL, cs.RO 等AI相关类别

**信息来源**：
- 主要来源：调用搜索工具搜索网页(https://arxiv.org/search/?query=llm&searchtype=all&abstracts=show&order=-announced_date_first&size=50)
- 辅助来源：Papers with Code、AI科技媒体对论文的报道

**内容要求**：
对于每篇论文，请提供：
1. 论文标题（15-20字,可以简化）
2. 简短的研究摘要（30-50字,重点说明创新点和应用价值）

请确保这些论文都是{current_date_str}或最近几天在arXiv上发布的最新研究，与AI领域密切相关，有学术价值和实用性，适合在社交媒体上创作科普内容。

搜索完成后，请按照以下JSON格式整理结果（注意：你的最终回复必须是纯JSON格式，不要包含任何其他文字）：
```json
[
  {{
    "title": "论文标题",
    "summary": "论文摘要"
  }}
]
```
"""
                else:
                    user_prompt = f"""请搜索并列出「{domain}」在{current_date_str}最热门的10个新闻话题。

**搜索策略**：
- 推荐关键词：{keywords_str}
- 可以组合搜索：如"{config['keywords'][0]} {current_date_str}"、"{config['keywords'][0]} 今日"
- 信息来源：
  * AI领域：机器之心、量子位、新智元、AI科技评论
  * 融资领域：36氪、投资界、创业邦、IT桔子
  * 机器人领域：机器人大讲堂、机器人在线、IEEE Robotics

**内容要求**：
对于每个话题，请提供：
1. 简洁的标题（15-20字）
2. 简短的摘要说明（30-50字）

请确保这些话题都是{current_date_str}的最新内容、与AI {domain}密切相关、有热度的，适合在社交媒体上创作内容。

搜索完成后，请按照以下JSON格式整理结果（注意：你的最终回复必须是纯JSON格式，不要包含任何其他文字）：
```json
[
  {{
    "title": "话题标题",
    "summary": "话题摘要"
  }}
]
```
"""
            else:
                system_prompt = f"""你是一个专业的新闻分析师，擅长发现和总结当前的热点话题。

【当前时间】{current_datetime_str}

请使用网络搜索工具查找过去24小时内（{current_date_str}）最热门的新闻话题。
重点关注：科技、AI、互联网、社交媒体等领域的热点新闻。

**搜索要求**：
1. 必须使用搜索工具获取最新信息
2. 关注时效性，优先选择{current_date_str}发布的内容
3. 确保信息的准确性和可靠性
"""

                user_prompt = f"""请搜索并列出{current_date_str}最热门的10个新闻话题。

**搜索指引**：
- 搜索关键词示例："今日热点", "最新新闻 {current_date_str}", "科技新闻"
- 时间范围：过去24小时内
- 信息来源：主流媒体、科技媒体、官方发布

对于每个话题，请提供：
1. 简洁的标题（15-20字）
2. 简短的摘要说明（30-50字）

请确保这些话题都是{current_date_str}的最新内容、有热度的，适合在社交媒体上创作内容。

搜索完成后，请按照以下JSON格式整理结果（注意：你的最终回复必须是纯JSON格式，不要包含任何其他文字）：
```json
[
  {
    "title": "话题标题",
    "summary": "话题摘要"
  }
]
```
"""

            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ]

            # 进行多轮工具调用
            max_iterations = 5
            iteration = 0

            while iteration < max_iterations:
                iteration += 1
                logger.info(f"热点主题检索 - 第 {iteration} 轮")

                # 获取工具调用响应
                response = self.llm_client.get_tool_call_response(messages, openai_tools)
                message = response.choices[0].message

                if message.tool_calls:
                    # 添加助手消息
                    assistant_msg = {
                        "role": "assistant",
                        "content": message.content or "",
                        "tool_calls": [
                            {
                                "id": tc.id,
                                "type": "function",
                                "function": {
                                    "name": tc.function.name,
                                    "arguments": tc.function.arguments
                                }
                            }
                            for tc in message.tool_calls
                        ]
                    }
                    messages.append(assistant_msg)

                    # 执行所有工具调用
                    for tool_call in message.tool_calls:
                        tool_name = tool_call.function.name
                        try:
                            arguments = json.loads(tool_call.function.arguments) if tool_call.function.arguments else {}
                        except json.JSONDecodeError:
                            arguments = {}

                        logger.info(f"执行工具: {tool_name}")

                        # 查找对应的服务器并执行工具
                        tool_result = None
                        for server in self.servers:
                            tools = await server.list_tools()
                            if any(tool.name == tool_name for tool in tools):
                                try:
                                    tool_result = await server.execute_tool(tool_name, arguments)
                                    break
                                except Exception as e:
                                    logger.error(f"执行工具 {tool_name} 出错: {e}")
                                    tool_result = f"Error: {str(e)}"

                        if tool_result is None:
                            tool_result = f"未找到工具 {tool_name}"

                        # 添加工具结果消息
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": str(tool_result)
                        })

                    # 获取最终响应
                    final_response = self.llm_client.get_final_response(messages, openai_tools)
                    final_message = final_response.choices[0].message

                    if final_message.tool_calls:
                        # 继续下一轮
                        response = final_response
                    else:
                        # 获取最终内容并解析
                        final_content = final_message.content or ""
                        logger.info("热点主题检索完成，开始解析结果")

                        # 尝试从返回内容中提取JSON
                        topics = self._parse_topics_from_response(final_content)
                        return topics
                else:
                    # 没有工具调用，直接返回内容
                    final_content = message.content or ""
                    topics = self._parse_topics_from_response(final_content)
                    return topics

            logger.warning("达到最大迭代次数，未能完成热点主题检索")
            return []

        except Exception as e:
            # 检查是否是Tavily API错误
            error_str = str(e).lower()
            if "429" in error_str or "quota" in error_str or "unauthorized" in error_str or "403" in error_str:
                logger.warning(f"检测到Tavily API可能受限: {e}，尝试轮换Key...")
                if await server_manager.rotate_tavily_key():
                    logger.info("Key轮换成功，重试获取热点主题...")
                    # 递归重试一次
                    return await self.fetch_trending_topics(domain)
            
            logger.error(f"获取热点主题失败: {e}", exc_info=True)
            return []

    def _parse_topics_from_response(self, content: str) -> List[Dict[str, str]]:
        """从LLM响应中解析主题列表

        Args:
            content: LLM返回的内容

        Returns:
            解析出的主题列表
        """
        try:
            # 尝试直接解析JSON
            import re

            # 查找JSON代码块
            json_match = re.search(r'```json\s*([\s\S]*?)\s*```', content)
            if json_match:
                json_str = json_match.group(1)
            else:
                # 查找数组格式的JSON
                json_match = re.search(r'\[\s*{[\s\S]*}\s*\]', content)
                if json_match:
                    json_str = json_match.group(0)
                else:
                    json_str = content

            topics = json.loads(json_str)

            if isinstance(topics, list):
                # 验证每个主题的格式
                valid_topics = []
                for topic in topics:
                    if isinstance(topic, dict) and 'title' in topic:
                        valid_topics.append({
                            'title': topic.get('title', ''),
                            'summary': topic.get('summary', '')
                        })

                logger.info(f"成功解析出 {len(valid_topics)} 个热点主题")
                return valid_topics[:20]  # 限制返回20个

        except json.JSONDecodeError as e:
            logger.error(f"解析JSON失败: {e}")
        except Exception as e:
            logger.error(f"解析主题失败: {e}")

        return []

    async def fetch_topics_from_url(self, url: str) -> List[Dict[str, str]]:
        """从URL爬取内容并提取主题

        Args:
            url: 要爬取的网页URL

        Returns:
            List[Dict[str, str]]: 提取的主题列表，每个主题包含 title 和 summary
        """
        try:
            logger.info(f"开始从URL提取主题: {url}")

            # 优先使用全局服务器管理器
            if server_manager.is_initialized():
                logger.info("使用全局服务器管理器")
                self.servers = server_manager.get_servers()
                self.llm_client = server_manager.get_llm_client()
                available_tools = await server_manager.get_available_tools()
            else:
                logger.info("全局服务器未初始化,使用本地获取")
                # 获取可用工具
                available_tools = await self.get_available_tools()

            if not available_tools:
                logger.error("没有可用的工具")
                return []

            # 将工具转换为OpenAI格式
            openai_tools = [tool.to_openai_tool() for tool in available_tools]

            # 构建提示词
            system_prompt = """你是一个专业的内容分析师，擅长从网页内容中提取有价值的主题。
            请使用网络爬取工具访问指定的URL，读取页面内容，然后分析提取出其中最有价值的主题。
            """

            user_prompt = f"""请访问以下网页并提取其中最有价值的20个主题：

            URL: {url}

            对于每个主题，请提供：
            1. 简洁的标题（15-20字）
            2. 简短的摘要说明（30-50字）

            请确保提取的主题具有独立性，适合作为社交媒体内容创作的选题。

            提取完成后，请按照以下JSON格式整理结果（注意：你的最终回复必须是纯JSON格式，不要包含任何其他文字）：
            ```json
            [
              {{
                "title": "话题标题",
                "summary": "话题摘要"
              }}
            ]
            ```
            """

            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ]

            # 进行多轮工具调用
            max_iterations = 5
            iteration = 0

            while iteration < max_iterations:
                iteration += 1
                logger.info(f"URL内容提取 - 第 {iteration} 轮")

                # 获取工具调用响应
                response = self.llm_client.get_tool_call_response(messages, openai_tools)
                message = response.choices[0].message

                if message.tool_calls:
                    # 添加助手消息
                    assistant_msg = {
                        "role": "assistant",
                        "content": message.content or "",
                        "tool_calls": [
                            {
                                "id": tc.id,
                                "type": "function",
                                "function": {
                                    "name": tc.function.name,
                                    "arguments": tc.function.arguments
                                }
                            }
                            for tc in message.tool_calls
                        ]
                    }
                    messages.append(assistant_msg)

                    # 执行所有工具调用
                    for tool_call in message.tool_calls:
                        tool_name = tool_call.function.name
                        try:
                            arguments = json.loads(tool_call.function.arguments) if tool_call.function.arguments else {}
                        except json.JSONDecodeError:
                            arguments = {}

                        logger.info(f"执行工具: {tool_name}")

                        # 查找对应的服务器并执行工具
                        tool_result = None
                        for server in self.servers:
                            tools = await server.list_tools()
                            if any(tool.name == tool_name for tool in tools):
                                try:
                                    tool_result = await server.execute_tool(tool_name, arguments)
                                    break
                                except Exception as e:
                                    logger.error(f"执行工具 {tool_name} 出错: {e}")
                                    tool_result = f"Error: {str(e)}"

                        if tool_result is None:
                            tool_result = f"未找到工具 {tool_name}"

                        # 添加工具结果消息
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": str(tool_result)
                        })

                    # 获取最终响应
                    final_response = self.llm_client.get_final_response(messages, openai_tools)
                    final_message = final_response.choices[0].message

                    if final_message.tool_calls:
                        # 继续下一轮
                        response = final_response
                    else:
                        # 获取最终内容并解析
                        final_content = final_message.content or ""
                        logger.info("URL内容提取完成，开始解析结果")

                        # 尝试从返回内容中提取JSON
                        topics = self._parse_topics_from_response(final_content)
                        return topics
                else:
                    # 没有工具调用，直接返回内容
                    final_content = message.content or ""
                    topics = self._parse_topics_from_response(final_content)
                    return topics

            logger.warning("达到最大迭代次数，未能完成URL内容提取")
            return []

            logger.warning("达到最大迭代次数，未能完成URL内容提取")
            return []

        except Exception as e:
            # 检查是否是Tavily API错误
            error_str = str(e).lower()
            if "429" in error_str or "quota" in error_str or "unauthorized" in error_str or "403" in error_str:
                logger.warning(f"检测到Tavily API可能受限: {e}，尝试轮换Key...")
                if await server_manager.rotate_tavily_key():
                    logger.info("Key轮换成功，重试URL内容提取...")
                    # 递归重试一次
                    return await self.fetch_topics_from_url(url)

            logger.error(f"从URL提取主题失败: {e}", exc_info=True)
            return []

    async def execute_step(self, step: Dict[str, Any], available_tools: List[Tool],
                          previous_results: List[Dict[str, Any]], user_topic: str) -> Dict[str, Any]:
        """执行单个步骤

        Args:
            step: 步骤配置
            available_tools: 可用工具列表
            previous_results: 之前步骤的结果
            user_topic: 用户输入的主题

        Returns:
            步骤执行结果
        """
        logger.info(f"执行步骤: {step['id']} - {step['title']}")

        # 将工具转换为OpenAI格式
        openai_tools = [tool.to_openai_tool() for tool in available_tools] if available_tools else None

        system_prompt = f"""你是一个专业的小红书内容创作专家，专门研究「{user_topic}」相关的最新发展。请根据任务背景、之前步骤的执行结果和当前步骤要求选择并调用相应的工具。
        【研究主题】
        核心主题: {user_topic}
        研究目标: 收集、分析并撰写关于「{user_topic}」的专业内容，最终发布到小红书平台
        
        【小红书文案要求】
        🎯 吸引力要素：
        - 使用引人注目的标题，包含热门话题标签和表情符号
        - 开头要有强烈的钩子，激发用户好奇心和共鸣
        - 内容要实用且有价值，让用户有收藏和分享的冲动
        - 语言要轻松活泼，贴近年轻用户的表达习惯
        - 结尾要有互动引导，如提问、征集意见等
        - 适当使用流行梗和网络用语，但保持专业度
        
        【任务背景】
        目标: f'深度研究{user_topic}并生成高质量的社交媒体内容'
        要求: 确保内容专业准确、提供3-4张真实可访问的图片、格式符合小红书发布标准，最好不要有水印，避免侵权的威胁
        
        【当前步骤】
        步骤ID: {step['id']}
        步骤标题: {step['title']}
        """

        # 根据是否有前置结果添加不同的执行指导
        if previous_results:
            system_prompt += "\n【前序步骤执行结果】\n"
            for result in previous_results:
                if result.get('response'):
                    response_preview = result['response'][:1000]  # 限制长度
                    system_prompt += f"▸ {result['step_id']} - {result['step_title']}：\n"
                    system_prompt += f"{response_preview}...\n\n"

            system_prompt += """【执行指南】
                1. 仔细理解前序步骤已获得的信息和资源
                2. 基于已有结果，确定当前步骤需要调用的工具
                3. 充分利用前序步骤的数据，避免重复工作
                4. 如需多个工具协同，可同时调用
                5. 确保当前步骤输出能无缝衔接到下一步骤
                
                ⚠️ 重要提示：
                - 如果前序步骤已提供足够信息，直接整合利用，不要重复检索
                - 如果是内容创作步骤，基于前面的素材直接撰写
                - 如果是发布步骤，直接提取格式化内容进行发布
                """
        else:
            system_prompt += """【执行指南】
            1. 这是一个独立步骤，不依赖其他步骤结果
            2. 分析当前任务需求，选择合适的工具
            3. 为工具调用准备准确的参数
            4. 如需多个工具，可同时调用
            5. 完成所有要求的子任务
            
            ⚠️ 执行要点：
            - 严格按照步骤描述执行
            - 确保工具调用参数准确
            - 收集的信息要完整且相关度高
            """

        user_prompt = step['description']

        try:
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ]

            all_tool_call_details = []
            max_iterations = 10
            iteration = 0
            publish_success = False  # 添加发布成功标志
            publish_error = None  # 保存发布失败的错误信息

            # 第一轮：初始工具调用
            response = self.llm_client.get_tool_call_response(messages, openai_tools)

            if not response.choices[0].message.tool_calls:
                logger.info("第一轮没有工具调用，直接返回")
                final_content = response.choices[0].message.content or ""
            else:
                # 进入循环处理工具调用
                while iteration < max_iterations:
                    iteration += 1
                    logger.info(f"处理第 {iteration} 轮")

                    message = response.choices[0].message

                    if message.tool_calls:
                        logger.info(f"第 {iteration} 轮发现 {len(message.tool_calls)} 个工具调用")

                        # 添加助手消息
                        assistant_msg = {
                            "role": "assistant",
                            "content": message.content or "",
                            "tool_calls": [
                                {
                                    "id": tc.id,
                                    "type": "function",
                                    "function": {
                                        "name": tc.function.name,
                                        "arguments": tc.function.arguments
                                    }
                                }
                                for tc in message.tool_calls
                            ]
                        }
                        messages.append(assistant_msg)

                        # 执行所有工具调用
                        for tool_call in message.tool_calls:
                            tool_name = tool_call.function.name
                            try:
                                arguments = json.loads(tool_call.function.arguments) if tool_call.function.arguments else {}
                            except json.JSONDecodeError:
                                arguments = {}

                            logger.info(f"执行工具: {tool_name} 参数: {arguments}")

                            # 🔍 特殊处理: 在发布前验证图片URL
                            if tool_name == "publish_content" and "images" in arguments:
                                original_images = arguments.get("images", [])
                                logger.info(f"🔍 开始验证 {len(original_images)} 个图片URL...")

                                valid_images = await self.validate_image_urls(original_images)

                                if len(valid_images) < len(original_images):
                                    logger.warning(f"⚠️ 部分图片URL无效: {len(original_images) - len(valid_images)} 个被过滤")

                                if len(valid_images) == 0:
                                    tool_result = "错误: 所有图片URL均无效，无法发布。请确保图片链接可访问。"
                                    logger.error("❌ 图片验证失败: 没有有效的图片URL")
                                    # 不执行实际的发布调用
                                elif len(valid_images) < 1:
                                    tool_result = f"错误: 有效图片数量不足({len(valid_images)}个)，小红书至少需要1张图片才能发布。"
                                    logger.error(f"❌ 图片数量不足: 只有 {len(valid_images)} 个有效图片")
                                else:
                                    # 更新参数中的图片列表为验证后的有效URL
                                    arguments["images"] = valid_images
                                    logger.info(f"✅ 图片验证通过，使用 {len(valid_images)} 个有效图片URL")

                                    # 🛑 Dry-run 模式: 截获参数, 不实际发布
                                    if self.dry_run:
                                        self.captured_publish_args = dict(arguments)
                                        tool_result = "DRY-RUN: 预览模式, 内容已捕获, 未实际发布到小红书"
                                        logger.info(f"🛑 DRY-RUN 截获 publish_content 参数: title={arguments.get('title')}")
                                    else:
                                        # 执行发布工具
                                        tool_result = None
                                        for server in self.servers:
                                            tools = await server.list_tools()
                                            if any(tool.name == tool_name for tool in tools):
                                                try:
                                                    tool_result = await server.execute_tool(tool_name, arguments)
                                                    break
                                                except Exception as e:
                                                    logger.error(f"执行工具 {tool_name} 出错: {e}")
                                                    tool_result = f"Error: {str(e)}"

                                        if tool_result is None:
                                            tool_result = f"未找到工具 {tool_name}"
                            else:
                                # 其他工具正常执行
                                tool_result = None
                                for server in self.servers:
                                    tools = await server.list_tools()
                                    if any(tool.name == tool_name for tool in tools):
                                        try:
                                            tool_result = await server.execute_tool(tool_name, arguments)
                                            break
                                        except Exception as e:
                                            logger.error(f"执行工具 {tool_name} 出错: {e}")
                                            tool_result = f"Error: {str(e)}"

                                if tool_result is None:
                                    tool_result = f"未找到工具 {tool_name}"

                            # 检查是否是 Tavily 搜索工具的错误返回
                            if tool_result is not None and "tavily" in tool_name.lower():
                                result_str = str(tool_result).lower()
                                if ("this request exceeds your plan\'s set usage limit. please upgrade your plan or contact support@tavily.com" in result_str and "432" in result_str):
                                    logger.warning(f"检测到Tavily API受限: {tool_result}")
                                    # 抛出特殊异常，让外层处理轮换和重试
                                    raise TavilyQuotaExceeded("Tavily API配额已用尽，需要轮换Key")

                            # 检测是否是发布工具，并且是否成功
                            if tool_name == "publish_content":
                                if self.dry_run:
                                    publish_success = True
                                    logger.info("✅ DRY-RUN 完成, 停止迭代")
                                else:
                                    # 检查结果是否表明成功
                                    result_str = str(tool_result).lower()
                                    if "success" in result_str or "成功" in result_str or "published" in result_str:
                                        publish_success = True
                                        logger.info("✅ 检测到发布成功，将在本轮结束后停止迭代")
                                    else:
                                        # 保存详细的错误信息
                                        publish_error = str(tool_result)
                                        logger.error(f"❌ 发布失败: {publish_error}")

                            # 记录工具调用详情
                            tool_detail = {
                                "iteration": iteration,
                                "name": tool_name,
                                "arguments": arguments,
                                "result": str(tool_result)
                            }
                            all_tool_call_details.append(tool_detail)

                            # 添加工具结果消息
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tool_call.id,
                                "content": str(tool_result)
                            })

                    # 如果发布已成功，直接结束迭代
                    if publish_success:
                        logger.info("🎉 发布已成功，停止迭代")
                        # 使用一个简单的最终响应
                        final_content = "内容已成功发布到小红书平台"
                        break

                    # 调用get_final_response决定下一步
                    logger.info("调用get_final_response决定下一步动作...")
                    final_response = self.llm_client.get_final_response(messages, openai_tools)
                    final_message = final_response.choices[0].message

                    if final_message.tool_calls:
                        # 继续下一轮
                        logger.info(f"get_final_response返回了 {len(final_message.tool_calls)} 个工具调用，继续...")
                        response = final_response
                    else:
                        # 任务完成
                        logger.info(f"get_final_response返回最终答案。任务在 {iteration} 轮内完成。")
                        final_content = final_message.content or ""
                        break
                else:
                    # 达到最大迭代次数
                    logger.warning(f"达到最大迭代次数 ({max_iterations})。停止工具调用。")
                    final_content = final_message.content or "任务执行超出最大迭代次数限制"

            # 构建结果
            step_result = {
                "step_id": step['id'],
                "step_title": step['title'],
                "tool_calls": all_tool_call_details,
                "total_iterations": iteration,
                "response": final_content,
                "success": True,
                "publish_success": publish_success,  # 添加发布成功标志
                "publish_error": publish_error  # 添加发布错误信息
            }

            return step_result

        except TavilyQuotaExceeded:
            # 不捕获此异常，让它继续向上传播到 generate_and_publish 进行轮换重试
            raise
        except Exception as e:
            logger.error(f"执行步骤 {step['id']} 出错: {e}")
            return {
                "step_id": step['id'],
                "step_title": step['title'],
                "error": str(e),
                "success": False
            }

    async def generate_and_publish(self, topic: str, content_type: str = "general") -> Dict[str, Any]:
        """生成内容并发布到小红书

        Args:
            topic: 用户输入的主题
            content_type: 内容类型 ("general" 或 "paper_analysis")

        Returns:
            生成和发布结果
        """
        try:
            logger.info(f"开始生成关于「{topic}」的内容，类型：{content_type}...")

            # 优先使用全局服务器管理器
            if server_manager.is_initialized():
                logger.info("使用全局服务器管理器")
                self.servers = server_manager.get_servers()
                self.llm_client = server_manager.get_llm_client()
                available_tools = await server_manager.get_available_tools()
            else:
                logger.info("全局服务器未初始化,使用本地初始化")
                # 获取可用工具
                available_tools = await self.get_available_tools()

                if available_tools is None or len(available_tools) == 0:
                    # 初始化服务器
                    await self.initialize_servers()
                    available_tools = await self.get_available_tools()

            logger.info(f"总共可用工具数: {len(available_tools)}")

            # 获取研究计划
            research_plan = self.get_research_plan(topic, content_type)

            # 执行每个步骤
            results = []
            for step in research_plan:
                max_retries = 2  # 最多重试2次（轮换2次Key）
                retry_count = 0

                while retry_count <= max_retries:
                    try:
                        step_result = await self.execute_step(step, available_tools, results, topic)
                        results.append(step_result)

                        if not step_result.get('success'):
                            logger.error(f"步骤 {step['id']} 执行失败")
                            return {
                                'success': False,
                                'error': f"步骤 {step['id']} 执行失败: {step_result.get('error', '未知错误')}"
                            }

                        logger.info(f"步骤 {step['id']} 执行成功")
                        break  # 成功则跳出重试循环

                    except TavilyQuotaExceeded as e:
                        retry_count += 1
                        if retry_count <= max_retries:
                            logger.warning(f"步骤 {step['id']} Tavily配额用尽（第{retry_count}次），开始轮换Key并重试...")

                            # 轮换Key + 重启服务器
                            if await server_manager.rotate_tavily_key():
                                logger.info(f"✅ Key轮换成功，重新执行步骤 {step['id']}...")
                                # 更新本地引用
                                self.servers = server_manager.get_servers()
                                self.llm_client = server_manager.get_llm_client()
                                available_tools = await server_manager.get_available_tools()
                            else:
                                logger.error("❌ Key轮换失败，没有更多可用的Key")
                                return {
                                    'success': False,
                                    'error': f"步骤 {step['id']} 执行失败: Tavily API配额已用尽且无法轮换Key"
                                }
                        else:
                            logger.error(f"❌ 步骤 {step['id']} 已重试{max_retries}次，全部失败")
                            return {
                                'success': False,
                                'error': f"步骤 {step['id']} 执行失败: 已轮换所有Tavily Key但仍然失败"
                            }

            # 检查发布步骤（step3）是否成功
            step3_result = next((r for r in results if r['step_id'] == 'step3'), None)
            publish_success = step3_result.get('publish_success', False) if step3_result else False

            # 如果发布失败，返回失败结果，包含详细的错误信息
            if not publish_success:
                logger.error("内容发布失败")
                publish_error = step3_result.get('publish_error', '') if step3_result else ''

                # 构建详细的错误消息
                error_message = '内容生成完成，但发布到小红书失败。'
                if publish_error:
                    # 清理错误信息，使其更易读
                    error_detail = publish_error.strip()
                    # 如果错误信息太长，截取前500个字符
                    if len(error_detail) > 500:
                        error_detail = error_detail[:500] + '...'
                    error_message += f'\n\n错误详情：{error_detail}'
                else:
                    error_message += '\n请检查小红书MCP服务连接或稍后重试。'

                return {
                    'success': False,
                    'error': error_message
                }

            # 从 step3 的工具调用中提取实际发布的内容
            step3_result = next((r for r in results if r['step_id'] == 'step3'), None)
            content_data = {
                'title': f'关于{topic}的精彩内容',
                'content': '',
                'tags': [topic],
                'images': []
            }

            # 尝试从 tool_calls 中提取 publish_content 的参数
            if step3_result and step3_result.get('tool_calls'):
                try:
                    # 查找 publish_content 工具调用
                    publish_call = next(
                        (tc for tc in step3_result['tool_calls'] if tc['name'] == 'publish_content'),
                        None
                    )

                    if publish_call and publish_call.get('arguments'):
                        # 从工具调用参数中提取实际发布的内容
                        args = publish_call['arguments']
                        content_data = {
                            'title': args.get('title', f'关于{topic}的精彩内容'),
                            'content': args.get('content', ''),
                            'tags': args.get('tags', [topic]),
                            'images': args.get('images', [])
                        }
                        logger.info(f"成功从 publish_content 参数中提取内容数据")
                    else:
                        logger.warning("未找到 publish_content 工具调用或参数为空")
                except Exception as e:
                    logger.error(f"从工具调用参数中提取内容失败: {e}")

            return {
                'success': True,
                'title': content_data.get('title', ''),
                'content': content_data.get('content', ''),
                'tags': content_data.get('tags', []),
                'images': content_data.get('images', []),
                'publish_status': '已成功发布',
                'full_results': results
            }

        except Exception as e:
            logger.error(f"生成和发布失败: {e}", exc_info=True)
            return {
                'success': False,
                'error': str(e)
            }

        finally:
            # 只有在使用本地服务器时才清理资源
            if not server_manager.is_initialized():
                await self.cleanup_servers()

    async def cleanup_servers(self):
        """清理服务器连接"""
        for server in reversed(self.servers):
            try:
                await server.cleanup()
            except Exception as e:
                logger.warning(f"清理警告: {e}")

    def get_paper_analysis_plan(self, user_topic: str) -> List[Dict[str, Any]]:
        """生成论文分析专用工作流"""
        return [
            {
                "id": "step1_paper",
                "title": f"「{user_topic}」领域论文检索与分析",
                "description": (
                    f"1. 使用搜索工具搜索「{user_topic}」相关的最新学术论文\n"
                    f"2. 搜索策略：\n"
                    f"   - 使用关键词：\"site:arxiv.org {user_topic}\" 搜索arXiv论文\n"
                    f"   - 搜索 \"{user_topic} paper research study\" 获取相关研究\n"
                    f"   - 重点关注最近1-2年的高影响力论文\n"
                    f"3. 筛选标准：\n"
                    f"   - 优先选择高引用量、知名会议/期刊的论文\n"
                    f"   - 关注技术创新点和实际应用价值\n"
                    f"   - 收集2-3篇最具代表性的论文\n"
                    f"4. 信息收集：\n"
                    f"   - 论文标题、作者、发表时间\n"
                    f"   - 核心摘要和研究问题\n"
                    f"   - 主要创新点和贡献\n"
                    f"   - 实验结果和关键图表\n"
                    f"   - 论文全文链接"
                ),
                "depends on": []
            },
            {
                "id": "step2_analysis",
                "title": "论文深度解读与内容生成",
                "description": (
                    "1. 按照以下标准格式生成论文分析内容：\n"
                    "   📚 **标题**: 论文核心价值的通俗化表达\n"
                    "   📝 **核心摘要**: 2-3句话概括论文要解决的问题和主要发现\n"
                    "   💡 **主要贡献**: 3个创新点（技术突破、方法创新、应用价值）\n"
                    "   🚀 **未来发展**: 技术改进方向、潜在应用场景、商业化前景\n"
                    "   🔮 **展望**: 个人观点、行业影响预期、后续研究方向\n"
                    "   📖 **论文链接**: 原始论文的完整链接\n"
                    "2. 语言要求：\n"
                    "   - 通俗易懂，避免专业术语堆砌\n"
                    "   - 适当使用emoji表情增加可读性\n"
                    "   - 保持客观准确，不夸大研究结果\n"
                    "3. 内容质量：\n"
                    "   - 长度控制在800-1200字\n"
                    "   - 突出论文的创新价值和应用意义\n"
                    "   - 提供具体的技术细节和数据支撑"
                ),
                "depends on": ["step1_paper"]
            },
            {
                "id": "step3_format",
                "title": "小红书格式适配与发布",
                "description": (
                    "1. 将论文分析内容适配小红书格式：\n"
                    "   - 标题突出论文的核心价值，保留「论文分享」标识\n"
                    "   - 正文移除#标签，改为自然语言表达\n"
                    "   - 提取5个精准标签（学术性+科普性+热点性）\n"
                    "   - 确保包含2-3张论文相关图片（图表、架构图、截图）\n"
                    "2. 标签示例：#AI研究 #学术论文 #科技前沿 #知识分享 #人工智能\n"
                    "3. 内容要求：\n"
                    "   - 保持学术严谨性同时兼顾可读性\n"
                    "   - 突出研究的创新点和实用价值\n"
                    "   - 避免过于技术化的表述\n"
                    "4. 直接使用publish_content工具发布到小红书\n"
                    "5. 确保图片链接有效且与论文内容相关"
                ),
                "depends on": ["step1_paper", "step2_analysis"]
            }
        ]
