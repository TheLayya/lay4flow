# Windows 原生打包与登录迁移交接记录

## 2026-10-22：1.5.2 布局与打包续接

- 创作编辑器已移除“描述 / 画面设定 / 参考素材”分区 Tab，三块常显；清理分区撑高规则，小窗口使用编辑器内部滚动。38001 已更新静态资源并观察到三块同显，未提交真实生成请求。
- Flow HTTP 包装器读取现有 Profile 页面 navigator 身份，沿用其 User-Agent、语言与匹配 TLS 配置；没有页面时只创建并关闭自有空白页面，不导航用户登录页。
- 本轮 `python scripts/check.py` 通过 1480 项 Python 测试（1 项跳过）、Node 契约与两轮离线 smoke；前端 131 项、4 档桌面共 28 项视口检查、390×844 手机图片/视频布局和 6 个批量生成模拟场景通过。
- 旧 `dist/1.5.2-final` 来自提交 `3d91ea2`，不含以上变动；重新打包使用新目录并记录固定来源。GitHub 1.5.1 为草稿；全新无 Docker Windows 和旧版桌面更新验收仍待完成，新候选同样保留草稿状态。

## 2026-10-22：1.5.2 生成补齐

- 1.5.1 仅完成认证并临时拦住生成，用户指出后暂停发布；GitHub v1.5.1 保持 Draft，没有提升 latest。
- 当前工作区接入现代 Flow image/upload/upscale/video/poll RPC，使用 Profile Cookie、固定账号出口及原远程验证码服务；不构造静态假 Bearer。
- `FlowProfileGeneration` 保留 Profile 会话/锁/关闭语义，通过独立 HTTP jar 发请求并合并受信任 Set-Cookie；结果不明不重发生成。
- 现场最小 BELUGA 图片 RPC成功，约 15 秒，下载 HTTP 200、436884 字节；证据仅在 `tmp/flow-rpc-evidence/live-image-summary.json`，不入包。
- 完整当前服务入口 `/v1/images/generations` 已验收 HTTP 200、1 张图片、35.53 秒（2026-10-22 09:00 UTC）；证据 `tmp/flow-rpc-evidence/local-api-image-summary.json`。当前 38001 已运行含该生成修复的镜像。测试中暴露并修复历史刷新 anchor/future schedule冲突及Playwright新请求通道超时；生成使用同Profile Cookie和固定出口的Curl请求，保留scope/删除语义及GUI并发Cookie更新。
- 刷新周期与五分钟校验缓存分开。新 Cloak 30 分钟；已有用户间隔保留。旧 Labs anchor/未来计划冲突已增加回归，周期不会因轻量探针不断滑动。
- 对照参考：TheSmallHanCat/flow2api commit `828b62080aa72233555bbc9c0bdbf81f244dc90e`。其 `flow-frontend-cookie` 是本地兼容标记，认证依赖完整 Cookie；本项目继续保存 `at=None`，保留既有身份/固定代理校验。
- 视频各路由已做合成契约验证；此轮真实网络验收只消耗最小图片请求，视频计费生成与跨系统迁移另记结果。

## 2026-10-22：Flow 认证已迁移到新版 RPC（当前工作区）

