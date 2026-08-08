# Design System: 智批π（ZhiPi π）

> **Single source of truth** for visual & interaction decisions.
> 实现映射：`demo/static/css/tokens.css` + `app.css`（本文件先于改码；改 UI 时以本文为准回写 token）。
> 调研依据：`docs/11-UI设计调研与风格定位.md`
> Skills：taste-skill · emil-design-eng · minimalist-ui · stitch-design-taste · redesign-existing-projects

---

## 0. Configuration Dials

| Dial | Level | Meaning for 智批π |
|------|------:|-------------------|
| **Creativity / Variance** | `5` | 工作台可预期；仅 Hero/答辩叙事允许编辑部非对称 |
| **Density** | `6` | 审卷队列与证据链偏 cockpit；营销首屏保持透气 |
| **Motion** | `3` | 日用反馈 100–200ms；禁装饰性长动画 |
| **Brand temperature** | Warm paper | 教育可信 + 作业纸面隐喻，非冷科技黑 |

**Design Read：** K12 教师可控的多模态批改与错因诊断工作台；信任优先；红黄绿是人机分工，不是皮肤。

---

## 1. Visual Theme & Atmosphere

### 1.1 一句话气质

**「批改台的编辑部」** —— 暖米纸张底、油墨字、孔版专色（Riso）做语义，不靠玻璃拟态与霓虹。打开像教案与试卷夹，不像 AI 创业公司官网。

### 1.2 氛围形容词

- 可信、冷静、可审
- 印刷感、纸面、专色
- 中高密度、对齐偏执
- 克制动效、按压有触觉

### 1.3 空间隐喻

| 区域 | 隐喻 | 密度 |
|------|------|------|
| Hero / 口令门 | 封面与扉页 | 低（3） |
| 01 提交 | 作业文件夹与收件台 | 中 |
| 02 结果 | 批注页（证据链） | 中高 |
| 03 教师台 | 审卷台（master–detail） | 高（7） |
| 04 看板 | 教研墙报 | 中高 |

### 1.4 质感

- **Paper grain：** 仅 Hero / 口令门；`opacity ≤ 0.12`；`pointer-events: none`；固定层。工作台默认 **关闭** grain（避免表格发脏）。
- **Halftone：** 仅装饰性专色块（统计 swatch、空夹），不铺正文。
- **阴影：** 染色阴影（带 ink 色相），禁纯黑 `rgba(0,0,0,.3)` 硬投影。

---

## 2. Color Palette & Roles

> 原则：中性色全暖；**业务交通灯三色保留**；主强调只留 **Riso Blue** 一条；禁止再引入紫色 AI 渐变。

### 2.1 纸与墨（Neutrals · warm）

| Token | Hex | Role |
|-------|-----|------|
| **Paper** | `#F4F1EA` | 页面 canvas |
| **Paper Warm** | `#EFEADF` | 分区底、toolbar、skeleton 底 |
| **Paper Deep** | `#E4DDCD` | 内凹、压线、active well |
| **Paper White** | `#FBF9F5` | 浮起 sheet / 卡片表面 |
| **Ink** | `#16130E` | 主文、主按钮、标题（非 `#000`） |
| **Ink Mid** | `#4A4237` | 次级文、表头 |
| **Ink Soft** | `#857B6C` | 说明、meta、placeholder 可再淡 |
| **Ink Faint** | `#B3A996` | disabled、folio 水印号 |
| **Rule** | `#D6CFBE` | 1px 结构线 |
| **Rule Soft** | `#E6E0D2` | 网格线、弱分割 |

### 2.2 主强调（单一 Accent）

| Token | Hex | Role |
|-------|-----|------|
| **Riso Blue** | `#2B41C8` | 链接、focus ring、主强调 CTA、文件夹容器色 |
| **Riso Blue Lt** | `#E5E7F8` | 选中浅底、tag 底 |
| **Folder Deep** | `#1E2E8E` | 夹体厚度 |
| **Folder Soft** | `#7C8AE0` | 夹面高光 |
| **Folder Tint** | `#B3BBE4` | 空夹前袋 |
| **Folder Tint Deep** | `#9AA3D2` | 空夹后板 |

