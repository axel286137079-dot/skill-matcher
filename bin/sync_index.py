#!/usr/bin/env python3
"""技能与专家索引同步脚本 (Skill Matcher) —— 通用版

「别人装了这个技能之后，怎么自动读到他本地的技能/专家/开源社区？」
答案在三个机制：

  1. 环境探测：不写死任何机器的绝对路径。自动按候选列表探测技能/专家目录
     （WorkBuddy 标准位 ~/.workbuddy、Claude Code ~/.claude、项目级 .workbuddy、
      通用 ~/.skills，且支持环境变量覆盖）。
  2. 远程开源索引：index/_sources.json 配置远程 JSON 索引 URL，联网时自动拉取
     合并（失败不阻塞，离线可用）。
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
    """读取远程索引哈希记录（url -> sha256），用于防篡改校验。"""
    try:
        return json.loads(HASH_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_remote_hash(url, digest):
    h = _load_remote_hashes()
    h[url] = digest
    write_json(HASH_FILE, h)


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


def fetch_remote_skills(offline=False):
    """拉取 index/_sources.json 里配置的远程开源索引（失败静默跳过）。"""
    if offline:
        return []
    src = OUT_DIR / "_sources.json"
    if not src.exists():
        return []
    try:
        sources = json.loads(src.read_text(encoding="utf-8"))
    except Exception:
        return []
    items = []
    for s in sources.get("remote_indexes", []):
        url = s.get("url")
        if not url:
            continue
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                raw = r.read()
            digest = hashlib.sha256(raw).hexdigest()
            known = _load_remote_hashes().get(url)
            if known and known != digest:
                print(f"  [remote] {s.get('name', url)} 内容哈希与上次不一致，已拒绝更新"
                      f"（源可能被篡改，保持旧版；如确为正常更新请清除 index/_remote_hashes.json）")
                continue
            data = json.loads(raw.decode("utf-8"))
            if not known:
                _save_remote_hash(url, digest)
            lst = data if isinstance(data, list) else (data.get("skills") or data.get("plugins") or [])
            for it in lst:
                if not it.get("id") and not it.get("name"):
                    continue
                iid = it.get("id") or it["name"]
                items.append({
                    "id": iid,
                    "name": it.get("name") or iid,
                    "description": it.get("description", ""),
                    "install": it.get("install") or s.get("install_hint", ""),
                    "source": "opensource",
                    "origin": s.get("name", url),
                })
            print(f"  [remote] {s.get('name', url)}: +{len(lst)} 条")
        except Exception as e:
            print(f"  [remote] {s.get('name', url)} 跳过: {type(e).__name__}")
    return items


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


def export_open_source(skills):
    """导出全局开源目录（数据资产）：发布到 GitHub 后作为远程源 index.json。"""
    os_items = [s for s in skills if s.get("source") == "opensource"]
    data = {
        "name": "skill-matcher 开源技能目录",
        "description": "由 skill-matcher 维护的全局开源技能索引，供所有安装者联网同步。",
        "version": 2,
        "updated_at": time.strftime("%Y-%m-%d"),
        "skills": os_items,
    }
    path = OUT_DIR / "opensource-index.json"
    write_json(path, data)
    print(f"opensource 全局目录: {len(os_items)} 条 -> {path}")


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
    add_only_new(skills_auto, fetch_remote_skills(offline=offline))
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
    export_open_source(skills)
    print(f"written to {OUT_DIR}")


if __name__ == "__main__":
    main()
