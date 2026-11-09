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

`pageAction` 支持 `IMAGE_GENERATION` 或 `VIDEO_GENERATION`；`action` 同步传递相同值以兼容旧网关。`token_id` 为本服务内部账号 ID，可能为空。远程浏览器及其代理由远程服务配置。首帧与参考图上传直接使用账号 HTTP 会话，上传阶段不申请验证码。

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

当前 Cloak 账号的 Flow RPC 复用本任务的 HTTP 会话，UA、语言、Cookie 和 `curl-cffi` 传输身份来自已验证的账号 Profile 快照；远程打码返回的 UA、语言和 Cookie 不覆盖该账号会话。远端指纹仍用于旧 REST 路径及媒体下载。每次 solve、失败和重试均清理旧请求指纹。验证码成功返回仅表示取码成功，真实提交是否通过需单独验收。

远端响应中的 `proxy_url/proxyUrl/proxy` 不改变本服务的账号固定出口或媒体池选择。若远程服务要求验证码与提交共用出口，应在远程服务侧协调配置并实测确认。

| 方法与路径 | 请求体 | 用途 |
|---|---|---|
| `POST /api/v1/prefill` | 与 solve 相同上下文及 `clientKey` | 可选预热；404/405 后本进程跳过后续预热 |
| `POST /api/v1/sessions/{session_id}/finish` | `{"clientKey":"YOUR_REMOTE_SERVICE_KEY","status":"success"}` | 上游生成请求完成，释放远端会话 |
| `POST /api/v1/sessions/{session_id}/error` | `{"clientKey":"YOUR_REMOTE_SERVICE_KEY","error_reason":"upstream_error"}` | 报告失败与释放会话 |

健康检查使用 `GET /health`。验证码获取的临时网络/资源错误按本地重试次数退避；鉴权、余额和参数错误直接显示错误码。重试发生在生成提交前，结果未知的上传或生成请求不自动重发。

「最大尝试次数（含首次）」是单份作品共享的总预算，包含验证码获取失败与生成提交；「最多账号尝试次数」限制外层账号重新分配，不意味着每次取码失败都换号。打码服务的连接故障不计为账号错误，也不能靠更换账号修复。Flow 明确拒绝的生成请求，包括 `PUBLIC_ERROR_UNUSUAL_ACTIVITY`、403、429，统一交给外层错误规则与账号保护处理，并在总尝试和账号分配预算内继续。自定义规则决定等待、换号和状态；没有匹配规则的可重试拒绝，本次请求跳过失败账号再分配。永久内容或规格拒绝不额外添加默认重试。已受理或提交结果未知的请求不自动重发。

`RESOURCE_EXHAUSTED` 同样继续规则处理，例如 `RESOURCE_EXHAUSTED|LIMIT|5s` 会把当前账号冷却 5 秒，并为本次请求重新分配可用账号；不会把整个请求直接结束。规则按顺序首次匹配生效，额度耗尽等更具体的规则可放在其前面。

传输错误保留 `TRANSPORT_ERROR` 兼容错误码，并附带安全的原因分类，例如连接超时、响应超时、DNS 或 TLS 失败。旧日志只记录该总错误码时，不能倒推出具体连接原因；「取码成功」与「Flow 提交通过」是两个不同阶段。

连接阶段最长等待 10 秒，短请求不会超过本次请求的等待上限；不再把所有取码的连接阶段固定在 2.5 秒。配置中的超时控制等候服务响应，视频取码仍受单次最长 45 秒限制。增加连接等待只能减少短暂慢连接误判，持续网络故障仍会明确报错，不会自动改用账号代理或重复发送同一次 POST。

所有请求使用同一 Key：Bearer 头用于兼容旧网关，JSON body 的 `clientKey` 是当前服务的 owner 绑定字段。`session_id` 放入路径前编码。回调失败记录错误分类，不改变已取得的生成结果。控制面 JSON 传输直接连接配置服务，不继承环境代理。

## 验收

```sh
python -m unittest discover -s tests -p 'test_remote_browser*.py'
python scripts/check.py
```

这些检查使用模拟服务，覆盖请求契约、指纹作用域、Cookie 规则、错误回调和媒体头。部署环境另行验证远程服务连通、真实验证码、单张生成及成品下载；离线检查通过并不代表远程服务已可用。
