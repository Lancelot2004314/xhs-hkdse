# Day 1 — xhs-mcp 部署与登录

> 目标：把 xpzouying/xiaohongshu-mcp 跑起来，扫码登录小红书账号，通过 MCP Inspector 测试发一篇 test 笔记。

## 前置

- macOS Apple Silicon (arm64) - 已确认
- 已安装 Chrome 或 Edge 浏览器（xhs-mcp 默认会找系统 Chrome）
- 一个小红书账号（手机端能扫码即可）

## 步骤 1：扫码登录（只需一次）

```bash
cd /Users/lance/marketing/xhs-hkdse/xhs-mcp
./xiaohongshu-login-darwin-arm64
```

会弹出浏览器窗口显示小红书登录页 + 二维码。
- 用手机小红书 App 扫码
- 在 App 上点击"确认登录"
- **如果出现滑动验证码**：手动滑动通过
- 登录成功后，浏览器窗口可关闭
- 当前目录会生成 `cookies.json`（已加入 .gitignore）

> **重要**：当前 mcp 服务登录的小红书账号，**不要**在其他网页平台同时登录，否则会"踢出"当前 mcp 的登录态。

## 步骤 2：启动 MCP 服务

```bash
cd /Users/lance/marketing/xhs-hkdse/xhs-mcp

# 默认无头模式
./xiaohongshu-mcp-darwin-arm64

# 或者非无头（首次/调试推荐，遇到验证码可手动过）
./xiaohongshu-mcp-darwin-arm64 -headless=false

# 自定义端口
./xiaohongshu-mcp-darwin-arm64 -port :18060
```

服务启动后监听 `http://localhost:18060/mcp`。

## 步骤 3：用 MCP Inspector 测试

新开一个终端：

```bash
npx @modelcontextprotocol/inspector
```

在浏览器打开 Inspector UI，配置：
- Transport: `Streamable HTTP`
- URL: `http://localhost:18060/mcp`

点击 `Connect`，然后 `List Tools` 应该看到：
- `check_login_status`
- `get_login_qrcode`
- `delete_cookies`
- `publish_content`
- `list_feeds` / `search_feeds`
- `get_feed_detail`
- `user_profile`
- `post_comment_to_feed`
- `reply_comment_in_feed`

### 3.1 测试登录状态

调用 `check_login_status`（无参数），应该返回当前已登录账号的昵称。

### 3.2 测试发布（test 笔记）

调用 `publish_content`，参数：

```json
{
  "title": "DSE 备考小工具测试",
  "content": "这是一条测试笔记，请忽略 #DSE2026 #香港高考",
  "images": ["https://images.unsplash.com/photo-1497633762265-9d179a990aa6?w=800"]
}
```

> 标题硬限 ≤ 20 字。
> images 支持 HTTP/HTTPS URL 或本地绝对路径。
> 话题标签直接写在 content 里，平台自动识别。

发布成功后到小红书 App 的"我的笔记"应该能看到这条。

## 步骤 4：在 Cursor 里挂载 xhs-mcp

在 Cursor 项目根目录创建 `.cursor/mcp.json`：

```json
{
  "mcpServers": {
    "xiaohongshu": {
      "url": "http://localhost:18060/mcp",
      "description": "小红书发布"
    }
  }
}
```

之后在 Cursor 对话里就能直接让 AI 调 `publish_content`。

## 常见问题

- **二维码出来后扫不出来**：登录窗口可能会卡住，等 30s 后再扫；或者 Cmd+R 刷新。
- **报"IP 存在风险"**：换网络（家庭 WiFi 优先），不要走 VPN/代理。
- **cookie 过期**：重跑 `./xiaohongshu-login-darwin-arm64` 即可，约 1–2 周需要做一次。
- **想跑在常开机器上**：把 `xhs-mcp/` 整个目录 scp 到 Mac mini / Linux GUI 机器，用 `nohup ./xiaohongshu-mcp-darwin-arm64 > mcp.log 2>&1 &` 跑。
- **风控**：单账号一天 ≤ 3 篇，间隔 ≥ 2 小时，不要在其他设备同时登录这个账号。

## Day 1 完成标志

- [ ] cookies.json 已生成
- [ ] mcp 服务能 `curl http://localhost:18060/mcp` 通
- [ ] MCP Inspector 能列出所有 tools
- [ ] 通过 publish_content 发了一条 test 笔记并能在小红书 App 看到
- [ ] Cursor `.cursor/mcp.json` 配好
