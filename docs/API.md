# lay4flow-remote API 接入

服务提供图片与视频生成接口。OpenAI 风格 `/v1` 使用 `Authorization: Bearer YOUR_API_KEY`；这里的 Key 是本服务管理设置中的 API Key，与 Google 账号 Cookie、Session Token 分开。

OpenAI SDK 的 `base_url` 填 `https://your-host.example/v1`。公共 API 文档为 `/api-guide`，Swagger 为 `/api-docs`，原始 schema 为 `/v1/openapi.json`。模型能力以运行服务的 `/v1/models` 为准。

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/v1/models` | 模型目录、默认参数、能力组合与推荐端点 |
| GET | `/v1/models/{model}` | 指定模型的能力 |
| GET | `/v1/models/aliases` | 生成配置使用的兼容名称 |
| POST | `/v1/chat/completions` | 图片、视频生成，支持 SSE |
| POST | `/v1/responses` | 媒体生成的 Responses 风格入口 |
| POST | `/v1/images/generations` | JSON 图片生成 |
| POST | `/v1/images/edits` | multipart 参考图或图片编辑 |
| POST | `/v1/videos` | 创建异步视频任务 |
| GET | `/v1/videos`、`/v1/videos/{id}` | 视频列表与状态 |
| GET | `/v1/videos/{id}/content` | 下载已完成 MP4 |
| DELETE | `/v1/videos/{id}` | 删除终态任务记录 |
| GET | `/models`、`/v1beta/models` | Gemini 风格模型目录 |
| POST | `/v1beta/models/{model}:generateContent` | Gemini 风格非流式生成 |
| POST | `/v1beta/models/{model}:streamGenerateContent` | Gemini 风格 SSE |

OpenAI 兼容接口（`/v1/*`）使用 `Authorization: Bearer YOUR_API_KEY`，不接受 `x-goog-api-key` 或 `?key=`。Gemini 兼容接口（`/models/*`、`/v1beta/models/*`）仍支持 `x-goog-api-key`、`?key=` 或 Bearer。认证失败使用 `error.type=authentication_error`。管理后台使用管理员 Cookie，与外部 API 分别认证。

## 后台账号导入（管理员）

这些接口使用管理员 Cookie 或管理员会话 Bearer，响应均为 `no-store`。

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/api/profile-transfer/jobs` | 提交 `{ "bundles": [PROFILE_OR_LEGACY_ACCOUNT], "request_id": "UNIQUE_ID", "proxy_pool_id": 1 }`，立即返回 `{ "success": true, "job": ... }` |
| GET | `/api/profile-transfer/jobs` | 获取当前管理员会话的近期任务，刷新或切页后恢复进度 |
| GET | `/api/profile-transfer/jobs/{id}` | 查看状态、逐项阶段、耗时与错误原因 |
| POST | `/api/profile-transfer/jobs/{id}/retry` | 只将失败项重新排队，成功项保持原样 |

原生 Profile、旧版账号及混合列表共用单工作队列，每任务最多 500 项。同一 `request_id` 与相同内容复用任务，内容不同返回 409。状态只含账号、进度和结果，原始登录态留在服务内存；成功项立即释放输入，失败项供重试。终态最多保留 24 小时 / 100 条，服务重启后重新提交 JSON，已导入账号和 Profile 保留。旧同步端点 `/api/profile-transfer/import` 保持兼容。

指纹浏览器账号登录使用 `/api/plugin/import/extension.zip` 下载当前管理站点专用扩展；管理员在同源页面调用 `POST /api/plugin/import/start`，提交 `{}`，取得五分钟有效的一次性会话。开始接口不访问代理池，Google 登录沿用指纹浏览器自身的网络设置。扩展将当前浏览器的作用域 Cookie、环境、标签页及 Cookie store 标识提交到 `/api/plugin/import/complete`，以 `Authorization: Bearer import:SESSION_ID.NONCE` 和当前管理员 Cookie 共同认证。扩展不读取或修改浏览器代理；服务端收到登录态后，按现有导入规则选择并保存账号固定出口，后续保持绑定。提交内容中的代理字段不会覆盖服务端选择。服务端通过当前 Flow 身份接口、Profile 正常关闭及冷启动检查后才保存账号；重复成功提交返回同一结果。会话随服务重启失效，现有账号不会被覆盖，验证码仍使用 `remote_browser`。

## 图片

```sh
curl https://your-host.example/v1/images/generations \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"gemini-3.1-flash-image","prompt":"窗边的一只猫","n":1,"response_format":"url"}'
```

响应的 `data[]` 包含 `url` 或 `b64_json`。一次生成一张图片。`1k/2k/4k` 为模型输出档位，基础结果的精确像素由上游决定。放大步骤失败时可能返回基础结果，应同时查看生成进度和结果信息。

使用 Chat 传参考图时，最后一条用户消息中放文本与有序的 `image_url`。图片参数放在 `generationConfig.imageConfig`：

```json
{
  "model": "gemini-3.1-flash-image",
  "messages": [{"role": "user", "content": [
    {"type": "text", "text": "保留主体，把背景换成花园"},
    {"type": "image_url", "image_url": {"url": "data:image/png;base64,BASE64_IMAGE"}}
  ]}],
  "generationConfig": {"imageConfig": {"aspectRatio": "16:9", "imageSize": "1k"}}
}
```

`POST /v1/images/edits` 接受 multipart 的 `model`、`prompt` 和 `image` 或重复 `image[]`，最多 10 张；图片限制及模型允许的组合以 OpenAPI 和模型目录为准。

## 视频

```sh
curl https://your-host.example/v1/videos \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: example-video-001" \
  -d '{"model":"veo_3_1","prompt":"海边日出，镜头缓慢推进","seconds":"8","size":"1280x720"}'
```

Videos 专用接口返回任务对象。保存任务 `id`，查询 `/v1/videos/{id}`；`completed` 后用同 Key 请求 `/content`。`queued` 表示接受任务，生成结果仍以终态为准。相同幂等键与相同请求复用原任务，改变参数返回冲突。运行中的任务在进程重启后标记失败，以免重复消费。

列表支持 `limit=20&after=<last_id>&order=desc`，通过 `has_more` 判断是否继续翻页。未完成的内容下载返回 `video_not_ready`，已过期媒体返回 `asset_expired`；删除进行中的任务返回 `video_in_progress`。

Chat 视频可在 `generationConfig.videoConfig` 中传 `mode`、`durationSeconds`、`resolution`、`aspectRatio` 和模型支持的其他字段。首尾帧使用显式 `mode=first_last_frame`，参考图按首帧、尾帧顺序排列。Veo 续写使用已有媒体的 `extend://MEDIA_ID`。Omni 视频编辑接受源视频和裁剪参数；先读取目录确认支持组合。

新增 `omni_1.2_flash`：文生、单图与参考图可选整数 3–15 秒，首尾帧按当前 Flow 目录开放 3–10 秒。11–15 秒、续写及 1080p 放大需 Plus/Pro/Ultra，4K 需 Ultra；账号调度和提交前实时目录校验均会检查权限。Omni 1.2 编辑可裁剪 3–15 秒，超过 10 秒需订阅。续写的 `mode=extend`、`durationSeconds=3..10` 表示追加秒数，可上传原视频并附 0–3 张参考图。工作台可直接从已有视频作品选择「续写视频」，无需填写媒体 ID；Chat 接口以 `video_url` 传原视频，已有 Flow 媒体的 `extend://MEDIA_ID` 引用保留给程序化集成。旧 `omni_1.1_flash` 保留独立调用，不自动切换新模型或价格。

```json
{"model":"omni_1.2_flash","prompt":"海边日出，镜头缓慢推进","seconds":"15","size":"1280x720"}
```

该示例用于 `POST /v1/videos`，需要已配置的订阅账号；仍按标准鉴权与 `Idempotency-Key` 创建任务。能力来自官方目录和本地模拟协议检查，真实付费生成验收单独记录。

Videos 续写入口可通过 multipart 的 `input_video` 上传 3–60 秒、最多 100 MiB 的 MP4，并传 `generationConfig.videoConfig.mode=extend`；`seconds` 表示追加秒数，参考图通过 `input_reference` 或重复的 `input_reference[]` 提供。上传、媒体 ID 与原账号绑定由后台处理，界面无需用户填写标识。程序化集成也可用 `input_video_media_id` 直接引用原 Flow 媒体。服务读取处理后的真实时长，校验最终结果是否等于原片加追加段；1080p/4K 续写要求该总时长不超过 15 秒。

SSE 客户端需检查流内错误与完成标记；HTTP 200 仅表示流已建立。客户端、网关和上游请求均应设置足够的超时，避免连接中断后盲目重发付费生成。

## Python SDK

```python
import os
from openai import OpenAI

client = OpenAI(
    base_url=os.environ["LAY4FLOW_BASE_URL"],
    api_key=os.environ["LAY4FLOW_API_KEY"],
    timeout=2400.0,
    max_retries=0,
)
result = client.chat.completions.create(
    model="gemini-3.1-flash-image",
    messages=[{"role": "user", "content": "窗边的一只猫"}],
    extra_body={"generationConfig": {"imageConfig": {"aspectRatio": "16:9", "imageSize": "1k"}}},
)
print(result.choices[0].message.content)
```

`extra_body` 内容合入 HTTP JSON 顶层。环境变量示例值：`LAY4FLOW_BASE_URL=https://your-host.example/v1`。

## 管理页请求日志

管理员登录后，`GET /api/logs` 返回轻量列表，新增 `attempt_count`（含首次）、`retry_count` 和 `account_count`。旧记录的这些字段为 `null`，表示当时未记录次数。

`GET /api/logs/{id}` 按需返回 `attempt_history`。其中 `attempts` 按发生顺序保存账号、阶段、结果、耗时、错误、命中规则、冷却或等待时长及后续动作。同一创作请求的同账号重试和换号重试更新同一条日志；视频提交受理后仍需等待最终结果。逐次记录仅适用于启用记录后的请求，旧数据不会推测补齐。

## 部署前提与边界

Flow 生成需要已验证且启用的账号、足够额度、可用的账号代理，以及已配置的打码 API Key。打码服务地址固定为 **<https://cap.tk218.top/>**，点击前往申请 Key，再在「工坊设置 → 远程有头打码」保存；缺少 Key 时显示全局配置提醒。服务契约见 [远程验证码文档](REMOTE_BROWSER_FINGERPRINT.md)。本服务按媒体能力接入，不承诺实现 OpenAI 全部资源；`Audio`、`Embeddings`、`Files`、`Batches` 等资源未列入 Flow `/v1`，以 schema 中实际端点为准。Vids 使用独立会话与配额，其独立语音入口见 [Vids API](VIDS_API.md)。

媒体下载链接有有效期，应及时保存。配置公开缓存 URL 时填写外部客户端可访问的 HTTPS 地址。下游 New API / One API 接入使用 [固定规格 SKU 入口](DOWNSTREAM_COMPAT.md)，Vids 使用 [独立 provider 路径](VIDS_API.md)。
