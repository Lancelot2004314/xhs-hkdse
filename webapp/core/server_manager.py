"""
全局 MCP 服务器管理器
负责在应用启动时初始化所有 MCP 服务器,并在整个应用生命周期内复用这些连接
"""
import asyncio
import json
import logging
import os
from typing import List, Optional, Dict, Any
from core.xhs_llm_client import Server, Tool, LLMClient

logger = logging.getLogger(__name__)


class ServerManager:
    """全局服务器管理器单例"""

    _instance = None
    _initialized = False

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        """初始化管理器"""
        if not ServerManager._initialized:
            self.servers: List[Server] = []
            self.llm_client: Optional[LLMClient] = None
            self.config: Optional[Dict[str, Any]] = None
            self._is_cleaning = False  # 防止重复清理的标志
            ServerManager._initialized = True

    async def initialize(self, config: Dict[str, Any]):
        """初始化所有 MCP 服务器

        Args:
            config: 应用配置字典
        """
        try:
            logger.info("开始初始化全局 MCP 服务器...")

            # 保存配置
            self.config = config

            # 动态构建服务器配置（使用传入的 config 参数，而不是从文件读取）
            # 注意: xhs MCP 故意不放进来 — xhs 单实例只能撑 1 个长会话,
            # 长会话会被浏览器自动化拖死. 所有 xhs 调用走 xhs_research._fresh_xhs_session.
            server_config = {
                "mcpServers": {
                    "jina-mcp-tools": {
                        "args": ["jina-mcp-tools"],
                        "command": "npx",
                        "env": {
                            "JINA_API_KEY": config.get('jina_api_key', '')
                        }
                    },
                    "tavily-remote": {
                        "command": "npx",
                        "args": [
                            "-y",
                            "mcp-remote",
                            f"https://mcp.tavily.com/mcp/?tavilyApiKey={config.get('tavily_api_key', '')}"
                        ]
                    },
                }
            }

            # 创建服务器实例
            self.servers = [
                Server(name, srv_config)
                for name, srv_config in server_config["mcpServers"].items()
            ]

            # 初始化 LLM 客户端
            self.llm_client = LLMClient(
                config.get('llm_api_key'),
                config.get('openai_base_url'),
                config.get('default_model', 'claude-sonnet-4-20250514')
            )

            # 初始化所有服务器
            initialized_count = 0
            for server in self.servers:
                try:
                    await server.initialize()
                    initialized_count += 1
                    logger.info(f"✅ 成功初始化服务器: {server.name}")
                except Exception as e:
                    logger.error(f"❌ 初始化服务器 {server.name} 失败: {e}")

            logger.info(f"🎉 全局 MCP 服务器初始化完成: {initialized_count}/{len(self.servers)} 个服务器已就绪")

        except Exception as e:
            logger.error(f"初始化全局服务器失败: {e}", exc_info=True)
            raise

    async def get_available_tools(self) -> List[Tool]:
        """获取所有可用的工具

        Returns:
            所有服务器提供的工具列表
        """
        all_tools = []
        for server in self.servers:
            try:
                tools = await server.list_tools()
                all_tools.extend(tools)
            except Exception as e:
                logger.error(f"从服务器 {server.name} 获取工具失败: {e}")

        return all_tools

    def get_servers(self) -> List[Server]:
        """获取所有已初始化的服务器

        Returns:
            服务器列表
        """
        return self.servers

    def get_llm_client(self) -> Optional[LLMClient]:
        """获取 LLM 客户端

        Returns:
            LLM 客户端实例
        """
        return self.llm_client

    def update_llm_client(self, config: Dict[str, Any]):
        """更新 LLM 客户端配置

        Args:
            config: 新的配置字典
        """
        self.config = config
        self.llm_client = LLMClient(
            config.get('llm_api_key'),
            config.get('openai_base_url'),
            config.get('default_model', 'claude-sonnet-4-20250514')
        )
        logger.info("LLM 客户端配置已更新")

    async def cleanup(self):
        """清理所有服务器连接"""
        # 防止重复清理
        if self._is_cleaning:
            logger.warning("清理操作正在进行中，跳过重复调用")
            return

        self._is_cleaning = True

        try:
            logger.info("开始清理全局 MCP 服务器...")

            for server in reversed(self.servers):
                try:
                    await server.cleanup()
                    logger.info(f"清理服务器: {server.name}")
                except asyncio.CancelledError:
                    # 被取消时静默处理，因为这是预期行为
                    logger.debug(f"清理服务器 {server.name} 被取消（预期行为）")
                except Exception as e:
                    # 检查是否是 AsyncExitStack 的上下文错误（这是无害的）
                    error_msg = str(e).lower()
                    if "cancel scope" in error_msg or "different task" in error_msg:
                        # 这些错误是无害的，降级为 debug 日志
                        logger.debug(f"清理服务器 {server.name} 时的上下文切换提示: {e}")
                    else:
                        # 其他真正的错误才打印 warning
                        logger.warning(f"清理服务器 {server.name} 时出错: {e}")

            # 无论如何都要清空引用，确保可以重新初始化
            self.servers = []
            self.llm_client = None
            ServerManager._initialized = False
            logger.info("全局 MCP 服务器清理完成")

        finally:
            # 确保清理标志被重置，即使出现异常
            self._is_cleaning = False

    async def rotate_tavily_key(self) -> bool:
        """轮换Tavily Key并重启服务器

        Returns:
            是否成功轮换并重启
        """
        try:
            from config.config_manager import ConfigManager
            config_manager = ConfigManager()

            # 轮换Key
            new_key = config_manager.rotate_tavily_key()
            if not new_key:
                logger.warning("无法轮换Tavily Key: 没有可用的新Key")
                return False

            logger.info(f"Tavily Key已更新，正在重启服务器...")

            # 获取最新配置（不做显示转换，保持原始格式）
            new_config = config_manager.load_config(for_display=False)

            # 重启服务器
            await self.cleanup()
            await self.initialize(new_config)

            return True

        except Exception as e:
            logger.error(f"轮换Tavily Key并重启服务器失败: {e}")
            return False

    def is_initialized(self) -> bool:
        """检查服务器是否已初始化

        Returns:
            是否已初始化
        """
        return len(self.servers) > 0 and self.llm_client is not None


# 创建全局单例实例
server_manager = ServerManager()