# 贡献指南 · Contributing

感谢你参与 skill-matcher 开源技能目录的共建！装的人越多 → 目录越全 → 匹配越准 → 更多人装。

## 三条红线

1. **隐私**：只提交你确定可以公开共享的技能。公司私有、含密钥/配置、未公开内容一律不要提交。
2. **质量**：描述写清楚（中英皆可，越具体匹配越准），SKILL.md frontmatter 完整（name + description）。
3. **诚实**：不伪造、不刷量。同一技能被 ≥3 个不同用户独立提交才自动采纳。

## 如何提交

### 方式 A：贡献文件（推荐）

1. 本地运行 `python3 bin/sync_index.py --collect-contributions` 生成候选清单。
2. 审核 `index/contributions/candidates.json`，把你确定要贡献的条目整理成 JSON 数组，放到本仓库 `contributions/<你的GitHub用户名>.json`，格式：

```json
[
  {
    "id": "my-skill",
    "name": "my-skill",
    "description": "这个技能做什么（中英皆可，越具体匹配越准）",
    "install": "git clone https://...",
    "origin": "作者/仓库名"
  }
]
```

3. 开 Pull Request，或直接把文件内容发 Issue。

### 方式 B：Issue 提交

在 Issues 里按模板贴：技能名 / 描述 / 安装方式。

## 审核流程

- 每个贡献会经过：机器预审（敏感词/质量分/查重）→ AI 审核（安全+质量）→ 人工兜底抽查。
- 状态：`candidate`（候选）→ `approved`（通过）→ `pending`（待查，会在 PR/Issue 里说明原因）。
- 同一技能被 ≥3 个不同贡献者提交 → 共识达成，自动采纳。

## 目录结构

```
index.json                  # 全局开源技能目录（联网同步的目标文件）
contributions/<user>.json   # 各贡献者的提交（待审核）
```

## 本地测试

```bash
# 插件（Node）：远程目录版本门控
cd plugin && node --test test/*.test.mjs      # 等价于 npm test

# 同步脚本（Python）：裁决逻辑 / tags / origin / 导出修订号
python3 -m unittest discover -s tests -v
```

## 发布目录时的修订号规则（维护者必读）

`index.json` 顶层的 `version` 是**整数修订号**，客户端只在它**严格变大**时才接受目录更新：

- 内容有变化 → 必须把 `version` 递增后再发布，否则**所有已同步的客户端都会拒绝这次更新**（并继续使用旧目录）。
- 内容没变化 → 不要动 `version`。
- 运行 `python3 bin/sync_index.py` 时会自动处理导出的 `index/opensource-index.json`：内容变才自增，且不会低于本次拉取到的已发布版本。

> 历史遗留：早期客户端存的是「纯字符串哈希、无修订号」。这类客户端需要**一次性**删除本地的哈希记录（`index/_remote_hashes.json`，插件版是 `~/.dsh/dsh-skill-matcher/cache.json`）才能收到本次及以后的更新，之后由修订号接管。

## 维护者

@axel286137079-dot 及其 AI 审核助手。
