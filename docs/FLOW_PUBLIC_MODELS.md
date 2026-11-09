# lay4flow-remote 模型与参数

`GET /v1/models` 返回公开模型及 `capabilities`、`defaults`、`supported_endpoints`、`recommended_endpoint`。Gemini 风格目录与其保持一致。模型名称表示产品入口，尺寸与质量通过参数选择。

| 模型 | 用途 |
|---|---|
| `gemini-3.1-flash-image` | Nano Banana 图片生成与参考图 |
| `gemini-3.1-flash-image-lite` | Lite 图片生成与参考图 |
| `gemini-3.0-pro-image` | Pro 图片生成与参考图 |
| `imagen-4.0-generate-preview` | Imagen 兼容入口 |
| `veo_3_1` | Veo 视频，按目录选择 fast / quality / lite |
| `omni_1.2_flash` | Omni 1.2 视频生成、编辑与续写 |
| `omni_1.1_flash` | 原版 Omni 1.1，独立保留；上游计划 2026-12-05 停用 |

图片参数放 `generationConfig.imageConfig`，视频参数放 `generationConfig.videoConfig`。图片常用 `aspectRatio`、`imageSize`；视频常用 `mode`、`quality`、`durationSeconds`、`resolution`、`aspectRatio` 和 `priority`。支持组合以模型的 `capabilities.combinations` 为准，不将不同组合随意拼接。

| 场景 | 参数与素材 |
|---|---|
| 图片生成 | 文本，可附模型支持数量的参考图 |
| 视频文生 | `mode=text_to_video` 与文本 |
| 首尾帧 | `mode=first_last_frame`，按顺序提供首帧、尾帧 |
| 参考素材视频 | 按模型能力提供有序参考图 |
| Veo 续写 | `mode=extend`，已有媒体 `extend://MEDIA_ID` |
| Omni 编辑 | `mode=edit`，源视频、可选参考图、裁剪起止时间 |

Omni 1.2 原生支持 360p、720p：文生、单图和参考图为整数 3–15 秒；当前实际模型目录中的首尾帧只列 3–10 秒，按该范围开放。无订阅账号最多 10 秒；11–15 秒及续写需要 Plus、Pro 或 Ultra。编辑使用 `trimStartMs`、`trimEndMs` 选择 3–15 秒原片段，超过 10 秒同样需要订阅。续写使用 `mode=extend`，上传 3–60 秒 MP4 原视频后追加 3–10 秒，可附最多 3 张参考图；时长表示追加长度。界面可从已有视频作品直接选择续写，上传和内部媒体 ID 由后台处理。API 同时保留已有 Flow 媒体 ID 入口供集成调用。

1080p、4K 为生成后放大，分别需要订阅、Ultra；当前账号的 GetModels 能力与价格许可会在付费提交前再次核对。未知套餐不推断为付费账号。Omni 1.1 的调用名称、时长与价格不被悄悄改到新版，历史请求继续按原模型处理。

续写的高清放大受当前放大器目录约束：原片加追加段的成品总长须不超过 15 秒；超出时应选择原生 360p/720p，服务会在付费提交前拒绝不支持的高清组合。

Flow 点数与 Gemini API 美元价格不同。按 2026-11-09 官方点数表，Omni 1.2 的 360p 前 10 秒每秒 1 点，11–15 秒每额外秒 2 点；720p 分别为 3 点与 6 点。上游可能调整，以 Flow 当前模型目录和结算为准。官方：[功能](https://support.google.com/flow/answer/16352836?hl=en)、[点数与停用计划](https://support.google.com/flow/answer/16526234?hl=en)、[续写与编辑](https://support.google.com/flow/answer/16935718?hl=en)。

图片 `1k/2k/4k` 与视频分辨率档位可能包含生成后的放大步骤；基础结果和放大结果应分别判断。目录描述实现支持范围，账号权限、余额与上游策略仍影响实际生成。兼容名称可通过 `/v1/models?include_legacy=true` 查看，新接入建议使用公开入口。

调用示例见 [API 文档](API.md)，用于网关计费的固定规格名称见 [SKU 文档](DOWNSTREAM_COMPAT.md)。