> 文件夹 **禁止** 使用红/黄/绿：那是分流语义，夹子只是容器。

### 2.3 分流语义（Traffic · 业务锁死）

| Token | Hex | 业务 | UI 用法 |
|-------|-----|------|---------|
| **St Green** | `#1F7A4C` | 自动通过 ≥85 | stamp、行首条、统计 |
| **St Green Lt** | `#E2F0E8` | — | 行底 tint |
| **St Yellow** | `#DE8A0B` | 教师确认 60–85 | 同上 |
| **St Yellow Lt** | `#FBF0DC` | — | 同上 |
| **St Red** | `#D8332A` | 转人工 <60 | 同上；**错误态边框可借用，但文案要区分「业务红」与「系统错误」** |
| **St Red Lt** | `#FAE6E4` | — | 同上 |

别名（实现已有）：`--st-green/yellow/red` → 上表。

### 2.4 系统反馈色（与分流分离）

| 用途 | 建议 | 说明 |
|------|------|------|
| 表单校验错误 | `#B42318` 文 + `#FEF3F2` 底 | 可与 St Red 同族，但组件类名用 `is-invalid`，不叫 `stamp--red` |
| 成功 toast | St Green 体系 | 「已终审」等 |
| 警告 | St Yellow 体系 | 「识别置信偏低」 |
| Focus | Riso Blue `outline 2px` offset 2px | 已有，保持 |

### 2.5 禁止色

- `#000000` 大面积底或字
- 紫/粉霓虹渐变、蓝紫 mesh（AI slop）
- 第二套冷灰（slate/zinc 冷）与暖纸混用
- 把绿/黄/红当「装饰标签」用在非分流场景
- 随机第四强调色（橙、青、紫）

### 2.6 CSS 变量映射（保持现名，便于回写）

```text
--paper --paper-warm --paper-deep --paper-white
--ink --ink-mid --ink-soft --ink-faint
--rule --rule-soft
--riso-blue --riso-blue-lt
--riso-red --riso-red-lt --riso-amber --riso-amber-lt
--riso-green --riso-green-lt
--st-green --st-yellow --st-red
--folder-ink --folder-ink-deep --folder-ink-soft
--folder-tint --folder-tint-deep
```

---

## 3. Typography

### 3.1 角色分工（关键决策）

| Role | 栈 | 用在 | 不用在 |
|------|-----|------|--------|
| **Display** | 系统宋/衬线栈（现 `--font-display`） | Hero 大标题、章节 folio 大号 | 表格、按钮、输入、导航 |
| **Serif** | `--font-serif` | sheet 章节标题（可保留，字号受限） | 长表单 label |
| **Sans** | 系统 UI 无衬线 `--font-sans` | 全局 UI、正文、按钮、Tab | — |
| **Mono** | `--font-mono` | 置信度数字、阈值、kicker、ID、进度 | 大段中文正文 |

> **软件区默认 Sans。** 衬线是「封面与章节」，不是「整站换装」。这是对现 Riso 方向的收敛，不是推翻。

### 3.2 字号阶梯（fluid）

| Token | 值 | 用途 |
|-------|-----|------|
| `--fs-hero` | `clamp(2.6rem, 8.4vw, 6rem)` | **建议上限从 8.5rem 降到 ~6rem**，小屏更稳 |
| `--fs-display` | `clamp(2.1rem, 5vw, 3.5rem)` | 次级大标题 |
| `--fs-h1` | `clamp(1.5rem, 2.4vw, 2rem)` | 屏级标题 |
| `--fs-h2` | `clamp(1.125rem, 1.6vw, 1.375rem)` | sheet 标题 |
| `--fs-h3` | `0.9375rem` | 小组标题（可改用 mono kicker） |
| `--fs-body` | `0.9375rem`–`1rem` | 正文；**移动端 ≥ 16px 避免 iOS 聚焦缩放**（输入框） |
| `--fs-small` | `0.8125rem` | 辅助 |
| `--fs-micro` | `0.6875rem`–`0.75rem` | kicker、stamp；过小需加 tracking |

