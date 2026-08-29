/**
 * dsh-skill-matcher — core index engine (host half, pure JS, no deps).
 *
 * Faithful JS port of the WorkBuddy `skill-matcher` skill's bin/sync_index.py:
 *   - environment discovery (multi-harness, no hardcoded absolute paths)
 *   - local skill / expert parsing (SKILL.md frontmatter + plugin.json)
 *   - remote open-source index fetch (fail-silent, offline-safe)
 *   - priority merge (local > marketplace > opensource)
 *   - keyword recall scoring (L0/L1); deeper L2/L3 reasoning stays with the LLM.
 *
 * Embedded seed = the skill's `_manual_skills.json` opensource catalog, so the
 * plugin works offline out of the box. Cache lives under ~/.dsh/dsh-skill-matcher.
 */
import { homedir, platform } from 'node:os';
import { existsSync, readFileSync, mkdirSync, writeFileSync, readdirSync, statSync } from 'node:fs';
import { join, resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createHash } from 'node:crypto';

const HOME = homedir();
const CACHE_DIR = join(HOME, '.dsh', 'dsh-skill-matcher');
const CACHE_FILE = join(CACHE_DIR, 'cache.json');
const FRESH_MS = 24 * 60 * 60 * 1000;

const DEFAULT_REMOTE = {
  name: 'skill-matcher 官方开源目录',
  url: 'https://raw.githubusercontent.com/axel286137079-dot/skill-matcher-index/main/index.json',
  install_hint: '见条目 install 字段',
};

// Sensitive keywords — never auto-contribute / surface entries whose id carries these.
const SENSITIVE_KEYWORDS = ['config', 'secret', 'credential', 'password', 'private',
  'internal', 'personal', 'token', 'api-key', 'auth', 'key'];

// Embedded opensource seed (mirror of skill-matcher/index/_manual_skills.json).
const SEED_OPENSOURCE = [
  { id: 'docx', name: 'docx', description: '创建和编辑 Microsoft Word 文档（.docx）：从零生成、排版、批注。Official Anthropic document skill.', install: 'git clone https://github.com/anthropics/skills', source: 'opensource', origin: 'anthropics/skills' },
  { id: 'pdf', name: 'pdf', description: 'PDF 文档处理：读取、提取文本/表格、合并拆分、生成 PDF。Official Anthropic document skill.', install: 'git clone https://github.com/anthropics/skills', source: 'opensource', origin: 'anthropics/skills' },
  { id: 'pptx', name: 'pptx', description: '创建和编辑 PowerPoint 演示文稿（.pptx），支持模板与图表。Official Anthropic document skill.', install: 'git clone https://github.com/anthropics/skills', source: 'opensource', origin: 'anthropics/skills' },
  { id: 'xlsx', name: 'xlsx', description: '创建和编辑 Excel 表格（.xlsx）：数据、公式、图表、格式。Official Anthropic document skill.', install: 'git clone https://github.com/anthropics/skills', source: 'opensource', origin: 'anthropics/skills' },
  { id: 'artifacts-builder', name: 'artifacts-builder', description: '构建可运行的 Web 应用原型（HTML/CSS/JS），适合快速做交互 demo。Official Anthropic skill.', install: 'git clone https://github.com/anthropics/skills', source: 'opensource', origin: 'anthropics/skills' },
  { id: 'webapp-testing', name: 'webapp-testing', description: 'Web 应用测试：用 Playwright 编写并执行端到端测试。Official Anthropic skill.', install: 'git clone https://github.com/anthropics/skills', source: 'opensource', origin: 'anthropics/skills' },
  { id: 'brand-guidelines', name: 'brand-guidelines', description: '品牌视觉规范设计与一致性检查（logo 使用、配色、字体）。Official Anthropic skill.', install: 'git clone https://github.com/anthropics/skills', source: 'opensource', origin: 'anthropics/skills' },
  { id: 'image-editing', name: 'image-editing', description: '图像编辑与处理（裁剪、滤镜、合成），配合图像生成模型使用。Official Anthropic skill.', install: 'git clone https://github.com/anthropics/skills', source: 'opensource', origin: 'anthropics/skills' },
  { id: 'mcp-builder', name: 'mcp-builder', description: '构建 MCP 服务器：从需求到可运行的服务端代码。Official Anthropic skill.', install: 'git clone https://github.com/anthropics/skills', source: 'opensource', origin: 'anthropics/skills' },
  { id: 'mcp-architect', name: 'mcp-architect', description: 'MCP 架构设计：规划服务器结构、工具清单与连接方案。Official Anthropic skill.', install: 'git clone https://github.com/anthropics/skills', source: 'opensource', origin: 'anthropics/skills' },
  { id: 'slack-gif-creator', name: 'slack-gif-creator', description: '创建 Slack GIF：脚本生成动画并上传。Official Anthropic skill.', install: 'git clone https://github.com/anthropics/skills', source: 'opensource', origin: 'anthropics/skills' },
  { id: 'video-editing', name: 'video-editing', description: '视频编辑：剪辑、字幕、转场（ffmpeg）。Official Anthropic skill.', install: 'git clone https://github.com/anthropics/skills', source: 'opensource', origin: 'anthropics/skills' },
  { id: 'canvas-design', name: 'canvas-design', description: '设计画布排版与设计规范。Official Anthropic skill.', install: 'git clone https://github.com/anthropics/skills', source: 'opensource', origin: 'anthropics/skills' },
];

