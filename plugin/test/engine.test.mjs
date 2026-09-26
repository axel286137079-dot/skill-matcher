/**
 * dsh-skill-matcher — 远程目录「版本门控」测试
 *
 * 覆盖：
 *   1. 首次拉取会记下哈希和 version
 *   2. version 3→4 且内容变化：接受，并保留 tags（且用条目自身的 origin）
 *   3. version 不变但内容变化：拒绝，且仍用上一份，而不是种子
 *   4. version 变小 / 缺失 / 非正整数：拒绝
 *   5. 旧缓存纯字符串哈希：能读、不崩；内容变了则拒绝（这就是需要一次性动作的场景）
 *   6. 端到端：getIndex 在拒绝时用的是上次接受的目录，而不是内置 SEED
 *
 * 运行：node --test plugin/test/
 */
import { test, beforeEach, after } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync, writeFileSync, readFileSync, mkdirSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createHash } from 'node:crypto';

// ⚠️ engine.js 在模块加载时解析缓存目录 —— 必须先设好环境变量，再动态 import。
const CACHE_DIR = mkdtempSync(join(tmpdir(), 'skill-matcher-test-cache-'));
const SKILLS_DIR = mkdtempSync(join(tmpdir(), 'skill-matcher-test-skills-'));
process.env.SKILL_MATCHER_CACHE_DIR = CACHE_DIR;
process.env.SKILL_MATCHER_SKILLS_DIR = SKILLS_DIR;

const engine = await import('../lib/engine.js');

const CACHE_FILE = join(CACHE_DIR, 'cache.json');
// 与 engine.js 的 DEFAULT_REMOTE.url 保持一致
const URL_ = 'https://raw.githubusercontent.com/axel286137079-dot/skill-matcher-index/main/index.json';

const sha = (s) => createHash('sha256').update(Buffer.from(s, 'utf8')).digest('hex');

const SKILLS_V3 = [
  { id: 'test-alpha', name: 'Test Alpha', description: 'alpha skill v3', install: 'git clone x', origin: 'acme/tools', tags: ['alpha'] },
  { id: 'test-beta', name: 'Test Beta', description: 'beta skill', install: 'git clone y', origin: 'acme/tools', tags: ['beta'] },
];
const SKILLS_V4 = [
  { id: 'test-alpha', name: 'Test Alpha', description: 'alpha skill v4', install: 'git clone x', origin: 'acme/tools', tags: ['alpha', 'updated'] },
  { id: 'test-beta', name: 'Test Beta', description: 'beta skill', install: 'git clone y', origin: 'acme/tools', tags: ['beta'] },
  { id: 'test-gamma', name: 'Test Gamma', description: 'gamma skill', install: 'git clone z', origin: 'other/repo', tags: ['Gamma', '  ', 42, null] },
];

/** 构造远程目录 JSON 文本。version 传 null 表示目录里根本没有 version 字段。 */
function catalog(version, skills) {
  const obj = { name: 'skill-matcher 开源技能目录', skills };
  if (version !== null) obj.version = version;
  return JSON.stringify(obj, null, 2);
}

function stubFetch(body, { status = 200 } = {}) {
  globalThis.fetch = async () => new Response(body, { status });
}

function writeCacheFile(obj) {
  mkdirSync(CACHE_DIR, { recursive: true });
  writeFileSync(CACHE_FILE, JSON.stringify(obj), 'utf8');
}
const readCacheFile = () => JSON.parse(readFileSync(CACHE_FILE, 'utf8'));

/** 预置一条「上次接受」的新格式记录。 */
function seedRecord(version, skills, opts = {}) {
  const body = opts.body ?? catalog(version, skills);
  writeCacheFile({
    skills: opts.cacheSkills ?? [],
    experts: [],
    remoteCatalog: { [URL_]: { hash: sha(body), version, acceptedAt: Date.now(), entries: opts.entries ?? engine.normalizeRemoteEntries({ skills }) } },
  });
  return body;
}

/** 端到端用的临时工作区：带一个项目级 skills 目录，保证 localFp 唯一（否则会撞上内存缓存）。 */
function makeCwd(tag) {
  const cwd = mkdtempSync(join(tmpdir(), `sm-cwd-${tag}-`));
  mkdirSync(join(cwd, '.workbuddy', 'skills'), { recursive: true });
  return cwd;
}

beforeEach(() => { rmSync(CACHE_FILE, { force: true }); });
after(() => {
  rmSync(CACHE_DIR, { recursive: true, force: true });
  rmSync(SKILLS_DIR, { recursive: true, force: true });
});

// ---------- 1. 裁决矩阵（纯函数） ----------