### 3.3 字距与行高

| 场景 | Tracking | Leading |
|------|----------|---------|
| Display 标题 | `-0.03em` ~ `-0.045em` | `0.95`–`1.1`（含 italic 降部时 ≥1.1） |
| UI 正文 | `0` | `1.55`–`1.65`（现 1.72 略松，工作台可收到 1.6） |
| Kicker / stamp | `0.12em`–`0.22em` | `1` |
| 数字（置信度、分数） | `-0.02em` + `tabular-nums` | `1` |

### 3.4 排版规则

- 段落宽度约 **65ch**（说明文）；表格与队列全宽
- 中文标题用 **sentence case / 原样**，不强制英文 Title Case
- 数字与单位：mono 或 `font-variant-numeric: tabular-nums`
- 禁：整站 Inter 外链、渐变填字、emoji 当图标

### 3.5 中文友好

- 优先系统：`PingFang SC` / `Microsoft YaHei` / `Noto Sans SC`
- 宋体仅 Display：`Songti SC` / `STSong` / `Noto Serif SC`
- **零外部字体请求**（答辩断网约束保留）

---

## 4. Layout, Spacing, Radius

### 4.1 网格

| Token | 值 |
|-------|-----|
| Columns | 12 |
| Max width | `1440px`（内容壳） |
| Gutter | `clamp(12px, 1.6vw, 24px)` |
| Page margin | `clamp(16px, 4.2vw, 72px)` |
| 工作台内容壳 | 可缩至 `1200–1280px` 提密度 |

**断点**

| 名 | 宽 | 行为 |
|----|-----|------|
| `sm` | ≤760 | 单列；Tab 横滑；split 上下 |
| `md` | ≤900 | Hero 单列；双栏收 |
| `lg` | ≥1100 | master–detail 可开 |
| `xl` | ≥1440 | 居中留边 |

### 4.2 Spacing scale（4 的倍数）

```text
--sp-1  4px
--sp-2  8px
--sp-3  12px
--sp-4  16px
--sp-5  24px
--sp-6  32px
--sp-7  48px
--sp-8  72px
--sp-9  112px   ← 仅 Hero/营销区段间距
```

**使用约定**

- 组件内 padding：`16–24`
- sheet 之间：`24–32`
- 区段（Hero）：`48–112`
- 表单 label → control：`8`
- control → 错误文案：`6–8`

### 4.3 Radius scale（新增，统一现状 0/6/8/10 混用）

| Token | 值 | 用在 |
|-------|-----|------|
| `--r-0` | `0` | 可选：严格印刷风控件（默认不再全局强制） |
| `--r-1` | `4px` | 输入框、小 tag、checkbox |
| `--r-2` | `8px` | 按钮、stamp、小卡片 |
| `--r-3` | `12px` | sheet、dialog、上传区 |
| `--r-4` | `16px` | 大卡片、文件夹视觉 |
| `--r-pill` | `999px` | 仅 filter chip / 小 pill；**主按钮不走 pill** |

> 个性：可保留「主按钮略方（8px）」对抗通用大圆角 SaaS；但 **同一层级半径必须一致**。

### 4.4 阴影

| Token | 值（指导） | 用在 |
|-------|------------|------|
| `--shadow-lift` | 微抬：`0 1px 0 paper-deep + 软扩散` | 卡片 hover |
| `--shadow-sheet` | 纸片：`2px 3px 0 paper-deep + 中扩散` | sheet |
| `--shadow-pop` | 浮层：更扩散、更淡 | dialog / popover |

禁：`shadow-md` 式黑雾；霓虹 glow。

### 4.5 Z-index 阶梯

