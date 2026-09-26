# 交接说明：远程目录改为「修订号门控」（remote catalog v2）

> **这份文档给谁看**：负责维护**本机已安装的 skill-matcher 副本**的 AI 助手。
> **背景提交**：`fix/version-gated-remote-catalog`（PR #1，基线 `32c7a572`）。
> **读完你应当能**：把本地副本同步到位、清掉会导致「永久拒绝更新」的旧状态、并在后续改代码时不破坏新的契约。

---

## 一、为什么改

两个客户端都把整个 `index.json` 的 SHA256 **钉死在可变 URL** 上：

```
https://raw.githubusercontent.com/axel286137079-dot/skill-matcher-index/main/index.json
```

旧行为：内容一变 → 拒绝。更糟的是拒绝之后**并没有「保持旧版」**：

| 客户端 | 旧代码 | 后果 |
|---|---|---|
| `plugin/lib/engine.js` | `fetchRemoteSkills()` 哈希不一致 → `return []` | `getIndex()` 落到 `remote.length ? remote : SEED_OPENSOURCE` → **退回内置种子**，并写回缓存 |
| `bin/sync_index.py` | `fetch_remote_skills()` 哈希不一致 → `continue` | 该源条目消失 → 退回 `_manual_skills.json` |

结论：**已同步过的客户端会永久拿不到目录更新**。

顺带修掉的两个丢数据问题（Python 侧）：拷贝条目时 **`tags` 被丢掉**、**`origin` 被写成远程源的名字**而不是条目自己的。

---

## 二、新规则

SHA256 仍然校验，但**接受条件换成「能被证明是正常递增的更新」**：

| 情形 | 结果 |
|---|---|
| 无记录（首次拉取） | ✅ 接受，记录 SHA256 + 顶层整数 `version` |
| 内容未变（哈希相同） | ✅ 接受（幂等；旧记录借此补上 `version`） |
| 内容变了，`version` 是正整数且**严格大于**上次接受值 | ✅ 接受，写入新哈希 / 新 version / 新条目快照 |
| `version` 缺失 / 非正整数 / 没变大，或内容变了但 `version` 没变 | ❌ 拒绝 |

**拒绝时必须继续使用上次接受的那份目录** —— 不退回种子，也不把空数组当成成功。
防篡改仍然在：**不是**「哈希不同就一律接受」。

---

## 三、你要做的三件事

### 1️⃣ 合并 PR 后，重新同步已安装副本

本机 `~/.workbuddy/skills/skill-matcher/` 的**现状**（2026-09-26 核查）：

- HEAD 停在旧提交 `b795ef5`（v1.0.0），**与仓库 `origin/main` 严重脱节**
- 还带着 **8 个未提交的游离改动**（`.gitignore` / `README.md` / `SKILL.md` / `bin/sync_index.py` / `index/opensource-index.json` / `plugin/lib/*.js`）

⚠️ **因此不能直接 `git pull`**，会冲突。先备份再重置：

```bash
cd ~/.workbuddy/skills/skill-matcher

# 1) 先看清有什么游离改动，确认是否需要保留
git status --short
git diff > /tmp/skill-matcher-local-changes.patch    # 备份，丢弃前务必做

# 2) 同步到远端 main
git fetch origin
git checkout main
git reset --hard origin/main

# 3) 确认关键改动已到位（有输出 = 已是新版）
grep -c "decideRemoteUpdate" plugin/lib/engine.js        # 期望 >0
grep -c "decide_remote_update" bin/sync_index.py         # 期望 >0
ls plugin/test/engine.test.mjs tests/test_sync_index.py  # 期望两个文件都存在
```

> 如果 `/tmp/skill-matcher-local-changes.patch` 里有值得保留的内容，**先挑出来再重置**，不要无脑丢。

### 2️⃣ 跑测试确认新逻辑可用

```bash
cd ~/.workbuddy/skills/skill-matcher
node --test plugin/test/*.test.mjs        # 期望：11 条全通过
python3 -m unittest discover -s tests     # 期望：13 条全通过
```

> `docstring` 里写了覆盖范围；两个套件都**不依赖网络**（JS 用 stub、Python 用 `opener` 注入）。

### 3️⃣ 清理旧哈希记录（**一次性，不做就永远收不到更新**）

```bash
# dsh 插件版
rm -f ~/.dsh/dsh-skill-matcher/cache.json

# 脚本版
rm -f ~/.workbuddy/skills/skill-matcher/index/_remote_hashes.json
```

**为什么**：旧记录是「纯字符串哈希、**没有修订号**」。服务端内容只要变过，新逻辑就会判 `rejected:no-baseline-version` 而拒绝 —— 这是**故意**保守的，因为无法证明「内容变了 + 没有修订号」是正常发布而不是篡改。

删完后跑一次同步，看到下面这行就说明已恢复：