// ---------- 1. environment discovery ----------

function envDir(...names) {
  for (const n of names) {
    const v = process.env[n];
    if (v) return v;
  }
  return null;
}

function dedupeDirs(cands) {
  const seen = new Set();
  const out = [];
  for (const d of cands) {
    let p;
    try { p = resolve(d); } catch { continue; }
    if (existsSync(p) && statSync(p).isDirectory() && !seen.has(p)) {
      seen.add(p);
      out.push(p);
    }
  }
  return out;
}

export function discoverSkillDirs(cwd) {
  const cands = [];
  const e = envDir('SKILL_MATCHER_SKILLS_DIR', 'WORKBUDDY_SKILLS_DIR');
  if (e) cands.push(e);
  cands.push(
    join(HOME, '.workbuddy', 'skills'),
    join(HOME, '.claude', 'skills'),
    join(HOME, '.codebuddy', 'skills'),
    join(HOME, '.skills'),
  );
  for (const root of [cwd, process.cwd()]) {
    if (root) cands.push(join(root, '.workbuddy', 'skills'));
  }
  return dedupeDirs(cands);
}

export function discoverExpertRoots(cwd) {
  const cands = [];
  const e = envDir('SKILL_MATCHER_EXPERTS_DIR', 'WORKBUDDY_EXPERTS_DIR');
  if (e) cands.push(e);
  cands.push(
    join(HOME, '.workbuddy', 'plugins', 'marketplaces'),
    join(HOME, '.claude', 'plugins', 'marketplaces'),
  );
  for (const root of [cwd, process.cwd()]) {
    if (root) cands.push(join(root, '.workbuddy', 'plugins', 'marketplaces'));
  }
  return dedupeDirs(cands);
}

// ---------- 2. parsing ----------

export function parseSkillFrontmatter(text) {
  const m = text.match(/^---\s*\n([\s\S]*?)\n---/);
  if (!m) return null;
  const fm = m[1];
  const name = (fm.match(/^name:\s*(.+)$/m) || [])[1];
  const desc = (fm.match(/^description:\s*(.+)$/m) || [])[1];
  if (!name || !desc) return null;
  return { name: name.trim(), description: desc.trim() };
}

function pickBilingual(d, key, lang) {
  const v = d[key];
  if (v && typeof v === 'object') return v[lang] || v.en || v.zh || '';
  return v || '';
}

function globFiles(parent, relPattern) {
  // Resolves a simple pattern where the LAST segment is a filename and earlier
  // segments are directory wildcards ('**' / '*' / literal). Returns absolute
  // file paths (not dirs) so callers can read plugin.json / marketplace.json.
  const out = [];
  if (!existsSync(parent)) return out;
  const parts = relPattern.split('/');
  let cur = [parent];
  for (let i = 0; i < parts.length; i++) {
    const part = parts[i];
    const isLast = i === parts.length - 1;
    const next = [];
    for (const base of cur) {
      if (!existsSync(base)) continue;
      let entries;
      try { entries = readdirSync(base); } catch { continue; }
      for (const entry of entries) {
        const full = join(base, entry);
        let isDir = false;
        try { isDir = statSync(full).isDirectory(); } catch { continue; }
        if (isLast) {
          if (entry === part) next.push(full); // file match
        } else if (isDir && (part === '**' || part === '*' || part === entry)) {
          next.push(full);
        }
      }
    }
    cur = next;
  }
  return cur;
}

// ---------- 3. collection ----------

