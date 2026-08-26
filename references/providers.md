# Provider 接口细节

## openai kind

| 模式 | 端点 | 格式 |
|---|---|---|
| 文生图 | `POST {base}/images/generations` | JSON |
| 参考图编辑 / inpaint | `POST {base}/images/edits` | multipart/form-data |

参数映射：

| imggen 参数 | API 字段 | 备注 |
|---|---|---|
| `-p` | `prompt` | |
| `-m` | `model` | gpt-image-2 / gpt-image-1 / dall-e-3 |
| `-s` | `size` | 直传 WxH；gpt-image-2 支持任意 ≤4096 尺寸；缺省不传 |
| `-q` | `quality` | 仅 gpt-image 系列；显式传参才发送 |
| `-n` | `n` | |
| `-r` | `image[]`（multipart） | 多参考图重复字段 |
| `--mask` | `mask`（multipart） | PNG alpha 蒙版，需配合 `-r` |
| `-f` | `output_format` | png/jpeg/webp |

响应处理：`data[].b64_json` 直接解码；`data[].url` 自动下载（部分中转的行为）；
`revised_prompt` 会打印到 stderr 供参考。

错误语义：429/500/502/503/504 自动退避重试 3 次（2s/4s/8s）；其余 4xx 不重试，
原样转述脱敏后的 `error.message`。

## gemini kind

端点：`POST {base}/models/{model}:generateContent`，鉴权头 `x-goog-api-key`。

请求构造：

```json
{
  "contents": [{ "parts": [
    {"text": "<prompt>"},
    {"inline_data": {"mime_type": "image/png", "data": "<b64参考图>"}}
  ]}],
  "generationConfig": {
    "responseModalities": ["TEXT", "IMAGE"],
    "imageConfig": { "aspectRatio": "9:16", "imageSize": "2K" }
  }
}
```

尺寸映射（imggen 自动完成）：

| 目标 WxH | aspectRatio | imageSize |
|---|---|---|
| 1080×1920 (portrait) | 9:16 | 2K |
| 1024×1024 (square) | 1:1 | 1K |
| 3840×2160 | 16:9 | 4K |

- 宽高比候选集：1:1, 2:3, 3:2, 3:4, 4:3, 4:5, 5:4, 9:16, 16:9, 16:21, 21:9；
  取与目标比例最近邻者。
- imageSize 分档：最长边 >2048 → 4K；>1024 → 2K；否则 1K。
- 兼容层不支持 `imageConfig` 时（报 Unknown name 等）自动降级去掉 imageConfig
  重试一次。
- 响应解析：`candidates[0].content.parts[]` 中 `inlineData.data` 解码为图片文件；
  text parts 打印为"模型附言"；`promptFeedback.blockReason` 表示安全拦截。
- `-n N` 通过 N 次调用实现（Gemini 单次单图）。

## MidJourney（预留适配器位）

MidJourney 至今无官方公开 API。将来按 **midjourney-proxy** 开源项目规范实现
（`kind: "mj"` 已在 CLI 中保留，调用时明确报错提示）：

- 提交：`POST {base}/mj/submit/imagine` `{prompt, base_url...}` → 返回任务号
- 轮询：`GET /mj/task/{id}/fetch` 直到 `status=SUCCESS` → 取图片 URL
- 后续动作：U1-U4 放大 / V1-V4 变体 → `POST /mj/submit/action`
  `{taskId, command:"U1"}`，再次轮询
- 参数风格：prompt 内嵌 `--ar 9:16 --v 7 --stylize` 等 MJ 原生参数
- 可对接的服务形态：自建 midjourney-proxy（挂 Discord 号池）、或任何兼容该
  REST 规范的托管服务（GoAPI/useapi.net/TTAPI 等）

实现时的注意点：异步任务必须带超时上限与失败态处理；账号池场景需要服务端
API-Key 之外的 secret 时沿用 api_key_env 机制。
