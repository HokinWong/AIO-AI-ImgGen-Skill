# 配置与 Key 管理

## 配置文件查找顺序

1. `$IMGGEN_CONFIG` 环境变量指定的路径
2. 当前目录 `./.imggen.json`
3. `~/.imggen/config.json`

运行 `python scripts/imggen.py init-config` 写出默认模板（同样尊重上述顺序）。

## 配置结构

```json
{
  "default_provider": "openai",
  "output_dir": "output/imagegen",
  "providers": {
    "<profile名>": {
      "kind": "openai | gemini | mj",
      "base_url": "https://...",
      "model": "默认模型",
      "api_key_env": "优先从该环境变量读 Key",
      "api_key": "备用：直接写 Key（不推荐，推荐用环境变量）"
    }
  }
}
```

## 常用 profile 示例

### OpenAI 官方
```json
"openai": { "kind": "openai", "base_url": "https://api.openai.com/v1",
            "model": "gpt-image-2", "api_key_env": "OPENAI_API_KEY" }
```

### Google Gemini 官方（AI Studio Key，Nano Banana 有免费额度）
```json
"gemini": { "kind": "gemini",
            "base_url": "https://generativelanguage.googleapis.com/v1beta",
            "model": "gemini-3-pro-image-preview", "api_key_env": "GEMINI_API_KEY" }
```
Key 申请：https://aistudio.google.com/apikey （免费层即可生成 Nano Banana 图）。

模型对照：
- `gemini-3-pro-image-preview` = Nano Banana Pro（2K/4K、aspectRatio、搜索接地）
- `gemini-2.5-flash-image` = Nano Banana（快速、便宜）

### 第三方 OpenAI 兼容中转站（任意 base_url）
```json
"apinebula": { "kind": "openai", "base_url": "https://apinebula.com/v1",
               "model": "gpt-image-2", "api_key_env": "CODEX_API_KEY" }
```
兼容性说明：
- 任何实现了 `/v1/images/generations` 与 `/v1/images/edits` 的中转都可直接用。
- 中转返回 `url` 而非 `b64_json` 时 CLI 会自动下载。
- 部分中转对 `quality`/`output_format` 字段报错——这些字段只在显式传参时才会发送，
  遇到报错去掉 `-q`/`-f` 重试即可。

### 第三方 Gemini 兼容端点
把 `base_url` 换成中转地址（保持 `/v1beta` 风格路径），`kind` 仍为 `gemini`。
CLI 对不支持 `generationConfig.imageConfig` 的兼容层会自动降级重试一次。

## Key 安全约定

- **首选环境变量**：Windows 下 `[Environment]::SetEnvironmentVariable("GEMINI_API_KEY","<值>","User")`
  设置后重启终端生效；会话内临时用 `$env:GEMINI_API_KEY="<值>"`。
- **.env 文件（推荐）**：CLI 启动时自动加载 `$IMGGEN_ENV_FILE` 指定文件或
  `~/.imggen/.env`（格式 `KEY=VALUE`，`#` 注释；不覆盖进程已有环境变量）。
  例：把 gpt-image skill 的现有配置直接复用——
  `Copy-Item ~\.codex\skills\gpt-image\.env ~\.imggen\.env`
- 配置文件里的 `api_key` 字段是兜底方案；文件权限注意不要提交进 git。
- Agent 行为约束：绝不把用户 Key 写入聊天回复、prompt、日志或代码仓库；
  `providers` 子命令只显示 key 来源，永不显示值。

## 推荐配置方式：setup 向导

在本地终端运行 `python imggen.py setup`，按提示走完五步即可（无需手动编辑 JSON）：

1. 选渠道类型（OpenAI 兼容 / Gemini）
2. 填 base_url（可不含 `/v1`，连通测试会自动探测并采纳可用值）
3. 粘贴 API Key（**不回显**，写入 `~/.imggen/.env`，与配置文件分离）
4. 选默认模型（优先展示渠道远端实际存在的图像模型，失败则用内置清单）
5. 免费连通测试（GET /models，不产生生成费用）→ 写配置

脚本/自动化场景用非交互模式（Key 需已就位）：

```powershell
python imggen.py setup --yes --kind openai --provider myrelay `
  --base-url https://host --model gpt-image-2 --api-key-env MYRELAY_API_KEY
```

## base_url 自动探测

`base_url` 填错（最常见：漏了 `/v1` 前缀）时 CLI 会自动补救：

- **触发条件**：仅"路由类"错误——空响应体、HTTP 404、返回非 JSON（HTML 错误页）。
  鉴权错误（401/403）、参数错误（400）、限流（429）不会触发换路径重试
  （那些换路径也没用）。
- **变体顺序**：配置原样 → 补 `/v1` → 去 `/v1`（已含 `/v1`、`/v1beta` 等版本段时
  则反向：原样 → 去掉版本段）。尾斜杠自动规范化。
- **自动写回**：备选路径命中后，修正值会写回配置文件该 profile 的 `base_url`
  字段，下次直接生效；stderr 会打印修正记录。写回前会校验配置未被并发修改。
- `--dry-run` 的报告里有 `base_url_candidates` 字段，可预览将尝试的顺序。

示例：`"base_url": "https://slb-v1.api.fan"` → 首选失败后自动改用
`https://slb-v1.api.fan/v1/images/generations` 成功，并把 `/v1` 版本写回配置。

## 多渠道切换

- 一次调用：`--provider <profile名>`
- 改默认：配置文件里改 `default_provider`
- 一次覆盖模型：`-m/--model` 参数优先于 profile 的 `model`
