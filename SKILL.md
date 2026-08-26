---
name: imggen
description: 统一多模型图像生成入口：GPT-Image-1/2（OpenAI 官方 API 或任意 OpenAI 兼容第三方端点）、Nano Banana / Gemini 图像模型（Google AI Studio Key）、MidJourney（适配器预留）。Use whenever the user asks to 生成/画/出图/画一张/做封面/海报/配图/头像/logo/mockup/编辑图片/参考图改图/inpaint/批量生图, or mentions gpt-image, DALL·E, nano banana, Gemini image, Midjourney, 文生图, AI绘图 — even if they don't name a specific model. Supports official API keys and custom third-party base_url profiles.
---

# imggen — 多模型图像生成

一个 CLI 打通多家图像模型：**openai** kind（GPT-Image 系列，官方端点或任意 OpenAI
兼容中转）、**gemini** kind（Nano Banana 系列，Google AI Studio Key）。MidJourney
为预留适配器位（见 `references/providers.md` §MidJourney）。

CLI 位置：本 skill 目录下 `scripts/imggen.py`。Python 3.10+ 标准库运行，零第三方依赖：

```powershell
python "<skill-dir>/scripts/imggen.py" <command> ...
```

## 0. 首次使用前：确认 Key 已配置

Key 只存本地（环境变量或 `~/.imggen/config.json`），永远不进聊天记录。
**推荐让用户在本地终端跑一次交互式向导**（Key 输入不回显、自动连通测试、
自动选模型），agent 不要代跑交互命令：

```powershell
python scripts/imggen.py setup           # 交互式向导：渠道→base_url→Key→模型→测试
python scripts/imggen.py providers       # 查看 profile 与 key 状态（set/unset）
python scripts/imggen.py doctor          # 全渠道体检；--fix 自动修正 base_url
python scripts/imggen.py models          # 列出渠道可用模型（远端拉取+内置清单）
python scripts/imggen.py init-config     # 只要配置模板时使用
```

非交互脚本场景可用 `setup --yes --kind openai --provider 名 --base-url URL
--model 模型 --api-key-env 名` 直接写入（Key 需已存在于环境变量或 .env）。
若用户尚未配置 Key：告知需要哪个环境变量（如 `OPENAI_API_KEY` / `GEMINI_API_KEY`，
或 profile 里自定义的 `api_key_env`），请用户在本地设置后告知，**不要让用户把
Key 粘贴到对话里**。配置格式详见 `references/config-and-keys.md`。

## 1. 工作流

1. **分类请求**：新图（text-to-image）/ 参考图编辑 / 局部重绘（mask）/ 批量任务。
   凡是"修改/替换/换背景/保持…不变/融合这几张"类请求一律属于**编辑**，
   必须带参考图执行（见第 5 步的 `edit` 子命令）。
2. **选渠道**：用户点名了模型就按名字映射（见 §2）；否则用 default_provider。
   用户说"走中转 / 用我的 apinebula / 第三方"时，选对应自定义 profile。
3. **构造 prompt**：保留用户的细节描述；图内出现文字时逐字引用并要求精确渲染。
   需要风格模板或提示词修复技巧时读 `references/prompt-craft.md`。
