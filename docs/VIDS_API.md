# lay4flow-remote Vids API

Vids 是独立 provider，使用已保存的加密会话。Flow 账号登录成功后仍需通过 Vids 身份、文档和会话校验。默认账号池为空；未接入时不建立媒体会话。

管理界面使用 `/vids-accounts` 管理接入与恢复，`/vids` 提交创作，`/vids-api-guide` 查看完整协议。外部客户端 Base URL 为 `https://your-host.example/providers/vids/v1`，使用本服务 `Authorization: Bearer YOUR_API_KEY`，与管理 Cookie 分开。

| 方法与相对路径 | 用途 |
|---|---|
| `GET /models` | 当前支持的模型与能力 |
| `POST /images/generations` | 文生图与上传原图编辑 |
| `POST /audio/speech` | 语音生成 |
| `POST /videos` | 视频任务 |
| `GET /videos`、`GET /videos/{id}` | 列表与状态 |
| `GET /videos/{id}/content` | 下载完成视频 |
| `GET /vids/generations/{id}` | 原生成收据 |
| `POST /vids/generations/{id}/download` | 恢复原结果下载 |

完整 Swagger 为 `/providers/vids/api-docs`，schema 为 `/providers/vids/openapi.json`。以运行服务提供的字段与能力表为准。

生成必须提供 `Idempotency-Key`。后台为新请求选择可用账号，认领后固定到原账号与原收据；中断或未知状态不换号补发。素材由调用方上传，图片编辑无需客户端选择内部文档或场景 ID。重复同键同请求读取原收据，变更参数返回冲突。

已启用、具备图片或视频能力的就绪 Cloak Profile，在 Flow 导入或会话刷新后可触发一次后台接入，按需创建空白 Vids 文档；失败状态会保存，后续可在账号页显式重试。管理员也可手动接入，选择是否创建空白文档。身份不一致、Profile 验证未通过或会话失效时停止并显示原因。恢复使用原账号与原文档，不重放付费生成。图片、视频和语音的支持范围由 `/models` 描述，音乐不在当前范围内。

`[vids].accounts` 保留已配置绑定，界面接入的账号默认写入 `data/vids/bindings.json`。会话加密保存，解密密钥由绑定的 `session_key_env` 环境变量或独立的 `session_key_file` 文件提供；界面接入默认将密钥放在 `data/vids-secrets/`。跨服务器移动时同时保留原绑定、密文与独立保管的解密密钥；自定义到 `data/` 外的路径也需备份。私有配置、密钥、Cookie、原始收据均排除于 Git 和发布包。

本项目的远程有头验证码配置属于 Flow 生成链，Vids 会话与原有 provider 协议分别管理。离线回归使用合成会话；真实 Vids 付费生成及具体参数组合需在部署环境单独验收。
