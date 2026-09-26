#!/usr/bin/env python3
"""技能与专家索引同步脚本 (Skill Matcher) —— 通用版

「别人装了这个技能之后，怎么自动读到他本地的技能/专家/开源社区？」
答案在三个机制：

  1. 环境探测：不写死任何机器的绝对路径。自动按候选列表探测技能/专家目录
     （WorkBuddy 标准位 ~/.workbuddy、Claude Code ~/.claude、项目级 .workbuddy、
      通用 ~/.skills，且支持环境变量覆盖）。
  2. 远程开源索引：index/_sources.json 配置远程 JSON 索引 URL，联网时自动拉取
     合并（失败不阻塞，离线可用）。防篡改 = SHA256 **+ 目录修订号** 双门控：
     内容变了必须同时满足「顶层 version 是正整数且严格大于上次接受值」才接受；
     拒绝时继续用上次接受的目录，不退回内置种子。
     已同步过的客户端若要接受新目录，需要服务端把 version 递增发布。
  3. 保鲜：SKILL.md 匹配前检查索引新鲜度，过期自动重跑本脚本。

数据源（按优先级，后者不覆盖前者）：
  A. 本地已装技能  <skill_dirs>/*/SKILL.md
  B. 本地专家      <expert_roots>/*/plugins/*/.codebuddy-plugin/plugin.json
  C. 市场未装条目  <expert_roots>/*/.codebuddy-plugin/marketplace.json（agents-* 归专家）
  D. 远程开源索引  index/_sources.json
  E. 手动精选      index/_manual_skills.json / _manual_experts.json
                   （覆盖市场条目；绝不覆盖本地已装）

用法：
  python3 bin/sync_index.py           # 完整同步（含远程）
  python3 bin/sync_index.py --offline # 仅本地+市场+手动，跳过远程
  python3 bin/sync_index.py --collect-contributions  # 本地侧：生成贡献候选清单（不上传）
  python3 bin/sync_index.py --merge-contributions    # 中央侧：合并已审核贡献并导出发布
  python3 bin/sync_index.py --submit-contribution    # 一键贡献：挑选→生成文件→gh已登录则自动推送
"""

import hashlib
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parent.parent / "index"
HASH_FILE = OUT_DIR / "_remote_hashes.json"