export function collectSkills(cwd) {
  const items = [];
  const localIds = new Set();
  for (const sd of discoverSkillDirs(cwd)) {
    if (!existsSync(sd)) continue;
    for (const entry of readdirSync(sd)) {
      const skmd = join(sd, entry, 'SKILL.md');
      if (!existsSync(skmd)) continue;
      try {
        const parsed = parseSkillFrontmatter(readFileSync(skmd, 'utf8'));
        if (!parsed) continue;
        localIds.add(entry);
        items.push({
          id: entry,
          name: parsed.name,
          description: parsed.description,
          install: null,
          source: 'local',
          kind: 'skill',
          tags: [],
        });
      } catch { /* ignore */ }
    }
  }
  // marketplace (uninstalled) plugin entries — agents-* are experts, skip here.
  for (const root of discoverExpertRoots(cwd)) {
    const mjs = globFiles(root, '**/.codebuddy-plugin/marketplace.json');
    for (const mj of mjs) {
      try {
        const data = JSON.parse(readFileSync(mj, 'utf8'));
        for (const p of data.plugins || []) {
          const name = p.name;
          if (!name || !p.description || name.startsWith('agents-')) continue;
          if (localIds.has(name)) continue;
          items.push({
            id: name, name, description: p.description,
            install: p.source || null, source: 'marketplace', kind: 'skill', tags: [],
          });
        }
      } catch { /* ignore */ }
    }
  }
  return { items, localIds };
}

export function collectExperts(cwd) {
  const items = [];
  for (const root of discoverExpertRoots(cwd)) {
    // 1) local experts with plugin.json
    const pjs = globFiles(root, '**/plugins/*/.codebuddy-plugin/plugin.json');
    for (const pj of pjs) {
      try {
        const d = JSON.parse(readFileSync(pj, 'utf8'));
        const dn = d.displayName;
        if (!dn) continue;
        items.push({
          id: d.name || d.agentName || (pj.split('/').slice(-4, -3)[0]) || '',
          name: pickBilingual(d, 'displayName', 'zh') || pickBilingual(d, 'displayName', 'en') || dn,
          description: pickBilingual(d, 'displayDescription', 'zh') || pickBilingual(d, 'displayDescription', 'en'),
          source: 'local', kind: 'expert',
          tags: (d.tags || []).filter((t) => t && typeof t === 'object' && t.zh).map((t) => t.zh),
        });
      } catch { /* ignore */ }
    }
    // 2) marketplace agents-* official expert packs
    const mjs = globFiles(root, '**/.codebuddy-plugin/marketplace.json');
    for (const mj of mjs) {
      try {
        const data = JSON.parse(readFileSync(mj, 'utf8'));
        for (const p of data.plugins || []) {
          const name = p.name || '';
          if (!name.startsWith('agents-') || !p.description) continue;
          items.push({
            id: name,
            name: pickBilingual(p, 'displayName', 'zh') || pickBilingual(p, 'displayName', 'en') || name,
            description: pickBilingual(p, 'displayDescription', 'zh') || pickBilingual(p, 'displayDescription', 'en') || p.description,
            source: 'marketplace', kind: 'expert', tags: [],
          });
        }
      } catch { /* ignore */ }
    }
  }
  return items;
}

export function isTrustedInstall(install) {
  // 安装白名单：仅官方/知名可信前缀自动执行，其余一律需用户确认。
  if (!install) return false;
  const s = String(install).trim();
  if (s.startsWith('skillhub install ')) return true;
  if (s.startsWith('git clone https://github.com/anthropics/')) return true;
  if (s.startsWith('git clone https://github.com/axel286137079-dot/')) return true;
  return false;
}

export async function fetchRemoteSkills(offline) {
  if (offline) return [];
  try {
    const ac = new AbortController();
    const timer = setTimeout(() => ac.abort(), 5000);
    const res = await fetch(DEFAULT_REMOTE.url, { signal: ac.signal });
    clearTimeout(timer);
    if (!res.ok) return [];
    // 防篡改：拉取内容 SHA256，与上次记录比对，不一致拒绝更新
    const buf = Buffer.from(await res.arrayBuffer());
    const digest = createHash('sha256').update(buf).digest('hex');
    const known = readCache()?.remoteHashes?.[DEFAULT_REMOTE.url];
    if (known && known !== digest) {
      console.warn('[skill-matcher] remote index SHA256 mismatch; keep old version (source may be tampered)');
      return [];
    }
    const data = JSON.parse(buf.toString('utf-8'));
    if (!known) {
      const prev = readCache() || {};
      writeCache({ ...prev, remoteHashes: { ...(prev.remoteHashes || {}), [DEFAULT_REMOTE.url]: digest } });
    }
    const lst = Array.isArray(data) ? data : (data.skills || data.plugins || []);
    return lst
      .filter((it) => it && (it.id || it.name))
      .map((it) => ({
        id: it.id || it.name,
        name: it.name || it.id,
        description: it.description || '',
        install: it.install || DEFAULT_REMOTE.install_hint || null,
        source: 'opensource',
        kind: 'skill',
        tags: it.tags || [],
        origin: it.origin || DEFAULT_REMOTE.name,
      }));
  } catch {
    return [];
  }
}