```text
0     gridlines / 装饰
1     内容
20    sticky masthead
40    dropdown
50    dialog backdrop
60    dialog
70    toast
80    口令门
90+   禁止业务乱用
```

Grain 若启用，≤ 装饰层，**不得**盖过 toast/dialog。

---

## 5. Iconography

- 单一家族：现有内联 SVG（`icons.js`）继续；新图标保持 **同 stroke 宽**
- 禁 emoji 当状态图标
- 语义：绿黄红用 **色 + 短文案**，不单靠色点（无障碍）
- 尺寸：`14 / 16 / 18 / 22`；触控旁图标热区仍 ≥44

---

## 6. Components

### 6.1 Button

| 变体 | 外观 | 何时用 |
|------|------|--------|
| **Primary** | Ink 底 + Paper 字；`radius r-2` | 每屏 **最多 1 个**主行动 |
| **Accent** | Riso Blue 底（`btn--blue`） | 与批改/扫描强相关的主行动 |
| **Ghost** | 透明底 + Ink 边 | 次要：重置、重命名、取消 |
| **Danger** | 透明或浅红底 + St Red 边/字 | 删除文件夹等不可逆 |

**交互**

```text
hover  : 背景微移或 1px 纸片抬起（可保留现 translate 个性）
active : scale(0.97) 或 translate(1px,1px) —— 必须有按压
disabled: opacity 0.45 + pointer-events none
focus  : 2px Riso Blue ring, offset 2px
```

**尺寸**

| 名 | padding | 最小高度 | 字号 |
|----|---------|----------|------|
| sm | 6×12 | **44px 触控热区**（可视可小于，热区用伪元素扩） | micro |
| md | 10×18 | 40–44 | small |
| lg | 14×28 | 48 | body |

禁：一屏两个同等重量实心按钮；外发光；渐变按钮。

### 6.2 Stamp / Tag / Factor

| 组件 | 形状 | 语义 |
|------|------|------|
| **Stamp** | 小色块，`r-2`，无边或细边 | 仅红黄绿分流 + quiet 模式章 |
| **Tag** | 浅底 + 细边 | 知识点、学科、来源 |
| **Tag KP** | 中性浅底 | 考点 |
| **Factor** | 浅底 + mono 数值 | 转写置信、评分置信、历史通过率 |

规则：Stamp 文案固定业务词（自动通过 / 教师确认 / 转人工），不写「成功/失败」替代。

### 6.3 Sheet / Card

- **Sheet**：工作台主表面；`Paper White`；顶部分割线或 `shadow-sheet`；内边距 24；`r-3`
- **Card**：仅当需要抬升层级时使用；高密度列表优先 **行 + 顶线**，少套卡片
- 禁：卡片套卡片套卡片

### 6.4 Form controls

```text
[Label 上方]
[Input / Select / Textarea]
[Hint 可选 · Ink Soft]
[Error 可选 · 校验红 · 在控件下方]
```

- Input：`r-1`；边框 Rule；focus 时 Riso Blue
- Textarea：等宽可混；OCR 结果区 min-height 稳定，避免布局跳
- 禁：placeholder 当 label；`alert()` 报错

### 6.5 Navigation

- **Masthead**：sticky；底边 1–3px Ink 或 Rule；品牌可点回 Hero
- **Tabs**：四步；`is-on` 下划线 scaleX；移动端横滑，不折行挤爆
- **Trail**：当前步说明 + 下一步暗示；完成态可勾选

### 6.6 Folder card

- 容器色 = Folder/Riso Blue 系；空夹用 tint
- 选中：`is-on` 名称变蓝 + 微抬；**不要**整卡变绿/红
- 数量用 mono tabular

### 6.7 Dropzone

- 虚线 Rule 边 + Paper Warm 底；`r-3`
- 拖入：边变 Riso Blue + 底微亮
- 内含：图标 + 一行主文 + 一行 hint + Ghost 选文件按钮

### 6.8 Dialog / Review panel