def _load_remote_hashes():
    """读取远程索引记录（url -> 记录）。

    兼容两种历史格式：
      - 旧：{url: "<sha256 字符串>"}（无修订号、无条目快照）
      - 新：{url: {hash, version, accepted_at, entries}}
    """
    try:
        data = json.loads(HASH_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _is_catalog_version(v):
    """目录修订号必须是正整数（bool 是 int 的子类，须显式排除）。"""
    return isinstance(v, int) and not isinstance(v, bool) and v > 0


def _read_catalog_version(data):
    """从目录 JSON 顶层取整数修订号；取不到返回 None（不得当成 0 或「最新」）。"""
    if not isinstance(data, dict):
        return None
    v = data.get("version")
    return v if _is_catalog_version(v) else None


def _normalize_record(raw):
    """把一条远程源记录规整为 {hash, version, entries}；兼容旧格式（纯字符串哈希）。"""
    if isinstance(raw, str):
        return {"hash": raw or None, "version": None, "entries": None}
    if isinstance(raw, dict):
        h, v, e = raw.get("hash"), raw.get("version"), raw.get("entries")
        return {
            "hash": h if isinstance(h, str) and h else None,
            "version": v if _is_catalog_version(v) else None,
            "entries": e if isinstance(e, list) and e else None,
        }
    return None


def read_remote_record(url):
    """读取某远程源的「已接受」记录；无记录返回 None。"""
    return _normalize_record(_load_remote_hashes().get(url))


def _save_remote_record(url, digest, version, entries):
    h = _load_remote_hashes()
    h[url] = {
        "hash": digest,
        "version": version,
        "accepted_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "entries": entries,
    }
    write_json(HASH_FILE, h)


def decide_remote_update(digest, version, record):
    """远程目录更新裁决（纯函数，便于测试）。返回 (accept, reason, version)。

      - 无记录（首次拉取）→ 接受，记录 hash + version
      - hash 相同（内容未变）→ 接受（幂等；旧记录借此补上 version）
      - hash 变了，但 version 是正整数且**严格大于**上次接受值 → 接受
      - 其余（version 缺失 / 非正整数 / 没变大，或内容变了但 version 没变）→ 拒绝

    防篡改仍然在：不是「哈希不同就一律接受」。
    """
    prev = record if isinstance(record, dict) else _normalize_record(record)
    prev_hash = (prev or {}).get("hash")
    prev_ver = (prev or {}).get("version")
    next_ver = version if _is_catalog_version(version) else None

    if not prev_hash:
        return True, "first-fetch", next_ver
    if prev_hash == digest:
        return True, "unchanged", next_ver if next_ver is not None else prev_ver
    if next_ver is None:
        return False, "rejected:version-missing", None
    if prev_ver is None:
        return False, "rejected:no-baseline-version", None
    if next_ver <= prev_ver:
        return False, "rejected:version-not-increased", None
    return True, "version-bumped", next_ver


def _normalize_tags(tags):
    """目录条目的 tags 一律规整为**字符串数组**（引擎按字符串标签打分，勿改成 {zh} 对象）。
    只保证类型与去空，不改写标签本身的值。"""
    if not isinstance(tags, list):
        return []
    return [t for t in tags if isinstance(t, str) and t.strip()]


def normalize_remote_entries(data, default_origin, install_hint=""):
    """目录条目归一化：保留远程 tags 与条目**自身**的 origin（origin 缺失才回退到源名）。"""
    lst = data if isinstance(data, list) else (
        (data.get("skills") or data.get("plugins") or []) if isinstance(data, dict) else [])
    out = []
    for it in lst:
        if not isinstance(it, dict):
            continue
        iid = it.get("id") or it.get("name")
        if not iid:
            continue
        out.append({
            "id": iid,
            "name": it.get("name") or iid,
            "description": it.get("description", ""),
            "install": it.get("install") or install_hint,
            "source": "opensource",
            "kind": "skill",
            "tags": _normalize_tags(it.get("tags")),
            "origin": it.get("origin") or default_origin,
        })
    return out


def _last_accepted_entries():
    """上次接受的开源目录（旧记录没有条目快照时，从已导出的全局目录回收）。"""
    try:
        d = json.loads((OUT_DIR / "opensource-index.json").read_text(encoding="utf-8"))
        lst = d.get("skills") if isinstance(d, dict) else d
        return [x for x in (lst or []) if isinstance(x, dict) and x.get("source") == "opensource"]
    except Exception:
        return []


def _record_entries(record):
    """记录里存的条目快照；旧记录没有快照则退回上次导出的开源目录。"""
    if record and record.get("entries"):
        return record["entries"]
    return _last_accepted_entries()


# ---------- 1. 环境探测 ----------

def _env(*names):
    for n in names:
        v = os.environ.get(n)
        if v:
            return Path(v)
    return None


def _dedupe_dirs(cands):
    seen, dirs = set(), []
    for d in cands:
        if d.exists() and d.is_dir() and d not in seen:
            seen.add(d)
            dirs.append(d)
    return dirs


def discover_skill_dirs():
    """探测本机存在的技能根目录列表（去重）。"""
    cands = []
    e = _env("SKILL_MATCHER_SKILLS_DIR", "WORKBUDDY_SKILLS_DIR")
    if e:
        cands.append(e)
    cands += [
        Path.home() / ".workbuddy" / "skills",
        Path.home() / ".claude" / "skills",
        Path.home() / ".codebuddy" / "skills",
        Path.home() / ".dsh" / "skills",     # DSH 用户技能根（user-dsh 源）
        Path.home() / ".agents" / "skills",  # DSH agent 技能目录（带版本后缀）
        Path.home() / ".skills",
    ]
    for root in [Path.cwd(), Path(__file__).resolve().parent.parent.parent]:
        cands.append(root / ".workbuddy" / "skills")
        cands.append(root / ".dsh" / "skills")
        cands.append(root / ".agents" / "skills")
    return _dedupe_dirs(cands)


def discover_builtin_skill_dirs():
    """WorkBuddy/CodeBuddy 官方内置技能目录（已装可用，不算市场未装）。"""
    cands = []
    for mp in [Path.home() / ".workbuddy" / "plugins" / "marketplaces",
               Path.home() / ".codebuddy" / "plugins" / "marketplaces"]:
        cands.append(mp / "codebuddy-plugins-official" / "plugins")
    return _dedupe_dirs(cands)


def strip_version_suffix(name: str) -> str:
    """剥离技能目录名末尾的版本号后缀（如 ui-ux-pro-max-0.1.0 → ui-ux-pro-max）。"""
    return re.sub(r"-\d+\.\d+(\.\d+)?$", "", name)


def discover_expert_roots():
    """探测本机存在的专家市场根目录列表（去重）。"""
    cands = []
    e = _env("SKILL_MATCHER_EXPERTS_DIR", "WORKBUDDY_EXPERTS_DIR")
    if e:
        cands.append(e)
    cands += [
        Path.home() / ".workbuddy" / "plugins" / "marketplaces",
        Path.home() / ".claude" / "plugins" / "marketplaces",
    ]
    for root in [Path.cwd(), Path(__file__).resolve().parent.parent.parent]:
        cands.append(root / ".workbuddy" / "plugins" / "marketplaces")
    return _dedupe_dirs(cands)


# ---------- 2. 解析 ----------

def parse_skill_skmd(path: Path):
    """解析 SKILL.md 的 YAML frontmatter，返回 (name, description) 或 None。"""
    text = path.read_text(encoding="utf-8", errors="ignore")
    m = re.search(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
    if not m:
        return None
    fm = m.group(1)
    name = re.search(r"^name:\s*(.+)$", fm, re.M)
    desc = re.search(r"^description:\s*(.+)$", fm, re.M)
    if not name or not desc:
        return None
    return name.group(1).strip(), desc.group(1).strip()


def _pick(d, key, lang):
    """兼容 marketplace/plugin.json 的双语字段：dict{zh,en} 或 纯字符串。"""
    v = d.get(key)
    if isinstance(v, dict):
        return v.get(lang, "") or v.get("en", "") or v.get("zh", "") or ""
    return v or ""


# ---------- 3. 收集 ----------

def collect_skills():
    """返回 (skills, local_ids)：本地已装技能 + 市场未装技能（排除 agents-* 专家）。"""
    items, local_ids = [], set()
    # 本地已装技能（常规技能目录 + DSH agent 目录，目录名可能带版本后缀）
    for sd in discover_skill_dirs():
        for skmd in sorted(sd.glob("*/SKILL.md")):
            try:
                parsed = parse_skill_skmd(skmd)
                if not parsed:
                    continue
                name, desc = parsed
                sid = strip_version_suffix(skmd.parent.name)
                local_ids.add(sid)
                items.append({
                    "id": sid,
                    "name": name,
                    "description": desc,
                    "install": None,
                    "source": "local",
                })
            except Exception:
                continue
    # 官方内置技能（codebuddy-plugins-official/plugins/*/SKILL.md，已装可用）
    for bd in discover_builtin_skill_dirs():
        for skmd in sorted(bd.glob("*/SKILL.md")):
            try:
                parsed = parse_skill_skmd(skmd)
                if not parsed:
                    continue
                name, desc = parsed
                sid = strip_version_suffix(skmd.parent.name)
                if sid in local_ids:
                    continue
                local_ids.add(sid)
                items.append({
                    "id": sid,
                    "name": name,
                    "description": desc,
                    "install": None,
                    "source": "local",
                })
            except Exception:
                continue

    # 市场未装条目（marketplace.json 兜底；agents-* 归专家，不进技能）
    for root in discover_expert_roots():
        for mj in sorted(root.glob("*/.codebuddy-plugin/marketplace.json")):
            try:
                data = json.loads(mj.read_text(encoding="utf-8"))
                for p in data.get("plugins", []):
                    name = p.get("name")
                    if not name or not p.get("description"):
                        continue
                    if name.startswith("agents-"):
                        continue  # 归专家
                    if name in local_ids:
                        continue  # 本地已装优先，不被市场覆盖
                    items.append({
                        "id": name,
                        "name": name,
                        "description": p["description"],
                        "install": p.get("source"),
                        "source": "marketplace",
                    })
            except Exception:
                continue
    return items, local_ids


def collect_experts():
    """本地专家（plugin.json 双语）+ 市场 agents-* 专家包。"""
    items = []
    for root in discover_expert_roots():
        # 1) 有 plugin.json 的本地专家
        for pj in sorted(root.glob("*/plugins/*/.codebuddy-plugin/plugin.json")):
            try:
                d = json.loads(pj.read_text(encoding="utf-8"))
                dn = d.get("displayName")
                if not dn:
                    continue
                items.append({
                    "id": d.get("id") or d.get("name") or d.get("agentName") or pj.parents[1].name,
                    "displayName_zh": _pick(d, "displayName", "zh"),
                    "displayName_en": _pick(d, "displayName", "en"),
                    "description_zh": _pick(d, "displayDescription", "zh"),
                    "description_en": _pick(d, "displayDescription", "en"),
                    "profession_zh": _pick(d, "profession", "zh"),
                    "profession_en": _pick(d, "profession", "en"),
                    "tags": [t.get("zh", "") for t in d.get("tags", []) if isinstance(t, dict) and t.get("zh")],
                    "source": "local",
                })
            except Exception:
                continue
        # 2) 市场 agents-* 官方专家包
        for mj in sorted(root.glob("*/.codebuddy-plugin/marketplace.json")):
            try:
                data = json.loads(mj.read_text(encoding="utf-8"))
                for p in data.get("plugins", []):
                    name = p.get("name", "")
                    if not name.startswith("agents-") or not p.get("description"):
                        continue
                    items.append({
                        "id": name,
                        "displayName_zh": _pick(p, "displayName", "zh") or name,
                        "displayName_en": _pick(p, "displayName", "en") or name,
                        "description_zh": _pick(p, "displayDescription", "zh") or p.get("description", ""),
                        "description_en": _pick(p, "displayDescription", "en") or p.get("description", ""),
                        "profession_zh": "",
                        "profession_en": "",
                        "tags": [],
                        "source": "marketplace",
                    })
            except Exception:
                continue
    return items


def _http_get(url, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()


def fetch_remote_skills(offline=False, opener=None, sources_path=None):
    """拉取 index/_sources.json 里配置的远程开源索引（version-gated，失败静默）。

    返回 (items, status)：
      - items：**应当使用的开源条目** —— 本次接受则是新条目，拒绝则是上次接受的目录
      - status：{url: {reason, version, hash}}，便于调用方与测试断言

    ⚠️ 拒绝时**不会**退回内置种子，也不会拿空列表冒充成功。
    """
    status = {}
    if offline:
        # 离线不拉取，但仍沿用上次接受的开源目录（不退回种子）
        return _dedupe_entries(_last_accepted_entries()), {
            "__offline__": {"reason": "offline", "version": None, "hash": None}}

    src = sources_path if sources_path is not None else (OUT_DIR / "_sources.json")
    if not src.exists():
        return [], status
    try:
        sources = json.loads(src.read_text(encoding="utf-8"))
    except Exception:
        return [], status

    records = _load_remote_hashes()
    seen, items = set(), []

    def _extend(extra):
        for it in extra:
            if isinstance(it, dict) and it.get("id") and it["id"] not in seen:
                seen.add(it["id"])
                items.append(it)

    for s in sources.get("remote_indexes", []):
        url = s.get("url")
        if not url:
            continue
        name = s.get("name", url)
        record = _normalize_record(records.get(url))
        keep = {"reason": "unknown", "version": (record or {}).get("version"),
                "hash": (record or {}).get("hash")}
        try:
            raw = (opener or _http_get)(url)
        except Exception as e:
            status[url] = {**keep, "reason": f"network-error:{type(e).__name__}"}
            print(f"  [remote] {name} 跳过: {type(e).__name__}")
            _extend(_record_entries(record))
            continue

        digest = hashlib.sha256(raw).hexdigest()
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception as e:
            status[url] = {**keep, "reason": "parse-error"}
            print(f"  [remote] {name} 跳过: JSON 解析失败（{type(e).__name__}）")
            _extend(_record_entries(record))
            continue

        version = _read_catalog_version(data)
        accept, reason, new_version = decide_remote_update(digest, version, record)
        if not accept:
            status[url] = {**keep, "reason": reason}
            hint = (f"（version {record['version']}）" if (record or {}).get("version")
                    else "（旧记录无修订号；如确为正常更新，请删除 index/_remote_hashes.json 后重试）")
            print(f"  [remote] {name} 更新被拒绝（{reason}）；继续使用上次接受的目录{hint}")
            _extend(_record_entries(record))
            continue

        entries = normalize_remote_entries(data, name, s.get("install_hint", ""))
        _save_remote_record(url, digest, new_version, entries)
        status[url] = {"reason": reason, "version": new_version, "hash": digest}
        _extend(entries)
        vtxt = f"，version {new_version}" if new_version is not None else ""
        print(f"  [remote] {name}: +{len(entries)} 条（{reason}{vtxt}）")
    return items, status


def _dedupe_entries(entries):
    """按 id 去重（保持顺序）。"""
    seen, out = set(), []
    for it in entries:
        if isinstance(it, dict) and it.get("id") and it["id"] not in seen:
            seen.add(it["id"])
            out.append(it)
    return out


def load_manual(kind: str):
    path = OUT_DIR / f"_manual_{kind}.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def merge_by_priority(auto, manual):
    """合并规则：本地已装(local) 最高优先，manual 不覆盖 local；
    manual 覆盖 market/opensource 条目。"""
    merged, local_ids = {}, set()
    for it in auto:
        merged[it["id"]] = it
        if it.get("source") == "local":
            local_ids.add(it["id"])
    for it in manual:
        if not it.get("id"):
            continue
        if it["id"] in local_ids:
            continue
        merged[it["id"]] = it
    return list(merged.values())


def add_only_new(items, extras):
    """把远程/新增条目并入，只加 id 不存在的（不覆盖本地与市场）。"""
    ids = {it["id"] for it in items}
    for it in extras:
        if it["id"] not in ids:
            items.append(it)
            ids.add(it["id"])


def write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


# ---------- 5. 社区贡献飞轮 ----------

CONTRIB_DIR = OUT_DIR / "contributions"
CONSENSUS_THRESHOLD = 3  # 共识阈值：同一技能被 ≥N 个不同贡献者提交才自动采纳
SENSITIVE_KEYWORDS = ("config", "secret", "credential", "password", "private",
                      "internal", "personal", "token", "api-key", "auth", "key")


def score_skill(it):
    """候选贡献质量分 0-100（暂无真实使用量，用结构信号代理「排名」）。"""
    desc = it.get("description", "")
    score = 0
    score += min(40, len(desc) // 5)              # 描述完整度
    if len(desc) >= 80:
        score += 10                               # 详细描述
    if it.get("install"):
        score += 20                               # 有开源安装来源
    if re.fullmatch(r"[a-z0-9][a-z0-9-]{2,40}", it.get("id", "")):
        score += 10                               # 命名规范
    if not any(k in it.get("id", "").lower() for k in SENSITIVE_KEYWORDS):
        score += 10                               # 无敏感词
    if it.get("source") == "opensource":
        score += 10                               # 开源来源加分
    return min(100, score)


def collect_contributions():
    """本地侧：扫描本机技能，筛出「值得贡献」的候选清单。

    隐私红线：候选清单仅存本地，必须经用户逐条确认后才构成贡献，默认绝不上传。"""
    print("贡献收集（本地侧）……")
    skills, _ = collect_skills()
    local = [s for s in skills if s["source"] == "local" and s["id"] != "skill-matcher"]
    cands = []
    for s in local:
        if any(k in s["id"].lower() for k in SENSITIVE_KEYWORDS):
            continue
        if len(s.get("description", "")) < 20:
            continue
        s = dict(s)
        s["trust"] = "community"
        s["status"] = "candidate"
        s["score"] = score_skill(s)
        cands.append(s)
    cands.sort(key=lambda x: -x["score"])
    # 标记已在中央目录的，避免重复贡献
    try:
        central = json.loads((OUT_DIR / "opensource-index.json").read_text(encoding="utf-8"))
        central_ids = {c["id"] for c in central.get("skills", [])}
        for c in cands:
            c["dup"] = c["id"] in central_ids
    except Exception:
        pass
    path = CONTRIB_DIR / "candidates.json"
    write_json(path, {"updated_at": time.strftime("%Y-%m-%d %H:%M"), "candidates": cands})
    print(f"候选 {len(cands)} 条（其中已在中央目录 {sum(1 for c in cands if c.get('dup'))} 条）")
    for c in cands[:15]:
        flag = "dup" if c.get("dup") else f"{c['score']}分"
        print(f"  [{flag:>4}] {c['id']}  {c['description'][:36]}…")
    print(f"→ 清单已存: {path}")
    print("→ 下一步：人工审核候选，把选中的条目移到 contributions/approved/ 后跑 --merge-contributions")


def merge_contributions():
    """中央侧：合并贡献并应用共识机制。

    共识规则：同一 id 被 ≥CONSENSUS_THRESHOLD 个不同贡献者提交 → approved 进目录；
    否则 pending（等待更多共识）。"""
    print("贡献合并（中央侧，共识机制）……")
    contrib_files = set(CONTRIB_DIR.glob("*.json")) | set((CONTRIB_DIR / "approved").glob("*.json"))
    contrib_files = {f for f in contrib_files if f.name not in ("candidates.json", "pending.json", "audit-report.json")}
    per_id = {}  # id -> {"contributors": set, "item": dict}
    for f in sorted(contrib_files):
        contributor = f.stem
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            items = data if isinstance(data, list) else data.get("skills", [])
        except Exception as e:
            print(f"  跳过 {f.name}: {e}")
            continue
        for it in items:
            iid = it.get("id")
            if not iid:
                continue
            rec = per_id.setdefault(iid, {"contributors": set(), "item": dict(it)})
            rec["contributors"].add(contributor)
    if not per_id:
        print("  没有贡献文件（把贡献放在 contributions/*.json，文件名=贡献者）")
        return
    approved, pending = [], []
    for iid, rec in per_id.items():
        n = len(rec["contributors"])
        it = rec["item"]
        it["source"] = "community"
        it["contributors"] = sorted(rec["contributors"])
        it["consensus"] = n
        if n >= CONSENSUS_THRESHOLD:
            it["status"] = "approved"
            approved.append(it)
        else:
            it["status"] = "pending"
            pending.append(it)
    out = {}
    try:
        central = json.loads((OUT_DIR / "opensource-index.json").read_text(encoding="utf-8"))
        for s in central.get("skills", []):
            out[s["id"]] = s
    except Exception:
        pass
    for it in approved:
        out[it["id"]] = it
    data = {
        "name": "skill-matcher 开源技能目录",
        "description": "由 skill-matcher 维护的全局开源技能索引，供所有安装者联网同步。",
        "version": 2,
        "updated_at": time.strftime("%Y-%m-%d"),
        "skills": list(out.values()),
    }
    write_json(OUT_DIR / "opensource-index.json", data)
    write_json(CONTRIB_DIR / "pending.json", {
        "updated_at": time.strftime("%Y-%m-%d %H:%M"),
        "threshold": CONSENSUS_THRESHOLD,
        "pending": pending,
    })
    print(f"  共识通过 {len(approved)} 条（≥{CONSENSUS_THRESHOLD} 人）→ 已合并，目录现共 {len(out)} 条")
    print(f"  待共识 {len(pending)} 条（<{CONSENSUS_THRESHOLD} 人）→ 存 pending.json")
    print("→ 上传 opensource-index.json 到 GitHub 仓库 index.json 即全网生效")


def audit_contributions():
    """机器预审：对 contributions/ 下所有待审条目做敏感/质量/查重检查，输出预审报告。

    供 AI 审核员（或人工）决策：verdict=ok 可进 approved；flag 需人工复查。"""
    print("贡献机器预审……")
    files = set(CONTRIB_DIR.glob("*.json")) | set((CONTRIB_DIR / "approved").glob("*.json"))
    files = {f for f in files if f.name not in ("candidates.json", "pending.json", "audit-report.json")}
    central_ids = set()
    try:
        central = json.loads((OUT_DIR / "opensource-index.json").read_text(encoding="utf-8"))
        central_ids = {c["id"] for c in central.get("skills", [])}
    except Exception:
        pass
    report = []
    for f in sorted(files):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            items = data if isinstance(data, list) else data.get("skills", [])
        except Exception:
            continue
        for it in items:
            iid = it.get("id")
            if not iid:
                continue
            desc = it.get("description", "")
            flags = []
            if any(k in iid.lower() or k in desc.lower() for k in SENSITIVE_KEYWORDS):
                flags.append("敏感词")
            if iid in central_ids:
                flags.append("中央目录已存在")
            if len(desc) < 20:
                flags.append("描述过短")
            if not it.get("install") and not it.get("origin"):
                flags.append("缺来源")
            report.append({
                "file": f.name,
                "id": iid,
                "score": score_skill(it),
                "flags": flags,
                "verdict": "flag" if flags else "ok",
            })
    write_json(CONTRIB_DIR / "audit-report.json", {
        "updated_at": time.strftime("%Y-%m-%d %H:%M"),
        "report": report,
    })
    print(f"  预审 {len(report)} 条：ok {sum(1 for r in report if r['verdict']=='ok')} / flag {sum(1 for r in report if r['verdict']=='flag')}")
    for r in report:
        if r["verdict"] == "flag":
            print(f"    [flag] {r['id']}  {'/'.join(r['flags'])}")
    print(f"→ 预审报告: {CONTRIB_DIR / 'audit-report.json'}")
    print("→ AI 审核员据此裁决：ok → approved/，flag → 人工复查")


def _catalog_digest(skills):
    """目录内容指纹（仅技能条目本体），用于判断修订号是否需要自增。"""
    payload = json.dumps(skills, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _published_floor(status):
    """已发布目录的修订号下界：本次拉取到的 / 上次接受的，取其中的最大值。"""
    vs = [v.get("version") for v in (status or {}).values() if isinstance(v, dict)]
    vs = [v for v in vs if _is_catalog_version(v)]
    return max(vs) if vs else 0


def export_open_source(skills, floor_version=0):
    """导出全局开源目录（数据资产）：发布到 GitHub 后作为远程源 index.json。

    ⚠️ 修订号 version 必须**单调递增**——客户端只在 version 严格变大时才接受目录更新。
    写死版本号会让已同步的客户端永久拒绝更新。未显式指定时：内容有变则 +1，内容未变则沿用；
    且绝不会低于「已发布版本的已知下界 floor_version」。
    """
    # 本机安装状态不能改写全局发布目录。某个开源技能若已安装，合并后的
    # `skills` 里可能只剩同 id 的 local 条目；仍需从手动精选源补回其
    # opensource 发布记录，否则一次本地同步就会把中央目录条目删掉。
    by_id = {
        s["id"]: s for s in skills
        if s.get("source") == "opensource" and s.get("id")
    }
    for item in load_manual("skills"):
        if (item.get("source") == "opensource" and item.get("id")
                and item["id"] not in by_id):
            by_id[item["id"]] = item
    os_items = list(by_id.values())
    path = OUT_DIR / "opensource-index.json"
    try:
        prev = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        prev = {}
    if not isinstance(prev, dict):
        prev = {}
    prev_version = _read_catalog_version(prev) or 0
    floor = floor_version if _is_catalog_version(floor_version) else 0
    digest = _catalog_digest(os_items)
    if digest == prev.get("content_sha256") and prev_version >= max(floor, 1):
        new_version = prev_version          # 内容未变：沿用，避免无谓的版本噪音
    else:
        new_version = max(prev_version, floor, 0) + 1
    data = {
        "name": "skill-matcher 开源技能目录",
        "description": "由 skill-matcher 维护的全局开源技能索引，供所有安装者联网同步。",
        "version": new_version,
        "content_sha256": digest,
        "updated_at": time.strftime("%Y-%m-%d"),
        "skills": os_items,
    }
    write_json(path, data)
    print(f"opensource 全局目录: {len(os_items)} 条 (version {new_version}) -> {path}")


def submit_contribution():
    """一键贡献（本地侧，opt-in 红线）：
    扫描候选 → 用户逐条挑选 → 生成本地贡献文件 → 若 gh 已登录且仓库可写则自动推送中央目录。
    默认绝不上传任何内容；用户不挑选 = 什么都不发生。"""
    import subprocess
    import base64
    import getpass
    import socket

    print("一键贡献（本地侧，opt-in）……")
    skills, _ = collect_skills()
    local = [s for s in skills if s["source"] == "local" and s["id"] != "skill-matcher"]
    cands = []
    for s in local:
        if any(k in s["id"].lower() for k in SENSITIVE_KEYWORDS):
            continue
        if len(s.get("description", "")) < 20:
            continue
        s = dict(s)
        s["trust"] = "community"
        s["status"] = "candidate"
        s["score"] = score_skill(s)
        cands.append(s)
    cands.sort(key=lambda x: -x["score"])
    try:
        central = json.loads((OUT_DIR / "opensource-index.json").read_text(encoding="utf-8"))
        central_ids = {c["id"] for c in central.get("skills", [])}
        for c in cands:
            c["dup"] = c["id"] in central_ids
    except Exception:
        pass
    fresh = [c for c in cands if not c.get("dup")]
    print(f"本机可贡献候选 {len(fresh)} 条（已在中央目录 {len(cands) - len(fresh)} 条自动跳过）：")
    for i, c in enumerate(fresh[:25], 1):
        print(f"  {i:2d}. {c['id']:<28} {c['score']:3d}分  {c['description'][:32]}…")
    if len(fresh) > 25:
        print(f"  …（其余 {len(fresh) - 25} 条见 candidates.json）")
    if not fresh:
        print("没有新的可贡献候选。")
        return
    # 2) opt-in 挑选（红线：用户逐条确认才构成贡献）
    try:
        choice = input("输入要贡献的编号（逗号分隔，如 1,3,5；all=全部；回车=跳过）> ").strip()
    except EOFError:
        print("非交互环境：请在交互终端运行，或用 --ids 参数。")
        return
    if not choice:
        print("未选择，跳过贡献（什么都没发生）。")
        return
    if choice.lower() == "all":
        picked = fresh
    else:
        idxs = []
        for part in choice.replace("，", ",").split(","):
            part = part.strip()
            if not part:
                continue
            try:
                idxs.append(int(part))
            except ValueError:
                print(f"忽略无效编号: {part}")
        picked = [fresh[i - 1] for i in idxs if 1 <= i <= len(fresh)]
    if not picked:
        print("未选中任何候选，跳过。")
        return
    print(f"选中 {len(picked)} 条：{', '.join(c['id'] for c in picked)}")
    try:
        confirm = input("确认将以上条目作为你的贡献提交？（y/N）> ").strip().lower()
    except EOFError:
        confirm = "n"
    if confirm != "y":
        print("已取消。")
        return
    # 3) 本地贡献文件（文件名 = 贡献者，供 --merge-contributions 共识机制消费）
    user = getpass.getuser() or socket.gethostname()
    CONTRIB_DIR.mkdir(parents=True, exist_ok=True)
    contrib_path = CONTRIB_DIR / f"{user}.json"
    out_items = []
    for c in picked:
        it = dict(c)
        it.pop("dup", None)
        it["status"] = "submitted"
        out_items.append(it)
    write_json(contrib_path, {
        "contributor": user,
        "submitted_at": time.strftime("%Y-%m-%d %H:%M"),
        "skills": out_items,
    })
    print(f"→ 本地贡献文件已生成: {contrib_path}")
    # 4) 推送：gh 已登录且仓库可写时自动推送到中央目录
    try:
        r = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True, timeout=10)
        gh_ok = r.returncode == 0
    except Exception:
        gh_ok = False
    if not gh_ok:
        print("→ 未检测到已登录的 gh CLI。请把该文件提交给维护者")
        print("  （PR 到 skill-matcher-index 仓库 contributions/ 目录，或邮件给维护者）。")
        return
    repo = "axel286137079-dot/skill-matcher-index"
    branch = "main"
    content_b64 = base64.b64encode(contrib_path.read_bytes()).decode()
    put = subprocess.run(
        ["gh", "api", "--method", "PUT", f"repos/{repo}/contents/contributions/{user}.json",
         "-f", f"message=skill-matcher: contribution from {user}",
         "-f", f"content={content_b64}", "-f", f"branch={branch}"],
        capture_output=True, text=True, timeout=60,
    )
    if put.returncode == 0:
        print(f"→ 已推送贡献到中央仓库: https://github.com/{repo}/blob/{branch}/contributions/{user}.json")
        print("  维护者合并（≥3 人共识自动采纳）后，目录全网更新。")
    else:
        print(f"→ 推送未成功（可能无写权限）。请通过 PR 把 {contrib_path.name} 提交到 {repo} 的 contributions/ 目录。")
        if put.stderr.strip():
            print("  原因: " + put.stderr.strip().splitlines()[-1])


def main():
    if "--submit-contribution" in sys.argv:
        submit_contribution()
        return
    if "--collect-contributions" in sys.argv:
        collect_contributions()
        return
    if "--merge-contributions" in sys.argv:
        merge_contributions()
        return
    if "--audit-contributions" in sys.argv:
        audit_contributions()
        return
    offline = "--offline" in sys.argv
    if offline:
        print("offline 模式：跳过远程拉取")

    print(f"技能目录: {[str(d) for d in discover_skill_dirs()] or '（未发现）'}")
    print(f"专家市场: {[str(d) for d in discover_expert_roots()] or '（未发现）'}")

    skills_auto, local_ids = collect_skills()
    remote_items, remote_status = fetch_remote_skills(offline=offline)
    add_only_new(skills_auto, remote_items)
    skills = merge_by_priority(skills_auto, load_manual("skills"))

    experts_auto = collect_experts()
    experts = merge_by_priority(experts_auto, load_manual("experts"))
    expert_ids = {e["id"] for e in experts}
    # 专家条目不进技能列表
    skills = [s for s in skills if s["id"] not in expert_ids]

    write_json(OUT_DIR / "skills.json", skills)
    write_json(OUT_DIR / "experts.json", experts)
    print(f"skills:  {len(skills)}  (本地 {sum(1 for s in skills if s['source']=='local')} / 市场 {sum(1 for s in skills if s['source']=='marketplace')} / 开源 {sum(1 for s in skills if s['source']=='opensource')})")
    print(f"experts: {len(experts)}  (本地 {sum(1 for e in experts if e['source']=='local')} / 市场 {sum(1 for e in experts if e['source']=='marketplace')})")
    export_open_source(skills, floor_version=_published_floor(remote_status))
    print(f"written to {OUT_DIR}")


if __name__ == "__main__":
    main()