// ---------- 4. merge ----------

export function mergeByPriority(auto, manual) {
  const merged = new Map();
  const localIds = new Set();
  for (const it of auto) {
    merged.set(it.id, it);
    if (it.source === 'local') localIds.add(it.id);
  }
  for (const it of manual) {
    if (!it || !it.id) continue;
    if (localIds.has(it.id)) continue; // never override a locally installed skill
    merged.set(it.id, it);
  }
  return [...merged.values()];
}

/** 同名去重：同一技能可能装于多环境（如 skill-matcher 与 skill-matcher__skillhub），
 *  优先保留 id === name 的规范条目，避免列表里出现两条同名。 */
export function dedupeByName(items) {
  const byName = new Map();
  for (const s of items) {
    const key = s.name || s.id;
    const cur = byName.get(key);
    if (!cur) byName.set(key, s);
    else if (cur.id !== cur.name && s.id === s.name) byName.set(key, s);
  }
  return [...byName.values()];
}

export function buildIndex(cwd, { offline = false } = {}) {
  const { items: skillsAuto, localIds } = collectSkills(cwd);
  const remote = fetchRemoteSkillsSync(offline);
  for (const r of remote) if (!skillsAuto.find((s) => s.id === r.id)) skillsAuto.push(r);
  const skills = mergeByPriority(skillsAuto, SEED_OPENSOURCE);
  const expertsAuto = collectExperts(cwd);
  let experts = expertsAuto;
  // experts never enter the skill list
  const expertIds = new Set(experts.map((e) => e.id));
  const skillsFinal = skills.filter((s) => !expertIds.has(s.id));
  return { skills: dedupeByName(skillsFinal), experts };
}

// fetchRemoteSkills is async; provide a sync wrapper for buildIndex convenience.
function fetchRemoteSkillsSync(offline) {
  if (offline) return [];
  // best-effort synchronous probe via a microtask is awkward; delegate to cache.
  // We re-run async path from callers that need freshness; offline uses seed only.
  return [];
}

// ---------- cache ----------

let _memCache = null;

export function readCache() {
  try {
    if (existsSync(CACHE_FILE)) {
      const data = JSON.parse(readFileSync(CACHE_FILE, 'utf8'));
      if (data && data.skills && data.experts) return data;
    }
  } catch { /* ignore */ }
  return null;
}

function writeCache(data) {
  try {
    mkdirSync(CACHE_DIR, { recursive: true });
    writeFileSync(CACHE_FILE, JSON.stringify(data), 'utf8');
  } catch { /* ignore */ }
}

export async function getIndex({ cwd, offline = false } = {}) {
  const now = Date.now();
  if (offline) {
    const idx = buildIndex(cwd, { offline: true });
    return { ...idx, builtAt: now, offline: true };
  }
  if (_memCache && now - _memCache.builtAt < FRESH_MS) return _memCache;
  const fileCache = readCache();
  if (fileCache && now - fileCache.builtAt < FRESH_MS) { _memCache = fileCache; return fileCache; }
  const idx = buildIndex(cwd, { offline: false });
  // fill remote at async time
  const remote = await fetchRemoteSkills(false);
  const merged = mergeByPriority(idx.skills, remote.length ? remote : SEED_OPENSOURCE);
  const expertIds = new Set(idx.experts.map((e) => e.id));
  const finalSkills = dedupeByName(merged.filter((s) => !expertIds.has(s.id)));
  const prev = readCache() || {};
  const wrapped = { skills: finalSkills, experts: idx.experts, builtAt: now, offline: false,
    remoteHashes: prev.remoteHashes || {} };
  _memCache = wrapped;
  writeCache(wrapped);
  return wrapped;
}

// ---------- 5. matching (L0/L1 keyword recall + intent hint) ----------

// 中文停用字/词：过滤"个/要/需要"这类无信息量的 token，避免无关条目靠虚词得分。
const CJK_STOP_CHARS = new Set(['的', '了', '我', '你', '他', '她', '它', '个', '要', '需', '想', '是', '和', '与',
  '或', '在', '也', '就', '都', '吗', '呢', '吧', '啊', '么', '很', '做', '用', '有', '没', '不', '给', '让', '请', '帮', '好', '会', '能']);
