/**
 * dsh-skill-matcher — core index engine (host half, pure JS, no deps).
 *
 * Faithful JS port of the WorkBuddy `skill-matcher` skill's bin/sync_index.py:
 *   - environment discovery (multi-harness, no hardcoded absolute paths)
 *   - local skill / expert parsing (SKILL.md frontmatter + plugin.json)
 *   - remote open-source index fetch (fail-silent, offline-safe, SHA256 anti-tamper)
 *   - priority merge (local > marketplace > opensource)
 *   - lexical retrieval (L0/L1): concept/alias canonicalization + IDF + multi-path recall;
 *     deeper L2/L3 reasoning stays with the LLM.
 *
 * Embedded seed = the skill's `_manual_skills.json` opensource catalog, so the
 * plugin works offline out of the box. Cache lives under ~/.dsh/dsh-skill-matcher.
 */
import { homedir, platform } from 'node:os';
import { existsSync, readFileSync, mkdirSync, writeFileSync, readdirSync, statSync, appendFileSync } from 'node:fs';
import { join, resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createHash } from 'node:crypto';

const HOME = homedir();
const CACHE_DIR = join(HOME, '.dsh', 'dsh-skill-matcher');
const CACHE_FILE = join(CACHE_DIR, 'cache.json');
const CONFIG_FILE = join(CACHE_DIR, 'config.json');
const LOG_FILE = join(CACHE_DIR, 'query.log.jsonl');
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
    join(HOME, '.dsh', 'skills'),     // DSH 用户技能根（user-dsh 源）
    join(HOME, '.agents', 'skills'),  // DSH agent 技能目录（带版本后缀，如 ui-ux-pro-max-0.1.0）
    join(HOME, '.skills'),
  );
  for (const root of [cwd, process.cwd()]) {
    if (root) {
      cands.push(join(root, '.workbuddy', 'skills'));
      cands.push(join(root, '.dsh', 'skills'));
      cands.push(join(root, '.agents', 'skills'));
    }
  }
  return dedupeDirs(cands);
}

/** WorkBuddy/CodeBuddy 官方内置技能目录（已装可用，不算市场未装）。 */
export function discoverBuiltinSkillDirs() {
  const cands = [];
  for (const mp of [join(HOME, '.workbuddy', 'plugins', 'marketplaces'),
                    join(HOME, '.codebuddy', 'plugins', 'marketplaces')]) {
    cands.push(join(mp, 'codebuddy-plugins-official', 'plugins'));
  }
  return dedupeDirs(cands);
}