- 官方 Labs `/fx/api/auth/csrf`、`/session` 等旧认证入口实测返回 HTTP 410，正文明确标注认证端点已停用。将旧路径直接换到 `flow.google.com` 返回的是 HTML，也不是认证接口。
- Cloak 登录校验、后台刷新和 Profile 迁移现使用 `https://flow.google.com/` 的 WIZ 配置，以及同一浏览器 Cookie/固定代理上下文中的 `GetPeople(o30O0e)`、`GetCredits(nzlxg)`；不再读取 NextAuth、不再 CSRF/OAuth 续签、不再 ST→AT 回退。
- 现代 Flow 验证结果保存 `at=None`、`at_expires=None`；数据库原 `st` 字段仅保留唯一占位标识。调度按成功 Profile 验证时间及刷新间隔判断新鲜度。身份比较、固定代理、seed、正常关闭和两次迁移冷启动校验继续保留。
- 项目准备使用当前 `GetProjects(UpteDb)`，优先复用远程现有项目；没有未入库项目时使用官方当前前端定义的 `CreateProject(jHPbke)` 请求。CreateProject 此轮只做合成校验，未向真实账号发写请求。
- 2026-10-22 05:52 UTC 只读现场验收：账号 3、固定代理绑定一致；首页、GetPeople、GetCredits 和 GetProjects 全部 HTTP 200，身份一致、895 credits、第一页 12 个项目。只记录布尔结果、额度、数量和状态，不保存凭据或完整身份响应。证据：`tmp/flow-rpc-evidence/live-readonly-summary.json`（本地诊断，不入包）。
- 新版生成 transport 尚待迁移。当前明确返回“Flow 登录态已验证；新版生成接口适配尚未完成”，防止把空 AT 送到旧 Bearer 接口。登录验证通过不代表生成已验收。
- 此节优先于下面旧链路记录；下文旧 OAuth/ST/AT 描述仅保留为历史背景。旧 `dist/1.5.0-final` 资产尚未包含本轮改动。
- 验证结果：`python scripts/check.py` 通过 1381 项 Python 测试（1 项跳过）、Node 契约和离线首次启动/冷重启 smoke；前端 131 项通过，资源已重新构建；隔离端口的原生登录 E2E 4 项、迁移 E2E 5 项通过。最后的 WIZ/GetPeople Gmail 别名比较复用既有身份函数后，登录/RPC/项目/迁移 63 项定向测试再次通过。
- 当前 `http://127.0.0.1:38001` 已运行 `lay4flow-remote:flow-auth-20261022` 镜像，容器 healthy；确认新模块存在、旧 `_renew_session` 已删除、私有 `flow_ck.env` 未进入镜像。尚未提交工作区或发布 Windows 包。再次 Compose 启动需显式选择该镜像：`LAY4FLOW_REMOTE_IMAGE=lay4flow-remote:flow-auth-20261022`；旧 `lay4flow-remote:1.5.0` 镜像保留用于回滚。

更新时间：2026-10-22
当前主线提交：`802466d fix: 校验原生 Cloak 二进制被打入安装包`
当前版本文件：`1.5.0`

交接文档本身随本次提交更新；如果后续版本号或最终包摘要变化，以 `git log`、`VERSION` 和对应 `dist/<version>/build-manifest.json` 为准。

## 2026-10-22 续接修复（当前工作区）

- 用户再次确认的提示为“登录验证未通过，请核对邮箱并完成 Google 登录”。该文案来自强制刷新返回 False 的统一处理，本身没有证明邮箱不匹配。
- 完成 Cloak 登录现在先读取当前浏览器的 Labs 会话，严格检查邮箱、AT 有效期和 Flow credits；现有会话有效时直接保存，再正常关闭并重开验证持久化。缺少或过期会话继续走 OAuth。手动刷新与 Profile 导入仍强制续签。
- 登录失败返回脱敏的 `reason`、`stage`、`network_step`、`retryable`、`browser_open`。刷新诊断随数据库升级持久化，成功后清除。前端保留登录回执，提供重试；关闭后的“继续登录”会重开同一 Profile，刷新页面后也保留该状态。
- Profile 迁移允许 Docker/Linux 与 Windows 原生之间的 runtime、UA、语言、时区和窗口差异；目标端记录自身环境。seed、受支持启动参数、固定代理、Cookie scope、账号身份和额度检查保留，仍需 OAuth 与两次关闭重开通过后才保存账号。
- 失败迁移只在相同 seed、目标启动配置和固定代理绑定下继续，兼容旧 sidecar 绑定；冷启动校验失败也会返回具体网络步骤。
- 校验使用合成账号和模拟服务，记录见本节末尾；真实账号与原安装目录未用于检查。`VERSION` 仍为 `1.5.0`，下述 `dist/1.5.0-final/` 是修复前资产，尚未包含本次修复。
- 当前工作区验证：全项目 `python scripts/check.py` 在本次阶段化修复前通过 1344 项 Python 测试、Node 检查和离线 smoke；阶段化修复后的登录、Profile、迁移、数据库升级定向套件 117 项通过；`npm.cmd --prefix frontend test` 132 项通过；前端构建和两个 Playwright 合成流程通过。
- 原生 smoke 证据：`tmp/native-smoke-dc77a16923cb4be58155977041219768/smoke-results.json`。隔离复制当前源码、静态资源和既有 bundled runtime，PATH 中 `python.exe`、`node.exe`、`docker.exe` 均不存在；START、重复 START（同 PID）、`/health` 1.5.0/native、合成管理员登录、前端入口、STOP 均通过，停止后进程和状态文件消失。该目录属于本地临时验证资料，不进入发布包。

