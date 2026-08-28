# Skill Matcher · 技能与专家匹配器

> **装一个就够。** 帮你从成堆的技能（Skills）和专家（Experts）里，挑出当下最该用的那一个。
>
> 一个项目，**两种形态**：`SKILL.md` 版（跨平台）+ `dsh 插件版`（DeepSeek Harness）。功能完全一致，只是外壳不同——你装哪个，取决于你用什么 AI 环境。

---

## 一、这是什么？为什么需要它？

现在的 AI 助手生态里，技能和专家越来越多：WorkBuddy 有技能市场，Claude Code 有 Skills，还有各种开源技能、专家包……**多到用户不知道装哪个、用哪个**。

Skill Matcher 是一个「**元技能 / 路由技能**」：它不干活，但最懂"哪个工具该干哪个活"。它读懂你的需求，从你本机已装的技能、市场技能、专家目录、开源社区里，自动匹配并推荐最该用的那一个，输出 **Top3 推荐 + 匹配理由 + 一键安装/启用方式**。

**它凭什么选得准？** 因为它按四层递进理解你的需求：

| 层 | 做什么 | 例子 |
|---|---|---|
| L0 字面 | 关键词命中 | 你说「PDF」，直接命中 PDF 技能 |
| L1 语义 | 这句话在说什么 | 「把这份合同转成能签字的格式」→ 文档处理 |
| L2 意图 | 他要什么结果 | 信息 / 分析 / 决策 / 执行 / 生成 |
| L3 潜在需求 | **没说出口的真实诉求** | 「今天黄金怎么这么弱」→ L2 是查行情，L3 是「手里的单要不要拿」——该推风控/决策，不是纯行情 |

L3 是拉开差距的地方：匹配器会识别「怎么办 / 该不该 / 好烦 / 纠结」这类信号，知道你要的不是信息，而是**决策支持或安心**。

**中立原则**：对所有技能与专家一视同仁，推荐排序只由需求匹配度决定，不偏向任何单一产品。若某条目是推广位，会明确标注「推广」。

---

## 二、两种版本，怎么选？

| | **SKILL.md 版** | **dsh 插件版** |
|---|---|---|
| 适用环境 | WorkBuddy / Claude Code / CodeBuddy / 任何支持 SKILL.md 的助手 | DeepSeek Harness（dsh web / dsh tui） |
| 形态 | 一个技能目录（SKILL.md + 索引脚本） | 一个 Cordis 插件（npm 包） |
| 提供的能力 | 匹配逻辑（由 LLM 读 SKILL.md 后执行）+ `sync_index.py` 索引脚本 | `skill_matcher_match / scan / list` 三个工具 + 引导段 |
| 核心引擎 | `bin/sync_index.py`（Python 标准库） | `plugin/lib/engine.js`（同一套逻辑的 JS 移植） |
| 索引 | `index/skills.json` + `experts.json` | `~/.dsh/dsh-skill-matcher/cache.json` |
| 安装 | `bash install.sh`（自动装到检测到的环境） | `bash install.sh`（自动写入 dsh profile） |

**两者共用**：同一份开源目录（`skill-matcher-index` 仓库）、同一套匹配逻辑、同一套贡献飞轮。装哪个、甚至两个都装，结果一致。

---

## 三、一分钟上手（一键安装）

### 方式 A：本地项目目录内

```bash
bash install.sh
```

### 方式 B：远程一键（无需先下载）

```bash
curl -fsSL https://raw.githubusercontent.com/axel286137079-dot/skill-matcher/main/install.sh | bash
```

### 常用参数

```bash
bash install.sh --skill-only   # 只装 SKILL.md 版
bash install.sh --dsh-only     # 只装 dsh 插件版
bash install.sh --dry-run      # 先看要做什么，不实际改动
```

装完即可用。首次使用会自动生成索引；也可以手动刷新：

```bash
python3 bin/sync_index.py           # 完整同步（含远程开源目录）
python3 bin/sync_index.py --offline # 仅本地
```

> dsh 插件版装完需重启 dsh web 生效：`launchctl kickstart -k gui/$(id -u)/com.deepseek.harness.web`

---

## 四、怎么用（示例）

对 AI 助手说：