/** 剥离技能目录名末尾的版本号后缀（如 ui-ux-pro-max-0.1.0 → ui-ux-pro-max）。 */
function stripVersionSuffix(name) {
  return name.replace(/-\d+\.\d+(\.\d+)?$/, '');
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
  // 本地已装技能（常规技能目录 + DSH agent 目录，目录名可能带版本后缀）
  for (const sd of discoverSkillDirs(cwd)) {
    if (!existsSync(sd)) continue;
    for (const entry of readdirSync(sd)) {
      const skmd = join(sd, entry, 'SKILL.md');
      if (!existsSync(skmd)) continue;
      try {
        const parsed = parseSkillFrontmatter(readFileSync(skmd, 'utf8'));
        if (!parsed) continue;
        const id = stripVersionSuffix(entry);
        localIds.add(id);
        items.push({
          id,
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
  // 官方内置技能（codebuddy-plugins-official/plugins/*/SKILL.md，已装可用，非市场未装）
  for (const bd of discoverBuiltinSkillDirs()) {
    if (!existsSync(bd)) continue;
    for (const entry of readdirSync(bd)) {
      const skmd = join(bd, entry, 'SKILL.md');
      if (!existsSync(skmd)) continue;
      try {
        const parsed = parseSkillFrontmatter(readFileSync(skmd, 'utf8'));
        if (!parsed) continue;
        const id = stripVersionSuffix(entry);
        if (localIds.has(id)) continue; // 已被常规目录收录则不重复
        localIds.add(id);
        items.push({
          id,
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

/** 专家 id 兜底：plugin.json 未显式声明 id 时，才用路径推导并告警。 */
function fallbackExpertId(pj) {
  const derived = (pj.split('/').slice(-4, -3)[0]) || '';
  if (derived) console.warn(`[skill-matcher] 专家 plugin.json 缺 id 字段，用路径推导（易错，建议补 id）: ${derived}`);
  return derived;
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
          id: d.id || d.name || d.agentName || fallbackExpertId(pj),
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
  // 安装白名单：仅官方 SkillHub CLI 命令自动执行；git clone 一律需用户确认（供应链防投毒）。
  if (!install) return false;
  const s = String(install).trim();
  if (s.startsWith('skillhub install ')) return true;
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

/** 同名去重：同一来源下同名只留一条（如 skill-matcher 与 skill-matcher__skillhub 同 local 同名，
 *  优先保留 id === name 的规范条目）；跨来源同名（本地旧版 vs 市场新版）视为不同条目，互不吞并。 */
export function dedupeByName(items) {
  const byKey = new Map();
  for (const s of items) {
    const key = `${s.source || ''}\u0000${s.name || s.id}`;
    const cur = byKey.get(key);
    if (!cur) byKey.set(key, s);
    else if (cur.id !== cur.name && s.id === s.name) byKey.set(key, s);
  }
  return [...byKey.values()];
}

// ---------- config (externalized weights/match/logging) ----------

const DEFAULT_CONFIG = {
  scoring: {
    nameExact: 8,   // 名称完全命中
    namePartial: 6, // 名称包含
    tag: 4,         // 标签命中
    desc: 1.5,      // 描述词频权重（已归一化长度）
    descCap: 3,     // 描述词频封顶
    lengthB: 0.5,   // 长度归一化指数（越大惩罚越长）
    tieEps: 1.0,    // 相关度差 < 此值视为接近，触发 local 优先
  },
  match: {
    candidateK: 15, // 召回条数（交给 LLM 的候选池）
    displayN: 5,    // 默认展示条数
    expertSlots: 2, // 专家展示配额
  },
  logging: { enabled: false },
};

let _cfg = null;
let _cfgMtime = 0;

/** 读取外部配置（可选 ~/.dsh/dsh-skill-matcher/config.json），损坏/缺失回退内置默认。 */
export function loadConfig(force = false) {
  if (!force && _cfg) return _cfg;
  try {
    if (existsSync(CONFIG_FILE)) {
      const st = statSync(CONFIG_FILE);
      if (!force && _cfg && st.mtimeMs === _cfgMtime) return _cfg;
      const raw = JSON.parse(readFileSync(CONFIG_FILE, 'utf8'));
      _cfg = deepMerge(structuredClone(DEFAULT_CONFIG), raw);
      _cfgMtime = st.mtimeMs;
      return _cfg;
    }
  } catch (e) {
    console.warn('[skill-matcher] config 读取失败，回退内置默认:', e?.message || e);
  }
  _cfg = structuredClone(DEFAULT_CONFIG);
  return _cfg;
}

function deepMerge(base, patch) {
  if (!patch || typeof patch !== 'object') return base;
  for (const k of Object.keys(patch)) {
    const v = patch[k];
    if (v && typeof v === 'object' && !Array.isArray(v) && base[k] && typeof base[k] === 'object') {
      deepMerge(base[k], v);
    } else {
      base[k] = v;
    }
  }
  return base;
}

/** 本地目录指纹：轻量（只 stat 顶层技能/专家目录 + 目录内条目数），检测「装了/删了新技能」。 */
function computeLocalFingerprint(cwd) {
  const dirs = [...discoverSkillDirs(cwd), ...discoverBuiltinSkillDirs(), ...discoverExpertRoots(cwd)];
  const parts = [];
  for (const d of dirs) {
    try {
      const st = statSync(d);
      parts.push(`${d}:${st.mtimeMs}:${readdirSync(d).length}`);
    } catch { parts.push(`${d}:missing`); }
  }
  return createHash('sha1').update(parts.join('|')).digest('hex').slice(0, 16);
}

/** 查询日志（默认关）：一行 JSONL，只记 query + top ids，不记用户上下文。 */
export function logQuery(row) {
  const cfg = loadConfig();
  if (!cfg.logging?.enabled) return;
  try {
    mkdirSync(CACHE_DIR, { recursive: true });
    appendFileSync(LOG_FILE, JSON.stringify({ ts: Date.now(), ...row }) + '\n', 'utf8');
  } catch { /* ignore */ }
}

// ---------- 5. matching (concept/alias canonicalization + IDF + multi-path recall) ----------

// 中文停用字/词：过滤"个/要/需要"这类无信息量的 token，避免无关条目靠虚词得分。
const CJK_STOP_CHARS = new Set(['的', '了', '我', '你', '他', '她', '它', '个', '要', '需', '想', '是', '和', '与',
  '或', '在', '也', '就', '都', '吗', '呢', '吧', '啊', '么', '很', '做', '用', '有', '没', '不', '给', '让', '请', '帮', '好', '会', '能', '这', '那', '这', '些']);
const CJK_STOP_BIGRAMS = new Set(['需要', '可以', '能够', '我们', '你们', '他们', '这个', '那个', '什么', '怎么',
  '为什么', '如何', '一个', '一些', '这种', '这样', '那样', '就是', '不是', '还有', '自己', '事情', '东西',
  '问题', '时候', '现在', '已经', '一直', '真的', '其实', '但是', '因为', '所以', '如果', '然后', '知道',
  '想要', '希望', '应该', '可能', '觉得', '想要', '帮我', '请问']);

// concept 规范化表：中英双向归一到同一 canonical id（查询词与索引词先 canonize 再匹配）。
// 这样「量化/quant/quantitative」全命中同一 id，跨语言召回在索引期即可打通。
const CONCEPT_ALIAS = {
  // 设计 / 界面 / 前端
  '设计': 'design', 'design': 'design', 'designer': 'design', '界面': 'ui', 'ui': 'ui', 'ux': 'ux', '用户界面': 'ui',
  '前端': 'frontend', 'frontend': 'frontend', 'front-end': 'frontend',
  '桌面': 'desktop', 'desktop': 'desktop', '网页': 'web', 'web': 'web', '网站': 'web', '页面': 'page', 'page': 'page',
  '应用': 'app', 'app': 'app', 'application': 'app',
  // 开发
  '开发': 'develop', 'develop': 'develop', 'development': 'develop', 'developer': 'develop',
  '构建': 'build', 'build': 'build', '代码': 'code', 'code': 'code', '编码': 'code', '编程': 'code',
  '程序': 'program', 'program': 'program', '后端': 'backend', 'backend': 'backend', 'back-end': 'backend',
  '接口': 'api', 'api': 'api', '工具': 'tool', 'tool': 'tool',
  // 量化 / 交易 / 金融
  '量化': 'quant', 'quant': 'quant', 'quantitative': 'quant',
  '交易': 'trading', 'trading': 'trading', 'trade': 'trading',
  '回测': 'backtest', 'backtest': 'backtest', 'backtesting': 'backtest',
  '股票': 'stock', 'stock': 'stock', 'stocks': 'stock', '基金': 'fund', 'fund': 'fund',
  '黄金': 'gold', 'gold': 'gold', '行情': 'quote', 'quote': 'quote', '市场': 'market', 'market': 'market',
  // 文档 / 创作
  '文档': 'document', 'document': 'document', 'doc': 'document', '写作': 'writing', 'writing': 'writing',
  '报告': 'report', 'report': 'report', '论文': 'paper', 'paper': 'paper',
  '演示': 'slides', 'slides': 'slides', 'ppt': 'slides', 'pptx': 'slides', '幻灯片': 'slides',
  '演讲': 'speech', 'speech': 'speech', '直播': 'livestream', 'livestream': 'livestream',
  // 数据 / 分析
  '数据': 'data', 'data': 'data', '分析': 'analysis', 'analysis': 'analysis', 'analytics': 'analysis',
  '图表': 'chart', 'chart': 'chart', '可视化': 'visualization', 'visualization': 'visualization',
  // 多媒体
  '图片': 'image', 'image': 'image', '图像': 'image', '视频': 'video', 'video': 'video',
  '音频': 'audio', 'audio': 'audio', '音乐': 'music', 'music': 'music',
  '头像': 'avatar', 'avatar': 'avatar', '截图': 'screenshot', 'screenshot': 'screenshot',
  // 通用
  '搜索': 'search', 'search': 'search', '翻译': 'translate', 'translate': 'translate', 'translation': 'translate',
  '邮件': 'email', 'email': 'email', 'mail': 'email', '简历': 'resume', 'resume': 'resume',
  '笔记': 'note', 'note': 'note', 'notes': 'note', '记忆': 'memory', 'memory': 'memory',
  '测试': 'test', 'test': 'test', 'testing': 'test', '部署': 'deploy', 'deploy': 'deploy', 'deployment': 'deploy',
  '安全': 'security', 'security': 'security', '加密': 'encrypt', 'encrypt': 'encrypt', 'encryption': 'encrypt',
  '密码': 'password', 'password': 'password', '登录': 'login', 'login': 'login', '注册': 'register', 'register': 'register',
  '数据库': 'database', 'database': 'database', 'db': 'database', '服务器': 'server', 'server': 'server',
  '网络': 'network', 'network': 'network', '模型': 'model', 'model': 'model', '学习': 'learning', 'learning': 'learning',
  '训练': 'train', 'train': 'train', 'training': 'train',
  '教育': 'education', 'education': 'education', '助手': 'assistant', 'assistant': 'assistant',
  '机器人': 'bot', 'bot': 'bot', 'robot': 'bot', '生成': 'generate', 'generate': 'generate', 'generation': 'generate',
  '创建': 'create', 'create': 'create', '编辑': 'edit', 'edit': 'edit', 'editing': 'edit',
  '下载': 'download', 'download': 'download', '上传': 'upload', 'upload': 'upload', '分享': 'share', 'share': 'share',
  // 情绪 / 心理（领域黑话，一词多义才需手工）
  '内耗': 'anxiety', '焦虑': 'anxiety', 'anxiety': 'anxiety', '焦虑症': 'anxiety',
  '情绪': 'emotion', 'emotion': 'emotion', '情绪化': 'emotion', '纠结': 'anxiety', '担心': 'anxiety', '害怕': 'anxiety',
  '心态': 'mindset', 'mindset': 'mindset', '自卑': 'self-esteem', 'self-esteem': 'self-esteem', '自信': 'self-esteem',
  '原生家庭': 'family', '家庭': 'family', 'family': 'family',
};

function canonize(term) {
  return CONCEPT_ALIAS[term] || term;
}

/** 原始分词：英文词 + 中文 2-gram（过停用字/停用 bigram）。返回去重后的 string[]。 */
export function rawTokenize(text) {
  if (!text) return [];
  const lower = String(text).toLowerCase();
  const tokens = new Set();
  for (const m of lower.matchAll(/[a-z0-9_]+/g)) tokens.add(m[0]);
  const cjk = lower.match(/[\u4e00-\u9fff]+/g) || [];
  for (const run of cjk) {
    for (let i = 0; i + 1 < run.length; i++) {
      const bg = run.slice(i, i + 2);
      if (CJK_STOP_CHARS.has(bg[0]) || CJK_STOP_CHARS.has(bg[1])) continue;
      if (CJK_STOP_BIGRAMS.has(bg)) continue;
      tokens.add(bg);
    }
  }
  return [...tokens].filter(Boolean);
}

/** 查询侧分析：token 带 weight + type，防止扩词漂移（设计==design 强，设计~ux 弱）。 */
export function analyzeQuery(query) {
  const raw = rawTokenize(query);
  const out = new Map(); // term -> {weight, type}
  for (const t of raw) {
    const c = canonize(t);
    // 原词（exact 召回，权重最高）
    const cur = out.get(t);
    if (!cur || cur.weight < 1.0) out.set(t, { weight: 1.0, type: 'original' });
    // canonical（alias 召回）
    if (c !== t) {
      const curc = out.get(c);
      if (!curc || curc.weight < 0.9) out.set(c, { weight: 0.9, type: 'alias' });
    }
  }
  return [...out.entries()].map(([term, meta]) => ({ term, ...meta }));
}

/** 索引侧：entry → 搜索词集合（含 concept 扩展，name/tag/desc 全字段）。 */
function entryTerms(entry) {
  const name = (entry.name || '').toLowerCase();
  const desc = (entry.description || '').toLowerCase();
  const tags = (entry.tags || []).map((t) => String(t).toLowerCase()).join(' ');
  const terms = new Set();
  const add = (tok) => {
    terms.add(tok);
    const c = canonize(tok);
    if (c !== tok) terms.add(c);
  };
  for (const t of rawTokenize(name)) add(t);
  for (const t of rawTokenize(desc)) add(t);
  for (const t of rawTokenize(tags)) add(t);
  return terms;
}

/** 给索引附加检索统计：每个条目的 _t 搜索词 + 全局 IDF / 文档数 / 平均描述长度。 */
function attachStats(skills, experts) {
  const s2 = skills.map((e) => ({ ...e, _t: entryTerms(e) }));
  const e2 = experts.map((e) => ({ ...e, _t: entryTerms(e) }));
  const df = Object.create(null); // 普通对象（可 JSON 序列化，缓存命中后 idf 仍可用）
  let totalLen = 0;
  for (const d of [...s2, ...e2]) {
    const seen = new Set();
    totalLen += (d.description || '').length;
    for (const t of d._t) {
      if (!seen.has(t)) { seen.add(t); df[t] = (df[t] || 0) + 1; }
    }
    delete d._t; // _t(Set) 不可序列化，用完即弃，避免写进缓存
  }
  const n = s2.length + e2.length;
  return { skills: s2, experts: e2, _df: df, _n: n, _avgdl: n ? totalLen / n : 1 };
}

function idf(term, stats) {
  const df = stats._df[term] || 0;
  return Math.log(1 + stats._n / (1 + df));
}

/** 词边界匹配：英文 token 用 \b 边界（避免 "remotion" 误含 "emotion"），中文 token 用 substring。 */
function hasWord(text, token) {
  if (!token) return false;
  if (/^[a-z0-9_]+$/.test(token)) {
    return new RegExp(`\\b${token}\\b`, 'i').test(text);
  }
  return text.includes(token);
}

/** 词边界计数（英文用 \b，中文用 substring）。 */
function countWord(hay, token) {
  if (!token) return 0;
  if (/^[a-z0-9_]+$/.test(token)) {
    const m = hay.match(new RegExp(`\\b${token}\\b`, 'gi'));
    return m ? m.length : 0;
  }
  let n = 0, i = 0;
  while ((i = hay.indexOf(token, i)) !== -1) { n++; i += token.length; }
  return n;
}

function scoreEntry(entry, weightedTokens, stats) {
  const w = loadConfig().scoring;
  const name = (entry.name || '').toLowerCase();
  const desc = (entry.description || '').toLowerCase();
  const tags = (entry.tags || []).map((t) => String(t).toLowerCase());
  let score = 0;
  const matched = new Set();
  for (const wt of weightedTokens) {
    const t = wt.term;
    if (!t) continue;
    const wv = wt.weight;
    const idfv = idf(t, stats);
    // 三路召回：name 精确 > name 包含 > tag > desc（字段权重递减）
    if (name === t) { score += wv * idfv * w.nameExact; matched.add(t); }
    else if (hasWord(name, t)) { score += wv * idfv * w.namePartial; matched.add(t); }
    if (tags.some((tag) => tag === t || hasWord(tag, t))) { score += wv * idfv * w.tag; matched.add(t); }
    const tf = countWord(desc, t);
    if (tf > 0) {
      const norm = Math.min(tf, w.descCap) / Math.pow(1 + desc.length / Math.max(stats._avgdl, 1), w.lengthB);
      score += wv * idfv * w.desc * norm;
      matched.add(t);
    }
  }
  return { score, matched: [...matched] };
}

function buildReason(e, matched) {
  const name = (e.name || e.id || '').toLowerCase();
  const parts = [];
  for (const t of matched.slice(0, 8)) {
    if (name === t) parts.push(`名称命中"${t}"`);
    else if (hasWord(name, t)) parts.push(`名称含"${t}"`);
    else parts.push(`相关词"${t}"`);
  }
  const src = e.source === 'local' ? '已装' : (e.source === 'marketplace' ? '市场' : '开源');
  return `${src}；` + (parts.slice(0, 3).join('、') || '关键词相关');
}

const DECISION_SIGNALS = ['怎么办', '该不该', '要不要', '会不会', '是不是', '好烦', '纠结', '犹豫', '担心', '焦虑', '怕', '万一', '帮帮我', '害怕'];

/** 轻量意图识别：决策/情绪 → decision；开发/构建 → build；操作步骤 → howto；解释 → explain；默认 general。 */
export function detectIntent(query) {
  const q = String(query || '');
  if (DECISION_SIGNALS.some((s) => q.includes(s))) return 'decision';
  if (/做一个|做出|开发|构建|搭建|实现|写一个|创建|design|build|create|develop|make/i.test(q)) return 'build';
  if (/怎么用|怎么操作|步骤|教程|怎么弄|怎么做|流程/.test(q)) return 'howto';
  if (/是什么|什么意思|为什么|区别|原理/.test(q)) return 'explain';
  return 'general';
}

/** 相关度接近（差 < tieEps）时，本地已装优先；否则纯按相关度。已装不再进相关度公式。 */
function rankWithTiebreak(list) {
  const eps = loadConfig().scoring.tieEps;
  const SOURCE_ORDER = { local: 0, marketplace: 1, opensource: 2 };
  list.sort((a, b) => {
    const d = b.score - a.score;
    if (Math.abs(d) < eps) return (SOURCE_ORDER[a.source] ?? 9) - (SOURCE_ORDER[b.source] ?? 9);
    return d;
  });
  return list;
}

/** 分池召回：skills / experts 分开打分排序，返回 { skills, experts, intent }。 */
export function match(query, index, { candidateK, includeExperts = true } = {}) {
  loadConfig();
  const k = candidateK ?? loadConfig().match.candidateK;
  const qTokens = analyzeQuery(query);
  if (qTokens.length === 0) return { skills: [], experts: [], intent: detectIntent(query) };
  const stats = index._df ? index : attachStats(index.skills, index.experts);
  const scorePool = (list) => list
    .map((e) => {
      const r = scoreEntry(e, qTokens, stats);
      return { ...e, score: r.score, matched: r.matched, reason: buildReason(e, r.matched) };
    })
    .filter((e) => e.score > 0);
  let skills = scorePool(stats.skills);
  let experts = includeExperts ? scorePool(stats.experts) : [];
  skills = rankWithTiebreak(skills);
  experts = rankWithTiebreak(experts);
  return { skills: skills.slice(0, k), experts: experts.slice(0, k), intent: detectIntent(query) };
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

export function buildIndex(cwd, { offline = false } = {}) {
  loadConfig();
  const { items: skillsAuto, localIds } = collectSkills(cwd);
  const remote = fetchRemoteSkillsSync(offline);
  for (const r of remote) if (!skillsAuto.find((s) => s.id === r.id)) skillsAuto.push(r);
  const skills = mergeByPriority(skillsAuto, SEED_OPENSOURCE);
  const expertsAuto = collectExperts(cwd);
  const expertIds = new Set(expertsAuto.map((e) => e.id));
  const skillsFinal = skills.filter((s) => !expertIds.has(s.id));
  return attachStats(dedupeByName(skillsFinal), expertsAuto);
}

// fetchRemoteSkills is async; provide a sync wrapper for buildIndex convenience.
function fetchRemoteSkillsSync(offline) {
  if (offline) return [];
  return [];
}

export async function getIndex({ cwd, offline = false } = {}) {
  const now = Date.now();
  if (offline) {
    const idx = buildIndex(cwd, { offline: true });
    return { ...idx, builtAt: now, offline: true, localFp: computeLocalFingerprint(cwd) };
  }
  const fp = computeLocalFingerprint(cwd);
  if (_memCache && now - _memCache.builtAt < FRESH_MS && _memCache.localFp === fp) return _memCache;
  const fileCache = readCache();
  if (fileCache && now - fileCache.builtAt < FRESH_MS && fileCache.localFp === fp) {
    _memCache = fileCache;
    return fileCache;
  }
  const idx = buildIndex(cwd, { offline: false });
  const remote = await fetchRemoteSkills(false);
  const merged = mergeByPriority(idx.skills, remote.length ? remote : SEED_OPENSOURCE);
  const expertIds = new Set(idx.experts.map((e) => e.id));
  const finalSkills = dedupeByName(merged.filter((s) => !expertIds.has(s.id)));
  const stats = attachStats(finalSkills, idx.experts);
  const prev = readCache() || {};
  const wrapped = { ...stats, builtAt: now, offline: false, localFp: fp,
    remoteHashes: prev.remoteHashes || {} };
  _memCache = wrapped;
  writeCache(wrapped);
  return wrapped;
}

export { SENSITIVE_KEYWORDS, HOME, CACHE_DIR };
export const __dirname = dirname(fileURLToPath(import.meta.url));