真实网络验收待使用包含本次修复的新构建完成：确认 Cloak 登录后添加成功，或记录新的具体阶段；确认 Docker 导出包在 Windows 导入、关闭重开后身份保持一致。

## 本次已完成的 Windows 核心

### 交付形态

Windows 安装包已经从 Docker 依赖切换为原生后台模式：

- 安装包自带 Python 3.12.10 embeddable runtime、Windows 依赖、Cloak Chromium 和桌面 WebView2 壳。
- 用户不需要安装 Docker Desktop、Python、Node、Git 或 .NET。
- 原生后台仅监听 `127.0.0.1`，由 `native-service.py` 启动 FastAPI/Uvicorn。
- 原生服务状态：`data/native-service.json`；日志：`data/native-service.log` 与 `.error`。
- `native-start.ps1` 做端口检查、安装目录/命令行/PID/启动时间校验、重复启动幂等和 `/health` 版本检查。
- `native-stop.ps1` 创建 stop 文件，等待后台正常关闭 Profile/任务；超时保留进程和状态，避免强杀破坏 Profile。
- `start.ps1` 检测内置 `backend/windows-x64/python.exe` 后走原生路径；没有内置 runtime 时仍保留 Docker 服务器路径。
- `install.ps1` 检测原生 bundle 后分派 `native-install.ps1`，不执行 Docker 检查；旧 Docker 安装覆盖升级前要求安全停旧 Docker 服务。
- 原生 Cloak 账号浏览器使用本机 headed Chromium，不再使用 VNC/noVNC；后端 viewer 返回 `presentation: native`，支持 focus、refresh、close。
- 账号页和 Profile 页面在 native 模式下不创建 noVNC popup/WS，显示本机窗口、显示窗口、刷新凭据、保存关闭、关闭窗口。

关键文件：

- `scripts/build_native_runtime.py`
- `scripts/windows/native-install.ps1`
- `scripts/windows/native-service.py`
- `scripts/windows/native-start.ps1`
- `scripts/windows/native-stop.ps1`
- `scripts/windows/start.ps1`
- `scripts/windows/install.ps1`
- `src/services/browser/profile_viewer.py`
- `src/services/browser/cloak_backend.py`
- `src/api/admin_routes/profile_browser.py`
- `frontend/src/views/AccountsView.vue`
- `frontend/src/views/ProfileView.vue`

### 网络

- `scripts/windows/network.ps1` 自动读取 Windows 系统代理/PAC，PAC 返回 DIRECT 时遵循直连；否则探测 Clash/Mihomo/sing-box 等实际监听端口。
- 支持任意端口、HTTP CONNECT、SOCKS5 完整握手，不要求 TUN，不写死 7890/7897。
- Windows 原生端将 loopback 代理写成 `127.0.0.1:实际端口`；Docker 端使用 `host.docker.internal:实际端口`。
- `LAY4FLOW_HOST_PROXY_MODE=auto/direct/manual`；manual 使用 `http(s)://127.0.0.1:端口` 或 `socks5(h)://127.0.0.1:端口`。
- Cloak、Flow REST、旧 Cookie、协议登录、代理检测、媒体下载和 Vids 请求都复用宿主代理前置链；账号固定 SOCKS5 仍是最终出口。

## 已构建最终 Windows 资产

目录：`dist/1.5.0-final/`

- `lay4flow-remote-1.5.0-windows-x64-setup.exe`：221,301,706 bytes
- `lay4flow-remote-1.5.0-windows-x64.zip`：321,737,198 bytes
- `lay4flow-remote-1.5.0-runtime.tar.gz`：4,074,912 bytes
- `artifact-inventory.json`
- `build-manifest.json`
- `SHA256SUMS`

