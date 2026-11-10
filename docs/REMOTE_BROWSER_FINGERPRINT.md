# lay4flow-remote 远程有头验证码契约

Flow 验证码仅使用 `remote_browser`，本发行版的服务地址固定为 **<https://cap.tk218.top/>**。用户在 **[打码服务申请入口](https://cap.tk218.top/)** 申请 API Key 后，进入「工坊设置 → 远程有头打码」填写 Key 并保存；超时默认 60 秒。设置对应 `[captcha].remote_browser_api_key` 与 `remote_browser_timeout`，服务地址由客户端固定使用。仓库包含客户端，不包含远程服务实现。

打码 API Key 是 Flow 图片、视频生成的必需配置。缺少 Key 时，登录后的全局提醒提供申请链接和设置入口。Vids 使用独立会话与 provider 协议，接入说明见 [Vids API](VIDS_API.md)。

## 获取验证码

```http
POST /api/v1/solve
Authorization: Bearer YOUR_REMOTE_SERVICE_KEY
Content-Type: application/json

{"clientKey":"YOUR_REMOTE_SERVICE_KEY","type":"RecaptchaV3TaskProxyless","pageAction":"IMAGE_GENERATION","project_id":"PROJECT_ID","action":"IMAGE_GENERATION","token_id":1}
```

`pageAction` 按官网操作传递：图片生成和图片放大使用 `IMAGE_GENERATION`，视频生成和视频放大使用 `VIDEO_GENERATION`，首帧、参考图及其他图片上传使用 `UPLOAD_IMAGE`。`action` 同步传递相同值以兼容旧网关；远程服务须支持该 action，客户端不会用其他 action 替代上传 Token。`token_id` 为本服务内部账号 ID，可能为空。远程浏览器及其代理由远程服务配置。图片上传仍使用账号原 HTTP 会话；视频上传沿用其独立的 Scotty 上传协议，不增加图片上传的验证码操作。

```json
{
  "success": true,
  "token": "CAPTCHA_TOKEN",
  "session_id": "SESSION_ID",
  "fingerprint": {
    "user_agent": "Mozilla/5.0 (...) Chrome/150.0.0.0 Safari/537.36",
    "accept_language": "en-US,en;q=0.9",
    "sec_ch_ua": "\"Chromium\";v=\"150\"",
    "sec_ch_ua_mobile": "?0",
    "sec_ch_ua_platform": "\"Windows\"",
    "session_cookies": []
  }
}
```

成功响应必须有非空 `token` 与 `session_id`；显式 `success=false` 或非零 `errorId` 为失败。客户端只保留允许的 `errorCode`，不回显服务响应正文、Key、Cookie 或验证码。`fingerprint` 可为空，嵌套有效字段优先，缺失字段可由同名顶层字段补齐，以兼容已有服务。应返回真实浏览器生成该验证码时的完整 UA、语言及 Client Hints。

Cookie 为含 `name/value/domain/path` 的对象列表，可附 `secure/expires`。客户端按域、路径、Secure 和有效期匹配；缺少域或路径的旧式 Cookie 字典会忽略。账号显式同名 Cookie 与 Authorization 优先，传输不自动跟随重定向。

## 请求环境与回调

提交使用官网 `20261108.05_p0` 已支持分支的 `batchexecute` RPC 和 JSON-proto 请求体；语言 `hl` 取自会话页面的 HTML `lang`，请求编号 `_reqid` 随逻辑 RPC 递增，表单显式使用 UTF-8 编码。官网 URL `hl` 优先级、请求号种子与页面级计数、批处理及 XSRF discriminator 并非完整复刻，不能宣称逐字节相同。已知项目的视频轮询复用项目页面；项目未知时保持根页面 fallback，不推测归属。混合非空 payload、错误和 XSRF 的响应按结果未知处理，不重发写请求；只有确定无非空 payload 的独立 XSRF 拒绝允许一次有界重放。这些协议修正不等于浏览器完整传输环境相同，也不代表异常流量拒绝已解除。

当前 Cloak 账号的 Flow RPC 复用本任务的 HTTP 会话，UA、语言、Cookie 和 `curl-cffi` 传输身份来自已验证的账号 Profile 快照；远程打码返回的 UA、语言和 Cookie 不覆盖该账号会话。登录、刷新与 Profile 仍使用账号固定代理；生成提交出口可在账号管理中单独或批量设置 `fixed` / `pool_random`。随机策略复用现有健康代理池分配规则，每份新任务选择一个出口，上传、生成、轮询及媒体地址查询均复用该任务会话；修改策略只影响新任务，不改账号绑定、Cookie 或 Profile。运行中仍检查原账号身份、固定绑定及 Profile epoch，绑定或凭据被更换时阻止继续写入。远端指纹仍用于旧 REST 路径及媒体下载。每次 solve、失败和重试均清理旧请求指纹。验证码成功返回仅表示取码成功，真实提交是否通过需单独验收。

远端响应中的 `proxy_url/proxyUrl/proxy` 不改变本服务的账号固定出口、任务提交出口或媒体池选择。若远程服务要求验证码与提交共用出口，应在远程服务侧协调配置并实测确认。