**目标态（教师终审）**

```text
Desktop: 左队列 36% | 右审卷 64%（同屏，不全靠中心 modal）
Mobile : 全屏 sheet；顶栏关闭；底栏主 CTA
```

若暂保留 modal：

- 宽度 `min(720px, 92vw)`；宽审卷 `min(960px, 96vw)`
- 进场：opacity + translateY(8px)，200ms ease-out；**从 scale(0.96) 起，禁 scale(0)**
- Esc / 遮罩点击关闭策略与未保存确认要定义（实现时）

### 6.9 Data display

- 分数、置信度：mono + tabular
- 进度条：确定进度优先；高度 6–8px；`r-pill` 轨道；填充 Riso Blue
- 表格/队列行：行高 44–52；左 3px 色条表达分流

---

## 7. States — Loading / Empty / Error / Success

> 每个可异步区块必须设计四态。禁止「转圈或空白」。

### 7.1 Loading

| 场景 | 模式 | 规格 |
|------|------|------|
| 文件夹网格 | **Skeleton** 3–6 个夹形块 | 与真实 card 同尺寸；shimmer 轻扫 Paper→Paper White |
| 审核列表 | Skeleton 行 | 左色条位 + 三行灰条 |
| 看板 KPI | Skeleton 数值块 | 保留 label 位 |
| 单次动作（终审提交） | 按钮内 spinner 或 busy 态 | 按钮 disabled +「提交中」 |
| 批量流水线 | **确定进度条** + 列表行状态 | 已有 batch-bar；单项 success/fail 色点 |

禁：整页居中一个无限 spinner 作为唯一反馈（可作极短 fallback ≤300ms）。

**Shimmer**

```text
背景: Paper Warm
高光: linear-gradient 透明 → Paper White/70 → 透明
动画: transform translateX，1.2s linear infinite
仅 transform/opacity；respect reduced-motion → 静止灰块
```

### 7.2 Empty

结构（composed empty）：

```text
[图标 32–40 · Ink Soft]
[标题 一行 · Ink]
[说明 1–2 行 · Ink Soft · ≤65ch]
[Primary 或 Ghost CTA]
```

| 场景 | 标题示例 | CTA |
|------|----------|-----|
| 结果页无批改 | 还没有批改结果 | 去拍照提交 |
| 教师台无队列 | 暂无待审作答 | 先跑一遍样例 / 整夹批改 |
| 空文件夹 | 夹内还没有照片 | 上传作业照片 |
| 看板无数据 | 还没有学情可汇总 | 完成批改后回来 |

禁：只写「暂无数据」；禁幽默「Oops」。

### 7.3 Error

| 层级 | 表现 | 示例 |
|------|------|------|
| **字段** | 边框校验红 + 下方一句 | 「请填写文件夹名称」 |
| **区块** | Note 条（浅红底）+ 重试按钮 | 「识别失败：网络超时」 |
| **全局** | Toast 或顶栏 banner | 「终审未保存，请重试」 |
| **口令门** | 输入框下错误，不清空口令策略自定 | 「口令不正确」 |

文案公式：`{发生了什么}。{下一步}`  
例：「连接失败。请检查网络后重试。」

禁：`window.alert`；禁责怪用户；业务红（转人工）≠ 系统错误红（文案与类名分离）。

### 7.4 Success / Partial

- 终审成功：行内 stamp 更新 + 可选 toast「已写入终审」
- 批量部分失败：进度区总结「12 成功 · 2 失败」+ 失败行可点重试
- 不使用感叹号堆砌

### 7.5 状态覆盖矩阵（验收）

| 页面 | Loading | Empty | Error | Partial |
|------|:---:|:---:|:---:|:---:|
| 口令门 | — | — | ✓ | — |
| 01 文件夹 | skeleton | ✓ 空夹+CTA | ✓ | — |
| 01 上传识别 | progress | — | ✓ | ✓ |
| 02 结果 | skeleton | ✓ | ✓ | — |
| 03 教师台 | skeleton | ✓ | ✓ | 单条失败 |
| 04 看板 | skeleton | ✓ | ✓ | — |

