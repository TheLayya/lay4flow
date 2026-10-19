# lay4flow-remote 远程有头验证码契约

Flow 验证码仅使用 `remote_browser`，本发行版的服务地址固定为 **<https://cap.tk218.top/>**。用户在 **[打码服务申请入口](https://cap.tk218.top/)** 申请 API Key 后，进入「工坊设置 → 远程有头打码」填写 Key 并保存；超时默认 60 秒。设置对应 `[captcha].remote_browser_api_key` 与 `remote_browser_timeout`，服务地址由客户端固定使用。仓库包含客户端，不包含远程服务实现。

打码 API Key 是 Flow 图片、视频生成的必需配置。缺少 Key 时，登录后的全局提醒提供申请链接和设置入口。Vids 使用独立会话与 provider 协议，接入说明见 [Vids API](VIDS_API.md)。

## 获取验证码

```http
POST /api/v1/solve
Authorization: Bearer YOUR_REMOTE_SERVICE_KEY
Content-Type: application/json

{"project_id":"PROJECT_ID","action":"IMAGE_GENERATION","token_id":1}
```

`action` 支持当前请求使用的 `IMAGE_GENERATION` 或 `VIDEO_GENERATION`。`token_id` 为本服务内部账号 ID，可能为空。请求体仅携带上述上下文；远程浏览器及其代理由远程服务配置。

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

成功响应必须有非空 `token` 与 `session_id`；显式 `success=false` 为失败。`fingerprint` 可为空，嵌套有效字段优先，缺失字段可由同名顶层字段补齐，以兼容已有服务。应返回真实浏览器生成该验证码时的完整 UA、语言及 Client Hints。

Cookie 为含 `name/value/domain/path` 的对象列表，可附 `secure/expires`。客户端按域、路径、Secure 和有效期匹配；缺少域或路径的旧式 Cookie 字典会忽略。账号显式同名 Cookie 与 Authorization 优先，传输不自动跟随重定向。

## 请求环境与回调

有效远端 UA 用于本次生成请求，HTTP 头与 `curl-cffi` 传输指纹按同一浏览器版本选择。语言与 UA-CH 沿用远端有效值；缺失值回退既有配置，不臆造远端 Client Hints。每次 solve、失败和重试均清理旧请求指纹。

远端响应中的 `proxy_url/proxyUrl/proxy` 不改变本服务的账号固定出口或媒体池选择。若远程服务要求验证码与提交共用出口，应在远程服务侧协调配置并实测确认。

| 方法与路径 | 请求体 | 用途 |
|---|---|---|
| `POST /api/v1/prefill` | 与 solve 相同上下文 | 可选预热；失败不阻断生成 |
| `POST /api/v1/sessions/{session_id}/finish` | `{"status":"success"}` | 上游生成请求完成，释放远端会话 |
| `POST /api/v1/sessions/{session_id}/error` | `{"error_reason":"upstream_error"}` | 报告失败与释放会话 |

所有请求使用同一 Bearer 鉴权。`session_id` 放入路径前编码。回调失败记录错误分类，不改变已取得的生成结果。控制面 JSON 传输直接连接配置服务，不继承环境代理。

## 验收

```sh
python -m unittest discover -s tests -p 'test_remote_browser*.py'
python scripts/check.py
```

这些检查使用模拟服务，覆盖请求契约、指纹作用域、Cookie 规则、错误回调和媒体头。部署环境另行验证远程服务连通、真实验证码、单张生成及成品下载；离线检查通过并不代表远程服务已可用。