签名媒体下载使用独立会话和媒体代理池，不转发账号 Cookie 或 Authorization。官网下载代码采用 `fetch → Blob → blob URL`；本地使用通配 `Accept`，不再硬编码 Labs Referer、`same-origin` 或图片／视频元素的 Fetch Metadata，`curl-cffi` 也禁用默认导航头。wget 与系统 curl 不再补回旧 Referer；API 图片字节读取复用同一头规则。保留现有 UA fallback 和允许的指纹字段，不凭公共 JS 猜测真实页面 Referrer-Policy、Client Hints、跳转或 TLS 行为。

历史视频归档恢复只在原账号及原固定代理作用域读取原媒体，随后恢复上下文；不取码、不生成、不换账号。

| 方法与路径 | 请求体 | 用途 |
|---|---|---|
| `POST /api/v1/prefill` | 与 solve 相同上下文及 `clientKey` | 可选预热；404/405 后本进程跳过后续预热 |
| `POST /api/v1/sessions/{session_id}/finish` | `{"clientKey":"YOUR_REMOTE_SERVICE_KEY","status":"success"}` | 上游生成请求完成，释放远端会话 |
| `POST /api/v1/sessions/{session_id}/error` | `{"clientKey":"YOUR_REMOTE_SERVICE_KEY","error_reason":"upstream_error"}` | 报告失败与释放会话 |

健康检查使用 `GET /health`。验证码获取的临时网络/资源错误按本地重试次数退避；鉴权、余额和参数错误直接显示错误码。重试发生在生成提交前，结果未知的上传或生成请求不自动重发。

「最大尝试次数（含首次）」是单份作品原生生成尝试的总预算，包含该阶段验证码获取失败、生成提交、同账号重试和换号；旧账号分配次数仅为兼容旧客户端保留，不再截断总预算。图片上传独立获取一次验证码；生成后的图片放大沿用独立、有界的取码重试上限，尚未合并进原生生成尝试记录，不能把该预算解读为全部素材操作的总取码次数。放大失败保留已生成的原图。打码服务的连接故障不计为账号错误，也不能靠更换账号修复。Flow 明确拒绝的生成请求，包括 `PUBLIC_ERROR_UNUSUAL_ACTIVITY`、403、429，统一交给外层错误规则与账号保护处理，在总次数内继续。没有可用账号、永久拒绝或取消可提前终止，日志显示实际原因。没有匹配规则的可重试拒绝，本次请求跳过失败账号再分配。已受理或提交结果未知的请求不自动重发。

`RESOURCE_EXHAUSTED` 同样继续规则处理，例如 `RESOURCE_EXHAUSTED|LIMIT|5s` 会把当前账号冷却 5 秒，并为本次请求重新分配可用账号；不会把整个请求直接结束。规则按顺序首次匹配生效，额度耗尽等更具体的规则可放在其前面。

传输错误保留 `TRANSPORT_ERROR` 兼容错误码，并附带安全的原因分类，例如连接超时、响应超时、DNS 或 TLS 失败。旧日志只记录该总错误码时，不能倒推出具体连接原因；「取码成功」与「Flow 提交通过」是两个不同阶段。

连接阶段最长等待 10 秒，短请求不会超过本次请求的等待上限；不再把所有取码的连接阶段固定在 2.5 秒。配置中的超时控制等候服务响应，视频取码仍受单次最长 45 秒限制。增加连接等待只能减少短暂慢连接误判，持续网络故障仍会明确报错，不会自动改用账号代理或重复发送同一次 POST。

所有请求使用同一 Key：Bearer 头用于兼容旧网关，JSON body 的 `clientKey` 是当前服务的 owner 绑定字段。`session_id` 放入路径前编码。回调失败记录错误分类，不改变已取得的生成结果。控制面 JSON 传输直接连接配置服务，不继承环境代理。

## 验收

现代图片、视频生成以及图片上传、图片放大会在取码前读取并验证项目页面；取码后仍再次检查账号与固定出口绑定。图片资产输入在取码前校验，上传只提交一次，取消、解析失败和未知结果也释放远端会话。请求详情记录生成尝试的取码耗时、取码到实际 POST 的间隔、提交响应耗时和实际 POST 次数，缺失的旧数据保持未知；图片资产操作不会覆盖生成尝试的诊断。上游明确拒绝使用 `upstream_rejected` 回调；提交结果未知使用 `submission_unknown`，尚未提交使用 `not_submitted`，有效响应解析通过才发送成功完成回调。`PUBLIC_ERROR_UNUSUAL_ACTIVITY` 与 `PUBLIC_ERROR_UNUSUAL_ACTIVITY_TOO_MUCH_TRAFFIC` 本身不能证明验证码无效。

```sh
python -m unittest discover -s tests -p 'test_remote_browser*.py'
python scripts/check.py
```

这些检查使用模拟服务，覆盖请求契约、指纹作用域、Cookie 规则、错误回调和媒体头。部署环境另行验证远程服务连通、真实验证码、单张生成及成品下载；离线检查通过并不代表远程服务已可用。