---

## 8. Motion & Interaction

依据 **emil-design-eng**；dial Motion = 3。

### 8.1 决策树

1. 日用 > 数十次？（Tab 切换、列表筛选）→ **极短或无动画**
2. 有目的？（反馈 / 防跳变 / 空间连续）→ 才做
3. 进入 → **ease-out**；移动 → ease-in-out；禁 UI 用 ease-in
4. 时长：按压 100–160ms；tooltip 125–200；面板 200–300；**UI ≤ 300ms**

### 8.2 Token

```text
--ease-out-ui:  cubic-bezier(0.23, 1, 0.32, 1)     /* 可替换/并存 --ease-paper */
--ease-move:    cubic-bezier(0.77, 0, 0.175, 1)
--ease-paper:   cubic-bezier(0.16, 0.84, 0.28, 1)  /* 现有 · 纸感 */
--ease-ink:     cubic-bezier(0.62, 0.02, 0.2, 1)   /* 现有 */
--dur-1: 160ms
--dur-2: 240ms   /* 建议从 320 略收 */
--dur-3: 400ms   /* 面板；原 520 可收 */
--dur-4: 仅 Hero 叙事
```

### 8.3 允许的动效

| 场景 | 做法 |
|------|------|
| 按钮 active | scale(0.97) |
| Sheet 换页 | opacity + 8px Y，ease-out |
| Toast | 同边进同边出；transition 可打断 |
| 列表首次出现 | 可选 stagger 40ms，≤6 项 |
| 进度条宽度 | transition width 240ms（可接受；或用 transform scaleX） |

### 8.4 禁止

- `transition: all`
- `scale(0)` 进场
- 键盘操作带动画
- 无限装饰 loop（漂浮、脉冲光）在工作台
- 滚动劫持 / 重型 parallax（Hero 轻微即可）

### 8.5 Reduced motion

```text
prefers-reduced-motion: reduce
→ duration ≈ 1ms；保留 opacity 必要时；去掉位移与旋转
```

（现 tokens 已有，改造时保持。）

---

## 9. Page Structure & Placement

### 9.1 Hero（答辩入口）

```text
┌─ top bar: 品牌 | meta（模式·年份）─────────────────────┐
│  kicker                                              │
│  Display 标题（最多两行）        │  可选拼贴/实物     │
│  Claim 段落（问题意识 + 技术主张）                    │
│  绿 | 黄 | 红  业务计数（非假百分比）                  │
│  [Primary CTA 开始体验]                               │
└──────────────────────────────────────────────────────┘
```

- 单 CTA；禁「Scroll to explore」
- 计数必须来自真实 API/样例，禁 99.9%
- 桌面可非对称；移动单列

### 9.2 App shell

```text
masthead (brand · mode stamp · reset)
tabs (01–04)
trail + tip
screen
  └ sheets…
```

### 9.3 01 提交

顺序：**文件夹工具条 → 夹网格 → 夹操作 → 上传 →（识别 | 批量）**  
选中夹后操作条出现；上传目标 hint 始终可见。

### 9.4 02 结果

- 有数据：总分 + 双置信 + 分流 stamp + 逐步证据 + 错因 + 评语
- 无数据：composed empty
- 批量：顶栏 batch-picker（单行 select，已有模式保留）

### 9.5 03 教师台（目标 IA）

```text
┌ Filter chips: 全部|红|黄|绿 | 未审  排序 ▾ ──────────┐
├ 队列列表（色条·学生·题·置信·状态）─┬ 审卷详情 ──────┤
│ 可键盘 j/k 规划（后期）            │ 原卷/OCR       │
│                                    │ 步骤证据可改判  │
│                                    │ 评语·错因·提交  │
└────────────────────────────────────┴────────────────┘
```

移动：列表全宽 → 点入全屏详情。

