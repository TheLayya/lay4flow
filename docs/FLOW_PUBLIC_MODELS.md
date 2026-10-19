# lay4flow-remote 模型与参数

`GET /v1/models` 返回公开模型及 `capabilities`、`defaults`、`supported_endpoints`、`recommended_endpoint`。Gemini 风格目录与其保持一致。模型名称表示产品入口，尺寸与质量通过参数选择。

| 模型 | 用途 |
|---|---|
| `gemini-3.1-flash-image` | Nano Banana 图片生成与参考图 |
| `gemini-3.1-flash-image-lite` | Lite 图片生成与参考图 |
| `gemini-3.0-pro-image` | Pro 图片生成与参考图 |
| `imagen-4.0-generate-preview` | Imagen 兼容入口 |
| `veo_3_1` | Veo 视频，按目录选择 fast / quality / lite |
| `omni_1.1_flash` | Omni 视频生成与编辑 |

图片参数放 `generationConfig.imageConfig`，视频参数放 `generationConfig.videoConfig`。图片常用 `aspectRatio`、`imageSize`；视频常用 `mode`、`quality`、`durationSeconds`、`resolution`、`aspectRatio` 和 `priority`。支持组合以模型的 `capabilities.combinations` 为准，不将不同组合随意拼接。

| 场景 | 参数与素材 |
|---|---|
| 图片生成 | 文本，可附模型支持数量的参考图 |
| 视频文生 | `mode=text_to_video` 与文本 |
| 首尾帧 | `mode=first_last_frame`，按顺序提供首帧、尾帧 |
| 参考素材视频 | 按模型能力提供有序参考图 |
| Veo 续写 | `mode=extend`，已有媒体 `extend://MEDIA_ID` |
| Omni 编辑 | `mode=edit`，源视频、可选参考图、裁剪起止时间 |

图片 `1k/2k/4k` 与视频分辨率档位可能包含生成后的放大步骤；基础结果和放大结果应分别判断。目录描述实现支持范围，账号权限、余额与上游策略仍影响实际生成。兼容名称可通过 `/v1/models?include_legacy=true` 查看，新接入建议使用公开入口。

调用示例见 [API 文档](API.md)，用于网关计费的固定规格名称见 [SKU 文档](DOWNSTREAM_COMPAT.md)。
