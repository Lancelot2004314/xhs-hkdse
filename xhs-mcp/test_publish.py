"""测试 xhs-mcp 的 publish_content：发一篇可立即删除的测试笔记。"""
import asyncio
import json

from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client

URL = "http://localhost:18060/mcp"

PAYLOAD = {
    "title": "Test - 请忽略",
    "content": "这是一条 API 测试笔记，发完即删 #test #api",
    "images": [
        "https://images.unsplash.com/photo-1497633762265-9d179a990aa6?w=1080&q=80"
    ],
}


async def main():
    async with streamablehttp_client(URL) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            print(f"调用 publish_content ...\n参数:\n{json.dumps(PAYLOAD, ensure_ascii=False, indent=2)}\n")
            result = await session.call_tool("publish_content", PAYLOAD)
            print("\n返回：")
            for c in result.content:
                if hasattr(c, "text"):
                    print(c.text)


if __name__ == "__main__":
    asyncio.run(main())