test('decideRemoteUpdate：裁决矩阵', () => {
  const rec = { hash: 'h3', version: 3, entries: [] };
  const d = (digest, version, record) => engine.decideRemoteUpdate({ digest, version, record });

  assert.equal(d('h9', 4, null).reason, 'first-fetch');
  assert.equal(d('h9', null, null).reason, 'first-fetch');       // 首次即使没有 version 也接受（但也记不下修订号）
  assert.equal(d('h3', 3, rec).reason, 'unchanged');
  assert.equal(d('h9', 4, rec).reason, 'version-bumped');
  assert.equal(d('h9', 3, rec).reason, 'rejected:version-not-increased'); // 内容变了但 version 没变
  assert.equal(d('h9', 2, rec).reason, 'rejected:version-not-increased'); // version 变小
  assert.equal(d('h9', null, rec).reason, 'rejected:version-missing');
  assert.equal(d('h9', 0, rec).reason, 'rejected:version-missing');
  assert.equal(d('h9', 2.5, rec).reason, 'rejected:version-missing');
  assert.equal(d('h9', '4', rec).reason, 'rejected:version-missing');    // 字符串不算正整数
  assert.equal(d('h9', 4, { hash: 'h3', version: null, entries: [] }).reason, 'rejected:no-baseline-version');

  assert.equal(d('h9', 4, rec).accept, true);
  assert.equal(d('h9', 3, rec).accept, false);
});

// ---------- 2. 首次拉取 ----------

test('首次拉取：记下哈希和 version', async () => {
  const body = catalog(4, SKILLS_V4);
  stubFetch(body);

  const r = await engine.fetchRemoteSkills(false);
  assert.equal(r.accepted, true);
  assert.equal(r.reason, 'first-fetch');
  assert.equal(r.version, 4);
  assert.equal(r.hash, sha(body));

  const cache = readCacheFile();
  assert.equal(cache.remoteCatalog[URL_].hash, sha(body));
  assert.equal(cache.remoteCatalog[URL_].version, 4);
  assert.equal(cache.remoteCatalog[URL_].entries.length, 3);
  // 条目自身的 origin 不被源名覆盖
  assert.equal(cache.remoteCatalog[URL_].entries.find((e) => e.id === 'test-gamma').origin, 'other/repo');
});

// ---------- 3. version 3 → 4 且内容变化：接受 ----------

test('version 3→4 且内容变化：接受，并保留 tags', async () => {
  seedRecord(3, SKILLS_V3, { body: catalog(3, SKILLS_V3) });
  stubFetch(catalog(4, SKILLS_V4));

  const r = await engine.fetchRemoteSkills(false);
  assert.equal(r.accepted, true);
  assert.equal(r.reason, 'version-bumped');
  assert.equal(r.version, 4);
  assert.deepEqual(r.entries.map((e) => e.id), ['test-alpha', 'test-beta', 'test-gamma']);

  // tags 必须是字符串数组，且保留标签原值；非字符串/空白项被剔除
  const alpha = r.entries.find((e) => e.id === 'test-alpha');
  assert.deepEqual(alpha.tags, ['alpha', 'updated']);
  const gamma = r.entries.find((e) => e.id === 'test-gamma');
  assert.deepEqual(gamma.tags, ['Gamma']);
  assert.ok(gamma.tags.every((t) => typeof t === 'string'));

  // 记录已推进到 v4
  const cache = readCacheFile();
  assert.equal(cache.remoteCatalog[URL_].version, 4);
  assert.equal(cache.remoteCatalog[URL_].hash, sha(catalog(4, SKILLS_V4)));
});

// ---------- 4. version 不变但内容变化：拒绝，且仍用上一份 ----------

test('version 不变但内容变化：拒绝，且仍用上一份（不是种子）', async () => {
  const prevEntries = engine.normalizeRemoteEntries({ skills: SKILLS_V3 });
  seedRecord(3, SKILLS_V3, { body: catalog(3, SKILLS_V3), entries: prevEntries });
  const oldHash = readCacheFile().remoteCatalog[URL_].hash;

  stubFetch(catalog(3, SKILLS_V4)); // 内容变了，version 仍是 3

  const r = await engine.fetchRemoteSkills(false);
  assert.equal(r.accepted, false);
  assert.equal(r.reason, 'rejected:version-not-increased');
  // 用的是上次接受的目录，而不是空数组
  assert.deepEqual(r.entries.map((e) => e.id), ['test-alpha', 'test-beta']);

  // 记录没有被改写
  const cache = readCacheFile();
  assert.equal(cache.remoteCatalog[URL_].version, 3);
  assert.equal(cache.remoteCatalog[URL_].hash, oldHash);
});