SHA256：

- runtime：`f8f02a8d88489b4f98f5ed44ff5c7fd433b2e7d86380caf0cad296be48ce6d85`
- Windows ZIP：`52d0e3c7ad5bbfd2034594ac1985b0055e0ff9c41d243b2713c51b6fa4e88b97`
- Windows setup：`0b3bb85388d76a2ad3cecadf719e82b6a55bceb48727878c8eb9ee6e6d6b1d99`

ZIP 内已检查存在：

- `backend/windows-x64/python.exe`
- `backend/windows-x64/cloakbrowser/chrome.exe`
- `windows/native-install.ps1`
- `desktop/windows-x64/Lay4Flow.Desktop.exe`

原生 runtime 展开约 732MB；完整 Cloak 浏览器被随包带入，避免用户首启下载。

## 已验证结果

- `python -m unittest tests.test_native_runtime tests.test_profile_viewer tests.test_profile_browser_routes tests.test_windows_installer tests.test_desktop_update -q`：通过。
- 全项目 `python scripts/check.py`：通过（1332 Python tests，Node checks，smoke package）。
- `npm --prefix frontend test`：128 项通过。
- `npm --prefix frontend run build`：通过。
- Windows 原生 smoke（PATH 中无用户 Python、Docker、Node）：START 0、`/health` True、admin/admin 登录 200、重复 START 0、STOP 0。
- 最终 EXE 隔离安装：原生后台启动，`/health` 返回 1.5.0，`viewer_presentation=native`，停止成功；安装日志显示没有 Docker 检查。
- Docker 原生旧 1.3.0 fixture 覆盖升级到 1.4.0 已验证健康、版本、配置和数据保留；原生 1.5.0 是新 Windows 交付形态。
- 真实网络链路曾验证：宿主 HTTP → 账号固定 SOCKS5 → HTTPS；labs.google 200、flow.google.com 200、accounts.google.com 302。没有用真实账号 Cookie 生成内容。

## 修复前问题与排查依据（历史）

### A. Cloak 直接登录“邮箱验证未通过”误报

用户截图中 Google Flow 页面已经明确显示已登录邮箱 `thelayya@gmail.com`，但点击“完成登录并添加”后界面显示：`登录验证未通过，请核对邮箱并完成 Google 登录`。

已确认：

- Cloak 本身可以打开窗口，Google 页面确实登录成功。
- 同一链路已有其他 Cloak 账号成功过，暂时没有证据是 Cloak 普遍限制。
- `DirectProfileLogin.complete()` 调用 `viewer.refresh(force=True)`。
- 强制刷新依次执行：
  1. `oauth_csrf`：`labs.google/fx/api/auth/csrf`
  2. `oauth_signin`：`labs.google/fx/api/auth/signin/google`
  3. `oauth_redirect`：访问 `accounts.google.com` 授权回调
  4. `oauth_confirmation`：若仍停留 Google 登录/挑战页
  5. `session_read`：读取 Labs session
  6. `credits_read`：读取 Flow credits
- `ProfileSessionRefresher` 已记录 `_renewal_step`；`TokenManagerAccountProbe` 已记录 `network_step`。
- 但 `TokenManager._refresh_at()` 目前把 `RefreshFailure`/网络异常最终压成 `False` 并写入 token 的 `refresh_error`；`DirectProfileLogin.complete()` 只看到 False，抛通用 422 文案，前端看不到阶段、reason、proxy/network 细节。
- 应读取最新 token 的 `refresh_error`、`refresh_status`、`browser_profile_error`、阶段字段，返回结构化错误；前端按 `login_required`、`transient_error`、`session_invalid`、`identity_mismatch`、`credits_invalid`、`proxy_error` 区分提示，并保留“继续登录/重试”入口。
- 不要直接展示 raw exception、Cookie、AT、代理凭据；可展示阶段和脱敏原因。
- 当前没有重试真实账号，没有修改生产 DB。

相关代码：

