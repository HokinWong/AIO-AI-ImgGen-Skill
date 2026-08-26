# AIO-AI-ImgGen-Skill

**All-in-One AI Image Generation Skill** —— 一个 CLI 打通多家图像生成模型。

- 🖼️ **多模型**：GPT-Image-1/2（OpenAI 官方 API，或任意 OpenAI 兼容第三方端点）、
  Nano Banana / Gemini 图像模型（Google AI Studio Key）、MidJourney（适配器预留）
- ⚙️ **零依赖**：纯 Python 标准库（Python 3.10+），一个文件即 CLI
- 🔒 **安全**：API Key 只存本地（环境变量或 `~/.imggen/.env`），日志/输出全程脱敏
- 🧭 **智能**：`base_url` 自动探测（漏 `/v1` 自动补全并写回配置）、路由错误自愈、
  免费 `GET /models` 连通性体检
- 💬 **易用**：交互式 `setup` 向导（Key 不回显、远端模型列表选择、自动连通测试）

## 快速开始

安装到 DSH / Claude Code 的 skills 目录（`~/.agents/skills/imggen/`），或直接调用
`scripts/imggen.py`：

```powershell
# 1) 交互式配置向导：选渠道 → 填地址 → 粘贴 Key（不回显）→ 选模型 → 连通测试
python scripts/imggen.py setup

# 2) 渠道体检（Key 状态 / 路由探测 / 输出目录），--fix 自动修正 base_url
python scripts/imggen.py doctor

# 3) 生成一张图（默认渠道 / 指定模型 / 竖版）
python scripts/imggen.py gen -p "一只戴红围巾的柯基幼犬，清晨窗台，柔光摄影" -s portrait
python scripts/imggen.py gen --provider slb -m gpt-image-2 -p "..." -s 1080x1920

# 4) 图生图 / 局部重绘（强制 --ref，防呆）
python scripts/imggen.py edit -p "把背景换成暖色摄影棚" -r input/product.png
python scripts/imggen.py edit -p "只改这里" -r a.png --mask mask.png

# 5) 批量生成（JSONL 任务文件，多线程）
python scripts/imggen.py batch -i tasks.jsonl --concurrency 4

# 6) 查看渠道可用模型 / 运行前预检（脱敏，不花钱）
python scripts/imggen.py models
python scripts/imggen.py gen -p "..." --dry-run
```

## 目录结构

```
AIO-AI-ImgGen-Skill/
├── SKILL.md                  # skill 定义（name/description/工作流），DSH/Agent 识别入口
├── scripts/
│   └── imggen.py             # 单文件 CLI（多线程 batch / 自动重试 / base_url 自愈）
├── references/
│   ├── config-and-keys.md    # 配置格式、多渠道 profile 示例、Key 安全约定
│   ├── providers.md          # 各家端点细节、参数映射、错误码语义、MJ 接入计划
│   └── prompt-craft.md       # 提示词结构、模型差异、风格模板
├── tests/
│   └── test_offline.py       # 离线回归（零网络零计费：脱敏/探测/编辑size/并发命名…）
└── config.example.json       # 配置模板（不含任何真实 Key）
```

## 支持矩阵

| kind | 模型 | 端点 | Key |
|---|---|---|---|
| `openai` | gpt-image-2 / gpt-image-1 / dall-e-3 | 官方 `api.openai.com` 或任意 OpenAI 兼容中转 | `OPENAI_API_KEY` 或自定义 `*_KEY` |
| `gemini` | gemini-3-pro-image-preview (Nano Banana Pro) / gemini-2.5-flash-image (Nano Banana) | Google AI Studio | `GEMINI_API_KEY` |
| `mj` | 适配器预留（midjourney-proxy 规范摘要见 providers.md） | — | — |

## 安全说明

- 你的 API Key **只存在本地**：环境变量、或 `~/.imggen/.env`（`setup` 向导写入时也不回显）。
- 本仓库**不含任何密钥**；`config.example.json` 仅是无 Key 的配置模板。
- 请勿提交 `.env`、`config.json` 或任何含真实 Key 的文件（仓库已内置 `.gitignore` 防护）。
- CLI 任何输出都不打印 Key 值（`providers` 只显示 set/unset，dry-run 只显示来源）。

## 许可

仅供个人与学习使用。调用第三方 API 请遵守对应服务条款。