test('version 变小：拒绝', async () => {
  seedRecord(3, SKILLS_V3, { body: catalog(3, SKILLS_V3) });
  stubFetch(catalog(2, SKILLS_V4));
  const r = await engine.fetchRemoteSkills(false);
  assert.equal(r.accepted, false);
  assert.equal(r.reason, 'rejected:version-not-increased');
  assert.deepEqual(r.entries.map((e) => e.id), ['test-alpha', 'test-beta']);
});

test('version 缺失：拒绝', async () => {
  seedRecord(3, SKILLS_V3, { body: catalog(3, SKILLS_V3) });
  stubFetch(catalog(null, SKILLS_V4)); // 目录里没有 version 字段
  const r = await engine.fetchRemoteSkills(false);
  assert.equal(r.accepted, false);
  assert.equal(r.reason, 'rejected:version-missing');
  assert.deepEqual(r.entries.map((e) => e.id), ['test-alpha', 'test-beta']);
});

// ---------- 5. 旧缓存纯字符串哈希 ----------

test('旧缓存纯字符串哈希：能读、不崩；内容未变时顺手补上修订号', async () => {
  const body = catalog(4, SKILLS_V4);
  writeCacheFile({ skills: [], experts: [], remoteHashes: { [URL_]: sha(body) } });

  const rec = engine.readRemoteRecord(readCacheFile(), URL_);
  assert.deepEqual(rec, { hash: sha(body), version: null, entries: null });

  stubFetch(body); // 内容未变
  const r = await engine.fetchRemoteSkills(false);
  assert.equal(r.accepted, true);
  assert.equal(r.reason, 'unchanged');
  assert.equal(r.version, 4);
  // 记录已从旧格式升级为带修订号的新格式
  assert.equal(readCacheFile().remoteCatalog[URL_].version, 4);
});

test('旧格式记录 + 内容变化：拒绝（rejected:no-baseline-version）', async () => {
  writeCacheFile({ skills: [], experts: [], remoteHashes: { [URL_]: 'stale-legacy-hash' } });
  stubFetch(catalog(4, SKILLS_V4));
  const r = await engine.fetchRemoteSkills(false);
  assert.equal(r.accepted, false);
  assert.equal(r.reason, 'rejected:no-baseline-version');
});

// ---------- 6. 网络异常时也保留上次接受的目录 ----------

test('HTTP 失败：不退回种子，仍用上次接受的目录', async () => {
  seedRecord(3, SKILLS_V3, { body: catalog(3, SKILLS_V3) });
  stubFetch('boom', { status: 500 });
  const r = await engine.fetchRemoteSkills(false);
  assert.equal(r.accepted, false);
  assert.equal(r.reason, 'http-500');
  assert.deepEqual(r.entries.map((e) => e.id), ['test-alpha', 'test-beta']);
});

// ---------- 7. 端到端：getIndex 拒绝时不退回 SEED ----------

test('端到端：拒绝时 getIndex 用上次接受的目录，而不是内置 SEED', async () => {
  seedRecord(3, SKILLS_V3, { body: catalog(3, SKILLS_V3) });
  stubFetch(catalog(3, SKILLS_V4)); // 内容变、版本没变 → 拒绝

  const cwd = makeCwd('reject');
  try {
    const idx = await engine.getIndex({ cwd });
    const byId = new Map(idx.skills.map((s) => [s.id, s]));

    // 上次接受的目录仍在，且内容是**上一版**的
    // （若退回 SEED，这里根本不会有 test-alpha，且描述会变）
    assert.ok(byId.has('test-alpha'), '上次接受的条目必须保留');
    assert.equal(byId.get('test-alpha').description, 'alpha skill v3');
    assert.ok(byId.has('test-beta'));
    // 被拒绝的新条目不得出现
    assert.ok(!byId.has('test-gamma'), '被拒绝的新目录不得进来');
  } finally {
    rmSync(cwd, { recursive: true, force: true });
  }
});

test('端到端：接受新版本后 getIndex 用新目录', async () => {
  seedRecord(3, SKILLS_V3, { body: catalog(3, SKILLS_V3) });
  stubFetch(catalog(4, SKILLS_V4)); // 版本递增 → 接受

  const cwd = makeCwd('accept');
  try {
    const idx = await engine.getIndex({ cwd });
    const byId = new Map(idx.skills.map((s) => [s.id, s]));
    assert.ok(byId.has('test-gamma'), '新版本条目应当进来');
    assert.equal(byId.get('test-alpha').description, 'alpha skill v4');
    assert.deepEqual(byId.get('test-alpha').tags, ['alpha', 'updated']);
  } finally {
    rmSync(cwd, { recursive: true, force: true });
  }
});