- 「我想把这份 PDF 里的表格提出来」→ 推荐 `pdf` 技能
- 「帮我做一份季度汇报 PPT」→ 推荐 `pptx` 技能
- 「最近好内耗，不知道该怎么办」→ 匹配到心理/成长类专家
- 「这个技术方案要不要上云？」→ 识别到决策诉求，推荐架构/顾问类能力
- 直接问「有没有什么技能能……？该用哪个专家？」→ 匹配器启动

输出格式：每个推荐包含 **名称 + 一句话说明 + 匹配理由（哪一层）+ 状态（已装/未装）+ 安装或启用方式**。中英双语，你说中文回中文，说英文回英文。

---

## 五、一键贡献（让目录越用越全）

**每个安装者都可以是目录维护者。** 装的人越多 → 目录越全 → 匹配越准 → 更多人装。

```bash
python3 bin/sync_index.py --submit-contribution
```

流程（**opt-in，默认绝不上传任何内容**）：

1. 扫描本机技能，过滤敏感/重复，按质量分排序，列出候选（已进中央目录的自动跳过）
2. **你逐条挑选**要贡献哪些（输入编号，或 `all`）
3. 生成本地贡献文件 `index/contributions/<你>.json`
4. 若检测到已登录的 `gh`，自动推送到中央仓库；否则给出提交指引

三条红线（必须遵守）：
1. **隐私红线**：默认绝不自动上传，用户逐条确认后才构成贡献
2. **质量红线**：垃圾进目录 = 匹配器信誉崩。同一技能被 ≥3 个不同安装者提交才自动采纳；单一用户自制进候选区待审核
3. **权限红线**：提交走贡献文件/PR，审核权永远在维护者手里

维护者合并：`python3 bin/sync_index.py --merge-contributions`

---

## 六、目录结构

```
skill-matcher/
├── SKILL.md              # SKILL.md 版核心（路由逻辑，"大脑"）
├── install.sh            # 一键安装器（两种版本）
├── README.md             # 本文件
├── CONTRIBUTING.md       # 贡献指南（三条红线）
├── _meta.json            # 市场发布元数据
├── bin/
│   └── sync_index.py     # 索引引擎（纯 Python 标准库）
├── index/
│   ├── _sources.json          # 远程开源目录源配置
│   ├── _manual_skills.json    # 手动精选技能（随包种子）
│   ├── _manual_experts.json   # 手动精选专家
│   ├── opensource-index.json  # 全局开源目录（数据资产，发布到 GitHub）
│   ├── skills.json / experts.json  # 本机生成的索引（不入库）
│   └── contributions/         # 贡献文件（opt-in）
└── plugin/               # dsh 插件版（Cordis 插件）
    ├── package.json
    ├── cordis.patch.yml
    └── lib/{index,tools,engine}.js
```

---

## 七、项目进展

- ✅ 2026-08-28 已在 **SkillHub 社区提交上架**（skillId 176266，v1.0.0，三线安全审核中）
- ✅ 开源目录仓库：`github.com/axel286137079-dot/skill-matcher-index`（全网同步）
- ✅ 已在 WorkBuddy / Claude Code / CodeBuddy / DeepSeek Harness 四环境实测可用
- ✅ 已通过安全自审（P2，无投毒风险；纯本地扫描、零第三方依赖、仅拉取 JSON 数据）

---

## 八、FAQ

**Q：它会上传我的隐私数据吗？**
A：不会。索引只读取本机技能/专家的「名称+简介」元数据用于匹配；贡献功能是 opt-in，你逐条确认才生成文件，默认绝不自动上传。

**Q：远程同步安全吗？**
A：安全。只拉取 JSON 目录数据（纯数据，不执行任何代码），失败静默降级，离线可用。

**Q：装了两个版本会冲突吗？**
A：不会。它们各自独立工作，结果一致（共用同一份开源目录）。

**Q：索引会过期吗？**
A：会自动保鲜——超过 24 小时未更新，匹配前自动重跑同步。

**Q：我想卸载？**
A：删除对应技能目录即可；dsh 版删除 profile 里的 `dsh-skill-matcher` 依赖行和 `node_modules/dsh-skill-matcher` 目录。

---

## 九、联系与许可

- 作者：苏格（个人开发者 · 心理辅导工作者 · 量化交易系统开发者）
- 联系方式：手机 15581813366 / 微信 SG592247888 / 邮箱 43298568@qq.com
- License：MIT

---

*想提建议、报 bug、贡献技能？欢迎通过上面的联系方式找到我们，或直接提交 PR。*