4. **预检（可选但推荐用于昂贵调用）**：加 `--dry-run` 打印请求计划（自动脱敏）。
5. **执行**：
   ```powershell
   # 新图（默认渠道）
   python scripts/imggen.py gen -p "..." -s portrait -o out/poster.png
   # 图生图：用显式 edit 子命令（强制校验 --ref，忘带会直接报错而不是静默重新生成）
   python scripts/imggen.py edit -p "把背景换成暖色摄影棚，保持产品与标签不变" `
       -r input/product.png
   # Gemini Nano Banana Pro，竖版
   python scripts/imggen.py gen --provider gemini -p "..." -s 1080x1920
   ```
   说明：`gen` 带 `-r` 也支持编辑（自动切 edits/generateContent 端点）；
   `edit` 是同一引擎的强制防呆入口，二选一即可，编辑场景推荐 `edit`。
6. **校验输出**：CLI 会打印绝对路径；确认文件存在且尺寸合理。涉及精确文字/
   构图的，建议用视觉工具回看再交付。
7. **报告**：输出绝对路径 + 所用 provider/model/size + 一句可选的改进建议。

## 2. 渠道与模型选择

| 用户提到 | provider | model |
|---|---|---|
| GPT-Image-2 / gpt-image-2 / OpenAI 出图 | openai（官方或中转 profile） | gpt-image-2 |
| GPT-Image-1 / dall-e-3 | openai | 对应模型名 |
| Nano Banana Pro（最强，支持 2K/4K、搜索接地） | gemini | gemini-3-pro-image-preview |
| Nano Banana（快、便宜） | gemini | gemini-2.5-flash-image |
| MidJourney / MJ | ⚠️ 尚未实现，如实告知并给替代方案 | — |

## 3. 参数速查

| 参数 | 说明 |
|---|---|
| `-p/--prompt` | 提示词；编辑时为修改指令 |
| `-s/--size` | `1080x1920` 字面量，或别名 `portrait`(1080×1920) / `landscape`(1920×1080) / `square` / `tall`(1024×1536) / `wide`(1536×1024)；缺省 = 平台默认 |
| `-r/--ref IMG...` | 参考图（≥1 张触发编辑模式；OpenAI 走 images/edits，Gemini 走多模态输入） |
| `--mask` | PNG 蒙版局部重绘（仅 OpenAI） |
| `-q/--quality` | low/medium/high/auto（仅 OpenAI gpt-image 系列有效） |
| `-n N` | 张数（Gemini 通过多次调用实现） |
| `-o/--out` | 输出路径；缺省写入配置的 output_dir 并按时间戳命名 |
| `--dry-run` | 打印脱敏请求计划，不发网络 |

尺寸映射规则（CLI 自动完成）：OpenAI 直传 WxH；Gemini 自动换算最近邻宽高比 +
1K/2K/4K 分档（如 1080×1920 → 9:16 + 2K）。

## 4. 能力差异矩阵（选渠道时用）

| 能力 | openai (gpt-image-2) | gemini (Nano Banana) |
|---|---|---|
| 多参考图融合 | ✅ edits 多图 | ✅ inline_data 多图 |
| mask 局部重绘 | ✅ | ❌ |
| quality 分档计费 | ✅ low/medium/high | ❌ 按 token 计费 |
| 大尺寸 | 任意 WxH(≤4096) | 固定比例 + 最高 4K |
| 中文文字渲染 | 强 | 强 |
| 免费额度 | ❌ 按量计费 | ✅ AI Studio 免费层可用 |

## 5. 安全规则

- API Key 只来自环境变量或本地配置文件；不打印、不落聊天、不写进 prompt/日志。
- 报错输出已做脱敏；不要在回复中复述完整 Key 或 Authorization 头。
- 不要替用户"顺手"创建含真实 Key 的文件；模板一律占位符。

## 6. 失败处理

- **base_url 路由错误**（空响应/404/HTML 页）：CLI 自动尝试补/去 `/v1` 的变体
  地址并写回配置（详见 `references/config-and-keys.md` §base_url 自动探测）。
- 429/5xx：CLI 已自动退避重试 3 次；仍失败则建议降 quality、缩小尺寸或稍后再试。
- 4xx：不要盲目重试；把 API 错误信息如实转述（已脱敏）给用户。
- Gemini `blockReason`：安全拦截，建议调整 prompt 措辞而非换渠道硬闯。
- 配置缺失（exit 2）：按 §0 引导用户配置。

## 7. 深入资料（按需读取）

- `references/config-and-keys.md` — 配置文件格式、多渠道 profile 示例（含第三方
  中转站）、Key 安全约定
- `references/providers.md` — 各家端点细节、参数映射表、错误码语义、MidJourney
  接入计划（midjourney-proxy 规范摘要）
- `references/prompt-craft.md` — 提示词结构与各模型差异、风格模板指引