### 9.6 04 看板

- 顶：班级选择 + 刷新时间
- KPI 条（非三等宽装饰卡强制）：薄弱点 / 高风险 / 完成率等
- 下：热力或列表 + 教学建议
- 空：composed empty

### 9.7 口令门

- 居中卡片；品牌 + 一句话 + 输入 + 进入
- 错误行内；无多余营销

---

## 10. Content & Voice

- 主动、具体、教师视角：「提交批改」「写入终审」「转人工批改」
- 同一动作全流程同名（按钮 = toast）
- 禁：Elevate / Seamless / Unleash / 赋能未来 等空话（答辩 claim 可保留已论证表述）
- 禁：感叹号成功文案；「Oops」
- 数字：真实或样例真实分布，不造假圆满

---

## 11. Accessibility & Quality Bar

- 对比度：正文 Ink on Paper ≥ 4.5:1；stamp 白字需验绿/黄/红底
- 焦点可见；跳过链接（可选增强）
- 触控 ≥ 44px
- 色不单独承载：分流同时有文字
- `color-scheme: light`（当前产品不做 dark；若做须另开 token 表）
- 断网：零外链字体/图床（约束保留）

---

## 12. Anti-Patterns（Banned）

**视觉**

- [ ] AI 紫粉渐变、霓虹 glow
- [ ] 纯黑 `#000` 大面积
- [ ] 冷暖灰混用
- [ ] 三等宽特性卡当默认
- [ ] 工作台全页 grain
- [ ] 渐变大标题字
- [ ] 主按钮 pill + 重阴影

**组件**

- [ ] 只有 spinner 无 skeleton
- [ ] Empty 单行无 CTA
- [ ] `alert` / 无重试错误
- [ ] 红黄绿乱用在文件夹/装饰
- [ ] 卡片无限嵌套
- [ ] Lucide/emoji 混搭新家族（保持单一图标源）

**动效**

- [ ] `transition: all`；`scale(0)`；ease-in 进场
- [ ] 键盘操作动画；>300ms 日用动画

**文案**

- [ ] 假 99.9%；Lorem；John Doe；Acme

---

## 13. Implementation Map（改码时用 · 本轮不执行）

| 设计层 | 文件 |
|--------|------|
| Token | `demo/static/css/tokens.css` |
| 组件与布局 | `demo/static/css/app.css` |
| 结构 | `demo/static/index.html` |
| 状态渲染 | `demo/static/js/app.js` |
| 图标 | `demo/static/js/icons.js` |
| 动效 | `demo/static/js/motion.js` |

**回写顺序建议**

1. tokens：radius 阶梯、dur 收敛、grain 作用域
2. 四态组件：`.skeleton` `.empty` `.note--error` `.toast`
3. 教师台 master–detail
4. 移动断点与热区
5. Hero 字号上限与工作台去 grain

---

## 14. Reference Links（调研）

| 类型 | 来源 |
|------|------|
| 赛道与竞品 | `docs/03-行业研究与竞品分析.md` |
| 教师台 IA | `docs/09-教师工作台P0改造说明.md` |
| 本轮调研 | `docs/11-UI设计调研与风格定位.md` |
| Skills | `.reasonix/skills/{design-taste-frontend,emil-design-eng,minimalist-ui,stitch-design-taste,redesign-existing-projects,...}` |
| 体验参考 | Linear · Notion · Gradescope · 希沃教育可信语汇 |
| 模式库 | Mobbin triage/split-view · Landingfolio 单 CTA Hero · Pinterest editorial/riso |

---

## 15. Changelog

| 日期 | 变更 |
|------|------|
| 2026-08-07 | 初版：调研沉淀；**不改业务代码**；保留 Riso 暖纸与分流色；收敛软件区衬线；补齐四态与 radius |

---

*所有新页面与改版必须先读本文，再改 `tokens.css` / `app.css`。页面级例外可后续加 `design-system/pages/*.md`，无例外则全文适用。*
