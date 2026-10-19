# 界面截图

这些图片由实际前端页面渲染，使用合成演示数据：账号邮箱为 `example.test`，代理为文档专用的 `192.0.2.0/24` 网段。创作工作台中的插画来自仓库现有的 lay 素材，不代表现场生成验收。截图未包含运行实例的账号、密钥、Cookie 或 Profile 内容。

远程打码设置展示固定申请入口 `https://cap.tk218.top/`。截图使用演示 Key 与已配置状态，拍摄时拦截外部请求，不连接真实打码服务。

`image-editor.webp` 展示上传本地 lay 插画后的裁剪、涂鸦与 AI 编辑入口。`vids-studio.webp` 展示语音工作台及 30 个预设声线；三个音频卡片使用本地合成演示音，不代表上游声线生成或实时验收。

桌面图片为 1600 × 1000，`mobile.webp` 为 390 × 844。WebP 使用质量 90 编码，保留原始尺寸。

在已安装前端依赖、Chromium 浏览器与 Python Pillow 的环境中复现：

```sh
npm --prefix frontend run build
python scripts/preview_frontend.py --port 38127
```

在另一个终端执行：

```sh
node scripts/capture_readme.mjs
```

脚本拦截全部 API 请求与外部请求，只从预览服务读取前端资源。中间 PNG 和检查报告写入已忽略的 `tmp/readme-review/`，公开图片写入本目录；报告包含页面错误、资源错误、网络拦截与横向溢出检查结果。可以通过 `FRONTEND_PREVIEW_URL` 指定其他本地预览端口，通过 `PLAYWRIGHT_BROWSER_PATH` 指定 Chromium 路径。
