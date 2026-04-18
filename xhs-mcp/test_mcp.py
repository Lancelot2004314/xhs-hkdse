"""快速验证 xhs-mcp 服务：列出工具 + 检查登录状态。"""
import asyncio

from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client

URL = "http://localhost:18060/mcp"


async def main():
    async with streamablehttp_client(URL) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            print(f"\n已连接 xhs-mcp，共 {len(tools.tools)} 个工具：")
            for t in tools.tools:
                print(f"  - {t.name}")

            print("\n调用 check_login_status ...")
            result = await session.call_tool("check_login_status", {})
            for c in result.content:
                if hasattr(c, "text"):
                    print(c.text)


if __name__ == "__main__":
    asyncio.run(main())