const CJK_STOP_BIGRAMS = new Set(['需要', '可以', '能够', '我们', '你们', '他们', '这个', '那个', '什么', '怎么',
  '为什么', '如何', '一个', '一些', '这种', '这样', '那样', '就是', '不是', '还有', '自己', '事情', '东西',
  '问题', '时候', '现在', '已经', '一直', '真的', '其实', '但是', '因为', '所以', '如果', '然后', '知道',
  '想要', '希望', '应该', '可能', '觉得', '想要', '帮我', '请问']);

export function tokenize(text) {
  if (!text) return [];
  const lower = String(text).toLowerCase();
  const tokens = new Set();
  for (const m of lower.matchAll(/[a-z0-9_]+/g)) tokens.add(m[0]);
  const cjk = lower.match(/[\u4e00-\u9fff]+/g) || [];
  for (const run of cjk) {
    for (let i = 0; i + 1 < run.length; i++) {
      const bg = run.slice(i, i + 2);
      // 2-gram，跳过含停用字的虚词组合与常见无信息 bigram
      if (CJK_STOP_CHARS.has(bg[0]) || CJK_STOP_CHARS.has(bg[1])) continue;
      if (CJK_STOP_BIGRAMS.has(bg)) continue;
      tokens.add(bg);
    }
  }
  return [...tokens].filter(Boolean);
}

function scoreEntry(entry, tokens) {
  let score = 0;
  const name = (entry.name || entry.id || '').toLowerCase();
  const desc = (entry.description || '').toLowerCase();
  const tags = (entry.tags || []).join(' ').toLowerCase();
  const matched = new Set();
  for (const t of tokens) {
    if (!t) continue;
    if (name === t) { score += 8; matched.add(t); }
    else if (name.includes(t)) { score += 6; matched.add(t); }
    if (tags.includes(t)) { score += 4; matched.add(t); }
    const n = desc.split(t).length - 1; // 描述词频，封顶 ×3，让语义接近的明显领先
    if (n > 0) { score += 2 * Math.min(n, 3); matched.add(t); }
  }
  if (desc.length >= 40) score += 1;
  // 已装技能加权：同等匹配度下本地已装优先（用户最常要"在已装里挑一个"）
  if (entry.source === 'local') score = Math.round(score * 1.2) + 2;
  return { score, matched: [...matched] };
}

function buildReason(e, matched) {
  const parts = [];
  const name = (e.name || e.id || '').toLowerCase();
  const desc = (e.description || '').toLowerCase();
  for (const t of matched) {
    if (name === t) parts.push(`名称完全命中"${t}"`);
    else if (name.includes(t)) parts.push(`名称含"${t}"`);
    else if (desc.includes(t)) parts.push(`描述含"${t}"`);
  }
  const src = e.source === 'local' ? '已装' : (e.source === 'marketplace' ? '市场' : '开源');
  return `${src}；` + (parts.slice(0, 3).join('、') || '关键词相关');
}

const DECISION_SIGNALS = ['怎么办', '该不该', '要不要', '会不会', '是不是', '好烦', '纠结', '犹豫', '担心', '焦虑', '怕', '万一', '帮帮我', '害怕'];

/** 轻量意图识别：决策/情绪 → decision；操作步骤 → howto；解释 → explain；默认 general。 */
export function detectIntent(query) {
  const q = String(query || '');
  if (DECISION_SIGNALS.some((s) => q.includes(s))) return 'decision';
  if (/怎么用|怎么操作|步骤|教程|怎么弄|怎么做|流程/.test(q)) return 'howto';
  if (/是什么|什么意思|为什么|区别|原理/.test(q)) return 'explain';
  return 'general';
}

export function match(query, index, { topN = 5, includeExperts = true } = {}) {
  const tokens = tokenize(query);
  if (tokens.length === 0) return [];
  let pool = index.skills;
  if (includeExperts) pool = pool.concat(index.experts);
  const scored = [];
  for (const e of pool) {
    const { score, matched } = scoreEntry(e, tokens);
    if (score > 0) scored.push({ ...e, score, matched, reason: buildReason(e, matched) });
  }
  scored.sort((a, b) => b.score - a.score);
  return scored.slice(0, topN);
}

export { SENSITIVE_KEYWORDS, HOME, CACHE_DIR };
export const __dirname = dirname(fileURLToPath(import.meta.url));
