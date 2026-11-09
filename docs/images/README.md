# 界面图来源

本目录的产品界面图使用 lay4flow-remote 当前前端构建产物，在独立浏览器会话中通过模拟 API 渲染。演示邮箱使用 `example.test`，代理也使用保留的测试域名；套餐、额度、状态和请求日志均为合成记录。没有读取真实账号、Cookie、Profile、数据库或代理凭据，没有调用 Google 或验证码服务，也没有提交生成任务。

| 图片 | 内容 |
|---|---|
| `accounts-overview.png` / `release-accounts.webp` | 紧凑账号表格、Free／Plus／Pro／Ultra 套餐、额度与会话状态 |
| `proxy-pool-overview.png` | 代理池绑定、连接状态、成功率、连续失败与冷却 |
| `request-logs-overview.png` | 请求列表的尝试次数、重试次数与账号数 |
| `request-attempts.png` / `release-logs.webp` | 逐次验证码／生成失败、命中规则、换号与成功记录 |
| `error-rules.png` / `release-rules.webp` | 可编辑关键词规则及连续失败保护 |
| `omni12-video-extend.png` / `release-studio.webp` | 从已有视频作品准备 Omni 1.2 续写；无媒体 ID 输入框 |
| `omni12-video-upload.png` | 续写原视频上传入口 |
| `extension-connected.png` | 隔离 Chromium 实际加载 1.8.4 扩展后，管理页显示已连接；后台 API 使用模拟响应 |
| `extension-install.png` | 隔离 Chromium 的扩展版本、站点权限与无痕设置；仅用于说明设置位置 |

视频画面是本地绘制的山谷示例，经本地编码为 8 秒 MP4，非生成服务的质量或成功率示例。WebP 文件是对应 PNG 的无损副本，用于 README 加载；截图中的模拟套餐和额度不构成上游服务承诺。

`qq-group.jpg` 为项目既有公开联系图片，不属于模拟产品界面。

## 重新拍摄

安装前端依赖、Python Pillow、FFmpeg 和 Chrome／Edge 后，在项目根目录运行：

```powershell
npm.cmd --prefix frontend run build
node scripts/capture_release_readme.mjs
```

脚本自行启动只读静态预览，绘制本地示例视频，拦截所有 API 与外部请求，不依赖已运行的真实后台。可通过 `PLAYWRIGHT_BROWSER_PATH`、`PYTHON`、`FFMPEG` 指定工具路径。中间视频和检查证据仅写入已忽略的 `tmp/docs-visuals/`；公开图片写入本目录。扩展实际连接的独立模拟浏览器验收见 `frontend/tests/extension-browser.e2e.mjs`。
