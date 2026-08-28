# dsh-skill-matcher — dsh 插件版

**技能与专家匹配器** 的 DeepSeek Harness 插件形态（Cordis 插件）。本目录是 `skill-matcher` 项目的 `plugin/` 子目录；功能与 SKILL.md 版完全一致，只是换了一层插件外壳。

## 它提供什么

- `skill_matcher_scan`：扫描本机技能/专家（多环境）+ 联网开源目录，构建并缓存索引
- `skill_matcher_match(query, topN?, includeExperts?)`：读懂需求，返回 Top-N 推荐（含匹配度、命中关键词、安装方式）
- `skill_matcher_list(scope?)`：浏览本机已装/市场/开源目录
- 同时在 system prompt 注入引导段：当用户问「该用哪个技能/专家」时优先调用 `skill_matcher_match`

核心引擎 `lib/engine.js` 是 `bin/sync_index.py` 的 JS 移植：环境探测 → SKILL.md frontmatter/plugin.json 解析 → 远程开源拉取 → 优先级合并 → 关键词召回。内置 13 条开源种子，离线可用；缓存写入 `~/.dsh/dsh-skill-matcher/cache.json`（24h 复用）。

## 安装

**推荐方式**：在项目根目录运行一键安装器（自动检测并安装到 dsh profile）：

```bash
bash ../install.sh --dsh-only
```

**或手动**（dsh 支持 `file:` 插件）：

```bash
dsh plugin --profile web add file:<本项目绝对路径>/plugin
```

装完重启 dsh web 生效：`launchctl kickstart -k gui/$(id -u)/com.deepseek.harness.web`

> 若 `dsh plugin add` 因 profile 的 pnpm 供应链策略（minimumReleaseAge 名单过期）失败，用根目录 `install.sh`（会直接写入 profile 的 package.json + node_modules，并自动备份）。

## 目录结构

```
plugin/
├── package.json        # 含 dsh.bundle.patch 清单
├── cordis.patch.yml    # bundle patch：插件行插入 web profile
└── lib/
    ├── index.js        # Cordis 入口：注册工具 + 注入引导
    ├── tools.js        # 三个 defineTool 定义
    └── engine.js       # 核心索引引擎（纯 JS 无依赖）
```

## 合规

- 纯本地扫描 + 仅拉取 JSON 目录数据，不执行任何远程代码
- 离线可用（内置开源种子）
- 推荐排序中立，只由需求匹配度决定

## License

MIT
