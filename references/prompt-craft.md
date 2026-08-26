# 提示词工程速查

## 通用结构（所有模型受益）

```
Asset type: 图片用途（公众号封面 / 产品图 / 海报…）
Primary request: 用户核心诉求
Scene/backdrop: 环境
Subject: 主体
Style/medium: 摄影 / 插画 / 3D / 平面设计…
Composition/framing: 构图、视角、留白
Lighting/mood: 光线与氛围
Color palette: 色彩
Text (verbatim): "图内出现的文字逐字引用"
Constraints: 必须保留的要素
Avoid: 禁止出现的要素
```

只写有信息量的行，不要为凑格式添加废话。图内文字务必**逐字引用**并要求精确渲染。

## 各模型差异

### GPT-Image 系列
- 吃结构化长 prompt；中文文字渲染强，海报/UI mockup 首选。
- 编辑指令要明确"保持什么不变"（invariants），防止无关细节漂移。
- quality 建议：草稿 low、探索 medium、含精确文字/最终稿 high。

### Nano Banana（Gemini 图像模型）
- 偏好自然语言完整描述（场景叙事式），而非标签堆叠；对话式追加修改也有效。
- 多参考图融合强（人物一致性、商品换背景）；描述每张参考图的角色。
- 想要相机感就写摄影语言（镜头、光圈感、布光），想要设计稿就写版式要求。
- Pro 版可指定"Square image."等句尾比例提示，同时 CLI 已自动映射 aspectRatio。

### MidJourney（预留）
- 参数内嵌 prompt：`--ar 9:16` 比例、`--v 7` 版本、`--stylize` 风格化强度。
- 短语堆叠 + 权重语法，不适合长指令句。

## 本地常用预设

- 公众号竖版配图：`-s portrait`（1080×1920）
- 温馨风格基线：暖黄/奶油白底色、圆润可爱字体、柔和光线

## 更大风格库

复杂风格需求可复用本机已装的 `gpt-image` skill 的 Reference Gallery
（162 个分类 prompt 模板，位于其 `references/gallery-*.md`），把选中的风格段落
嫁接到当前 prompt 后仍走本 CLI 执行。
