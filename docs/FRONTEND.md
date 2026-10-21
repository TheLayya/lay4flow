# lay4flow-remote 前端开发

界面源码在 `frontend/`，使用 Vue、Element Plus 与 Vite。构建产物写入 `static/app/`，由 Python 服务提供，无需独立 Node 生产服务。修改界面后应重新构建并提交产物。

```sh
npm --prefix frontend ci
npm --prefix frontend test
npm --prefix frontend run build
python scripts/check.py
```

本地预览使用两个终端：

```sh
python scripts/preview_frontend.py
npm --prefix frontend run dev
```

预览脚本使用临时配置、合成管理员和临时数据库，不读取部署账号。浏览器测试可使用 `npm --prefix frontend run test:e2e`；需在本机安装 Chrome/Edge，或通过 `PLAYWRIGHT_BROWSER_PATH` 指定浏览器路径。测试结果与截图保存到仓库外的临时目录。

应用使用 hash 路由。管理员 API 使用已有 HttpOnly Cookie，公开 API 使用 Bearer Key。创作台复用管理会话，不向页面暴露服务 API Key。Profile 浏览器在单独窗口中运行，沿用同源、WebSocket 和账号归属校验，关闭时释放连接。

管理登录 Cookie 为 `lay4flow_remote_admin_session`，默认 30 天；服务端在数据目录保存令牌哈希和到期时间，容器重启后沿用。退出或修改密码撤销持久会话。前端仅将带 `X-Admin-Session: expired` 的 401 视为后台登录失效，账号 / 上游 401 保留业务提示。`node frontend/tests/admin-session.e2e.mjs` 使用端口 38128 的临时预览，验证重开浏览器、刷新、业务 401 和退出；报告位于 `tmp/admin-session-review/`。

账号 JSON 通过 `/api/profile-transfer/jobs` 提交后台任务，原生 Profile、旧版账号与混合列表共用服务端队列。账号页按真实服务端阶段显示任务，切页由全局提示继续跟踪；刷新后通过任务列表恢复。同一 `request_id` 防止提交响应丢失后重复排队，失败重试仅处理失败项。任务保留在服务内存，服务重启后重新提交，已导入账号与 Profile 保留。`node frontend/tests/profile-transfer.e2e.mjs` 在端口 38126 的临时预览上验证关窗、切页、刷新与重试；报告位于 `tmp/profile-transfer-ui/`。

代理池位于独立菜单，支持账号固定出口与媒体独立分配。远程打码固定接入 <https://cap.tk218.top/>，设置页提供申请链接、API 密钥与超时。缺少密钥时，全局提示并引导到设置页；Flow 创作提交前提示配置，Vids 维持独立会话链。本地 Cloak 用于账号登录与刷新。

账号快速展开及网络编辑页可保存 `submission_proxy_mode: fixed | pool_random`，只影响 Flow 生成提交，登录绑定保留。代理池行显示 `success_rate`、`inflight`、`consecutive_failures`、`cooldown_until` 和 `circuit_state`，与服务端熔断探针结果一致。随机策略按任务预留出口并在结束时释放，整份任务保持同一出口。

构建产物中的 JS、CSS 与图片需要完整随镜像更新。生产入口 HTML 使用 `no-store`，部署新版本后刷新页面读取新资源。功能验证应覆盖登录、账号状态、代理编辑、远程配置和移动端布局；生成测试使用模拟服务，真实额度验收单独记录。

## README 截图

创作工作台通过 `POST /api/admin/studio/jobs` 提交 Flow / Vids 后台批次，图片和视频支持 1–20 份；生成时编辑表单保持可用。`GET /api/admin/studio/jobs` 恢复逐项结果与进度，Vids 内容仍使用原收据端点。`node frontend/tests/studio-batch.e2e.mjs` 在端口 38129 的合成预览上验证连续提交、两种媒体数量、刷新恢复和桌面/手机布局。

公开截图位于 `docs/screenshots/`，由实际前端渲染，账号、代理和配额全部使用合成数据。作品使用仓库已有 lay 插画。先构建前端并启动临时预览，再在另一终端执行：

```sh
python scripts/preview_frontend.py --port 38127
```

```sh
node scripts/capture_readme.mjs
```

脚本需要已安装的 Chrome / Edge 与开发依赖中的 Pillow。中间 PNG 和网络、页面检查报告保存在已忽略的 `tmp/readme-review/`；对外的 WebP 保存在 `docs/screenshots/`。拍摄过程拦截全部 API 和外部请求。截图说明与浏览器路径配置见[截图文档](screenshots/README.md)。