```bash
cd ~/.workbuddy/skills/skill-matcher && python3 bin/sync_index.py
# 期望输出形如：[remote] skill-matcher 官方开源目录: +15 条（first-fetch，version 3）
```

之后**由修订号接管**，不需要再手动清理。

---

## 四、新契约（改代码前必读）

### 返回签名变了 —— 调用方必须跟着改

| 语言 | 函数 | 旧签名 | 新签名 |
|---|---|---|---|
| JS | `fetchRemoteSkills(offline)` | `string[]` | `{ entries, accepted, reason, version, hash }` |
| Python | `fetch_remote_skills(...)` | `list` | `(items, status)` |

`entries` / `items` 是**应当使用的条目**：接受则是新条目，**拒绝则是上次接受的目录**。

`reason` 取值：`first-fetch` / `unchanged` / `version-bumped` / `rejected:version-missing` / `rejected:no-baseline-version` / `rejected:version-not-increased` / `offline` / `http-*` / `parse-error` / `network-error`。

### 不许破坏的不变量

1. **`tags` 必须是字符串数组**（`string[]`），**不是** `{zh: ...}` 对象。打分函数（JS `scoreEntry`、约 629–640 行）按字符串标签匹配。
2. **顶层 `version` 必须是正整数**，发布时**严格递增**。写死版本号 = 所有已同步客户端永久拒绝更新。
3. **`aliases` 字段引擎不读**，先不要接。
4. 目录里那 **4 个没有同名路径的技能不要删**。
5. **拒绝时必须用上次接受的目录** —— 不许退回 SEED，不许把 `[]` 当成成功。
6. **旧格式必须能读不崩**：`remoteHashes[url] = "<sha256 字符串>"` 这种老记录要能解析。

### 状态存储

**JS 插件版**（`~/.dsh/dsh-skill-matcher/cache.json`）：

```jsonc
{
  "skills": [...], "experts": [...],
  "remoteCatalog": {
    "<url>": { "hash": "<sha256>", "version": 4, "acceptedAt": 1758..., "entries": [ ... ] }
  },
  "remoteHashes": { "<url>": "<sha256>" }   // 旧键，仍维护，便于回退旧版本客户端
}
```

**Python 脚本版**（`index/_remote_hashes.json`，已在 `.gitignore`）：

```jsonc
{ "<url>": { "hash": "<sha256>", "version": 4, "accepted_at": "2026-09-26 09:30:00", "entries": [ ... ] } }
```

> 两种格式都**兼容**旧值（纯字符串）。读的时候用 `_normalize_record()` / `normalizeRecord()`，不要自己 `raw["hash"]` 硬取。

### 新增的环境变量

| 变量 | 作用 |
|---|---|
| `SKILL_MATCHER_CACHE_DIR` | 覆盖 JS 插件版缓存目录（**测试隔离用**，与既有的 `SKILL_MATCHER_SKILLS_DIR` 同一约定） |

> 注意：`CACHE_DIR` 在 **模块加载时**解析，测试必须先设环境变量再 `await import()`。

---

## 五、常见问题

**Q：客户端还是收不到更新？**
先看服务端 `index.json` 的 `version` 有没有**严格变大**。没变 → 客户端**按设计**拒绝。发布侧 `export_open_source()` 已改为「内容变才自增，且不低于本次拉取到的已发布版本」。

**Q：怎么确认本机跑的是新逻辑？**
`grep -c decideRemoteUpdate plugin/lib/engine.js`，>0 即新版。或在同步时看输出里有没有 `（first-fetch，version N）` 这种带原因的文案（旧版没有）。

**Q：能不能自动迁移旧哈希，省掉人工删除？**
**不能，也故意不做。** 自动接受「内容变了 + 无修订号」等于放弃防篡改。只能人工删一次。

**Q：网络异常时会怎样？**
返回**上次接受的目录**（不是 `[]`），`reason` 为 `http-*` / `network-error` / `timeout`。只有「确实没有任何可用目录」时 `getIndex()` 才会退回 `SEED_OPENSOURCE`。

**Q：离线模式（`--offline`）呢？**
仍然沿用上次接受的开源目录，不再直接返回空。

---

## 六、参考

| 内容 | 位置 |
|---|---|
| 裁决逻辑（JS） | `plugin/lib/engine.js` → `decideRemoteUpdate` / `readCatalogVersion` / `readRemoteRecord` / `normalizeRemoteEntries` |
| 裁决逻辑（Python） | `bin/sync_index.py` → `decide_remote_update` / `_read_catalog_version` / `_normalize_record` / `normalize_remote_entries` |
| 测试 | `plugin/test/engine.test.mjs`（11 条）、`tests/test_sync_index.py`（13 条） |
| 用户可见说明 | `SKILL.md` → 「远程索引安全（防源被替换带毒）」 |
| 维护者规则 | `CONTRIBUTING.md` → 「本地测试」「发布目录时的修订号规则」 |
| PR | https://github.com/axel286137079-dot/skill-matcher/pull/1 |
