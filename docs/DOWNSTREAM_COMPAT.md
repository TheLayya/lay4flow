# lay4flow-remote 下游网关接入

New API / One API 等网关可使用独立固定规格 SKU 入口。网关渠道根地址填 `https://your-host.example/compat`；OpenAI SDK 的 `base_url` 填 `https://your-host.example/compat/v1`。鉴权使用本服务的 Bearer API Key。

| 方法与路径 | 用途 |
|---|---|
| `GET /compat/v1/models` | 固定规格与定价所用 SKU 目录 |
| `GET /compat/v1/models/{model}` | 单个 SKU 的规格、模式与比例 |
| `POST /compat/v1/chat/completions` | 图片与视频，非流式或完成后 SSE |
| `POST /compat/v1/images/generations` | 单张图片 |
| `POST /compat/v1/images/edits` | multipart 图片编辑与参考图 |
| `POST /compat/v1/videos` | 异步视频生成或 Omni 编辑 |
| `GET /compat/v1/videos`、`GET /compat/v1/videos/{id}` | 列表与任务状态 |
| `GET /compat/v1/videos/{id}/content` | 下载已完成 MP4 |
| `DELETE /compat/v1/videos/{id}` | 删除终态记录 |
| `GET /compat/v1/openapi.json` | 协议描述 |

图片 SKU 按模型与档位拆分，例如 `flow-banana-pro-1k`、`flow-banana-flash-2k`。视频 SKU 固定质量、时长、分辨率与优先级，例如 `flow-veo-3-1-fast-8s-720p`。默认 `standard` 优先级省略后缀，仅 Relaxed 保留 `-relaxed`；旧 `-standard` 名称仍兼容调用，目录只列简短名称。完整列表从运行服务读取，不把 SKU 映射回聚合模型，否则会丢失计费规格。

```sh
curl https://your-host.example/compat/v1/chat/completions \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"flow-banana-pro-1k","messages":[{"role":"user","content":"窗边的一只猫"}],"stream":false}'
```

显式参数与 SKU 收费规格冲突时在上游提交前返回 400。画面比例可单独选择，清晰度由 SKU 决定。参考图可通过 Chat `image_url` 或 Images multipart 上传；首尾帧需显式指定模式。

成功完成的 Chat / Images 返回固定 1000 结算单位，由网关为每个 SKU 设置价格或倍率。它是固定结算基数，与上游真实文本 token 数分开。失败响应不作为成功结算；连接中断或超时应先确认结果，避免重复消费。

异步 Videos 的 `queued` 仅表示接受任务，创建和轮询不返回成功结算用量。网关需要具备匹配的异步任务适配，再按 `completed/failed` 结算。网关仅支持 Chat 时，使用 Chat 视频入口。幂等键可复用同请求；已执行的付费生成不会因下载失败重新提交。

原生聚合 API 继续使用 `/v1`，其模型目录与 SKU 目录分开。完整参数、返回示例和错误定义见网页 `/api-guide` 的下游接入模式。