- `src/services/browser/profile_login.py`：`complete()` 目前在 `viewer.refresh` False 时抛通用提示。
- `src/services/token_manager.py`：刷新失败状态和 `refresh_error` 写入处。
- `src/services/browser/profile_session_refresher.py`：`_renewal_step` 和 OAuth 流程。
- `src/services/browser/token_account_probe.py`：`network_step`。
- `src/services/browser/profile_transfer.py`：错误阶段映射可复用。
- `frontend/src/views/AccountsView.vue`：Cloak 登录弹窗与状态提示。
- `tests/test_profile_viewer.py`、`tests/test_profile_browser_routes.py`、前端 `admin.test.mjs`。

### B. Docker Profile 导出 → Windows 原生导入失败

用户截图任务显示：`1 / 1 项 · 新增 0 · 更新 0 · 失败 1 · 0 秒`，这与同步 `_validate()` 在启动目标浏览器前立即拒绝一致。

已确认：

- `ProfileTransfer._validate()` 当前要求源/目标完全相同：
  - `browser.launch_options` 完全相等；
  - `runtime.cloakbrowser` 完全相等；
  - `runtime.playwright` 完全相等；
  - Chromium version 字符串完全相等；
  - `_check_context()` 又比较 Chromium version；
  - user_agent/language/languages/timezone/platform 完全相等。
- Docker 源是 Linux/Cloak/Chromium；Windows 原生目标是 Windows/Cloak bundled Chromium/不同 Playwright，故 Docker→Windows 必然 `runtime_mismatch` 或 `environment_mismatch`，耗时接近 0 秒。
- 这不是邮箱损坏，也不是单一 Cloak 限制；迁移契约把平台运行环境误当成 Profile 身份，策略过严。
- `storage_state` 的 Cookie scope 仍应严格保留 Google/Flow/Labs 域；fingerprint seed、固定代理 binding、账号 identity check、Profile close/cache 语义仍要保留。
- 目标实现建议：
  1. 允许跨平台迁移（Docker Linux → Windows native、反向也可）；
  2. 不比较源/目标 OS、UA、语言、时区、viewport、Chromium patch 级版本；
  3. 仍拒绝外部/不受支持的 launch 参数、fingerprint seed 冲突、代理绑定变化、Cookie scope 越界；
  4. 目标端以自身 Cloak runtime 创建新 Profile，注入一次受限 storage state；
  5. 强制 OAuth renewal + session identity + credits probe，失败具体标记 `network_step`；
  6. 目标环境 snapshot 以目标环境重写，不能拿源 environment 强行覆盖；
  7. 重试旧失败 Profile 时允许同 seed/同 binding 原地继续，不允许绕过 identity/Google probe。
- 现有迁移 tests 有一个 `test_invalid_scope_runtime_and_native_api_reject_without_persistence`，其中应把“完全不同 runtime 必须拒绝”改成“跨平台 runtime 允许/目标重建”，并新增 Docker→Windows synthetic fixture；保留 launch unsupported、foreign cookie、seed conflict、proxy binding change 拒绝测试。

相关代码：

- `src/services/browser/profile_transfer.py`：`_validate()`、`_check_context()`、`_cold_check()`、`_resume_transfer()`。
- `src/services/browser/environment.py`：`EnvironmentSnapshot`。
- `tests/test_profile_transfer.py`：迁移契约、跨环境、失败重试。
- `tests/test_profile_import_jobs.py`：后台任务阶段和 0 秒同步失败。
- `frontend/src/views/AccountsView.vue`：导入任务显示和失败详情。

## 下一会话开始方式

1. 读取本文件和 `docs/WINDOWS_INSTALL.md`。
2. 确认 `git status` 与 `git log -3`，不要重复 Windows runtime 打包。
3. 先跑定向测试：

```powershell
python -m unittest tests.test_profile_transfer tests.test_profile_import_jobs tests.test_profile_viewer tests.test_profile_browser_routes
npm --prefix frontend test
```

4. 只改登录错误阶段和跨平台 Profile 迁移；修复后再跑全量 `python scripts/check.py`、前端测试和原生 smoke。
5. 不重试真实账号，不覆盖生产 Profile，不提交 Cookie/数据库/日志/原生 runtime 到 Git。
