#!/usr/bin/env python3
"""skill-matcher 远程目录「版本门控」测试（Python 侧）。

覆盖：
  1. 首次拉取会记下哈希和 version
  2. version 3→4 且内容变化：接受，并保留远程 tags 与条目自身的 origin
  3. version 不变但内容变化：拒绝，且仍用上一份，而不是种子
  4. version 变小 / 缺失 / 非正整数：拒绝
  5. 旧格式（纯字符串哈希）记录：能读、不崩；内容变了则拒绝
  6. Python 路径不再丢 tags，也不再覆盖条目 origin
  7. 导出目录的修订号：内容变才 +1，且不低于已发布版本

运行：
  python3 -m unittest discover -s tests -v
  python3 tests/test_sync_index.py
"""
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "bin"))

import sync_index as S  # noqa: E402

URL = "https://example.invalid/skill-matcher-index/main/index.json"

SKILLS_V3 = [
    {"id": "test-alpha", "name": "Test Alpha", "description": "alpha skill v3",
     "install": "git clone x", "origin": "acme/tools", "tags": ["alpha"]},
    {"id": "test-beta", "name": "Test Beta", "description": "beta skill",
     "install": "git clone y", "origin": "acme/tools", "tags": ["beta"]},
]
SKILLS_V4 = [
    {"id": "test-alpha", "name": "Test Alpha", "description": "alpha skill v4",
     "install": "git clone x", "origin": "acme/tools", "tags": ["alpha", "updated"]},
    {"id": "test-beta", "name": "Test Beta", "description": "beta skill",
     "install": "git clone y", "origin": "acme/tools", "tags": ["beta"]},
    {"id": "test-gamma", "name": "Test Gamma", "description": "gamma skill",
     "install": "git clone z", "origin": "other/repo", "tags": ["Gamma", "  ", 42, None]},
]


def catalog(version, skills):
    """构造远程目录字节流。version 传 None 表示目录里根本没有 version 字段。"""
    obj = {"name": "skill-matcher 开源技能目录", "skills": skills}
    if version is not None:
        obj["version"] = version
    return json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")


class RemoteGateTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.out = Path(self._tmp.name)
        self._orig = (S.OUT_DIR, S.HASH_FILE)
        S.OUT_DIR = self.out
        S.HASH_FILE = self.out / "_remote_hashes.json"
        (self.out / "_sources.json").write_text(json.dumps(
            {"remote_indexes": [{"name": "测试源", "url": URL, "install_hint": "见 install"}]},
            ensure_ascii=False), encoding="utf-8")

    def tearDown(self):
        S.OUT_DIR, S.HASH_FILE = self._orig
        self._tmp.cleanup()

    # ---------- helpers ----------
    def _fetch(self, body):
        return S.fetch_remote_skills(offline=False, opener=lambda url: body)

    def _seed(self, version, skills, body=None, entries=None):
        body = catalog(version, skills) if body is None else body
        S.write_json(S.HASH_FILE, {URL: {
            "hash": hashlib.sha256(body).hexdigest(),
            "version": version,
            "accepted_at": "2026-01-01 00:00:00",
            "entries": entries if entries is not None else
                       S.normalize_remote_entries({"skills": skills}, "测试源", "见 install"),
        }})
        return body

    def _record(self):
        return json.loads(S.HASH_FILE.read_text(encoding="utf-8"))[URL]

    def _export(self, skills=None):
        return json.loads((self.out / "opensource-index.json").read_text(encoding="utf-8"))

    # ---------- 1. 裁决矩阵 ----------
    def test_decision_matrix(self):
        rec = {"hash": "h3", "version": 3, "entries": []}
        cases = [
            (("h9", 4, None), True, "first-fetch"),
            (("h3", 3, rec), True, "unchanged"),
            (("h9", 4, rec), True, "version-bumped"),
            (("h9", 3, rec), False, "rejected:version-not-increased"),
            (("h9", 2, rec), False, "rejected:version-not-increased"),
            (("h9", None, rec), False, "rejected:version-missing"),
            (("h9", 0, rec), False, "rejected:version-missing"),
            (("h9", 2.5, rec), False, "rejected:version-missing"),
            (("h9", "4", rec), False, "rejected:version-missing"),
            (("h9", True, rec), False, "rejected:version-missing"),
            (("h9", 4, {"hash": "h3", "version": None, "entries": []}), False,
             "rejected:no-baseline-version"),
        ]
        for (digest, version, record), want_accept, want_reason in cases:
            ok, reason, _ = S.decide_remote_update(digest, version, record)
            self.assertEqual((ok, reason), (want_accept, want_reason), msg=f"{digest}/{version}")

    # ---------- 2. 首次拉取 ----------
    def test_first_fetch_records_hash_and_version(self):
        body = catalog(4, SKILLS_V4)
        items, status = self._fetch(body)
        self.assertEqual(status[URL]["reason"], "first-fetch")
        self.assertEqual(status[URL]["version"], 4)
        self.assertEqual([i["id"] for i in items], ["test-alpha", "test-beta", "test-gamma"])
        rec = self._record()
        self.assertEqual(rec["hash"], hashlib.sha256(body).hexdigest())
        self.assertEqual(rec["version"], 4)
        self.assertEqual(len(rec["entries"]), 3)

    # ---------- 3. version 3 → 4 且内容变化 ----------
    def test_version_bump_accepts_and_keeps_tags_and_origin(self):
        self._seed(3, SKILLS_V3)
        items, status = self._fetch(catalog(4, SKILLS_V4))
        self.assertEqual(status[URL]["reason"], "version-bumped")
        self.assertEqual(status[URL]["version"], 4)

        by_id = {i["id"]: i for i in items}
        # tags 是字符串数组，标签原值保留，非字符串/空白项被剔除
        self.assertEqual(by_id["test-alpha"]["tags"], ["alpha", "updated"])
        self.assertEqual(by_id["test-gamma"]["tags"], ["Gamma"])
        self.assertTrue(all(isinstance(t, str) for t in by_id["test-gamma"]["tags"]))
        # origin 用条目自身的，不被远程源名覆盖
        self.assertEqual(by_id["test-gamma"]["origin"], "other/repo")
        self.assertEqual(by_id["test-alpha"]["origin"], "acme/tools")
        # 记录推进到 v4
        self.assertEqual(self._record()["version"], 4)
        self.assertEqual(self._record()["hash"], hashlib.sha256(catalog(4, SKILLS_V4)).hexdigest())

    # ---------- 4. version 不变但内容变化 → 拒绝且保留上一份 ----------
    def test_same_version_changed_content_rejected_keeps_previous(self):
        self._seed(3, SKILLS_V3)
        old_hash = self._record()["hash"]
        items, status = self._fetch(catalog(3, SKILLS_V4))  # 内容变了，version 仍是 3
        self.assertEqual(status[URL]["reason"], "rejected:version-not-increased")
        # 返回的是上次接受的条目，不是空列表
        self.assertEqual([i["id"] for i in items], ["test-alpha", "test-beta"])
        self.assertEqual(self._record()["hash"], old_hash)   # 记录未被改写
        self.assertEqual(self._record()["version"], 3)

    def test_version_lower_rejected(self):
        self._seed(3, SKILLS_V3)
        items, status = self._fetch(catalog(2, SKILLS_V4))
        self.assertEqual(status[URL]["reason"], "rejected:version-not-increased")
        self.assertEqual([i["id"] for i in items], ["test-alpha", "test-beta"])

    def test_version_missing_rejected(self):
        self._seed(3, SKILLS_V3)
        items, status = self._fetch(catalog(None, SKILLS_V4))
        self.assertEqual(status[URL]["reason"], "rejected:version-missing")
        self.assertEqual([i["id"] for i in items], ["test-alpha", "test-beta"])

    # ---------- 5. 旧格式记录 ----------
    def test_legacy_string_hash_readable(self):
        body = catalog(4, SKILLS_V4)
        digest = hashlib.sha256(body).hexdigest()
        S.write_json(S.HASH_FILE, {URL: digest})            # 旧格式：纯字符串
        self.assertEqual(S.read_remote_record(URL),
                         {"hash": digest, "version": None, "entries": None})
        # 内容未变：接受，并顺手补上修订号
        items, status = self._fetch(body)
        self.assertEqual(status[URL]["reason"], "unchanged")
        self.assertEqual(status[URL]["version"], 4)
        self.assertEqual(self._record()["version"], 4)
        self.assertEqual(len(items), 3)

    def test_legacy_record_content_change_rejected(self):
        S.write_json(S.HASH_FILE, {URL: "stale-legacy-hash"})
        items, status = self._fetch(catalog(4, SKILLS_V4))
        self.assertEqual(status[URL]["reason"], "rejected:no-baseline-version")
        self.assertEqual(items, [])   # 旧记录无快照，且本次尚无已导出目录

    def test_legacy_record_recovers_previous_catalog_from_export(self):
        S.write_json(self.out / "opensource-index.json",
                     {"skills": [{"id": "keep-me", "source": "opensource"},
                                 {"id": "not-os", "source": "local"}]})
        S.write_json(S.HASH_FILE, {URL: "stale-legacy-hash"})
        items, status = self._fetch(catalog(4, SKILLS_V4))
        self.assertEqual(status[URL]["reason"], "rejected:no-baseline-version")
        # 用已导出的开源目录兜底，而不是退回内置种子
        self.assertEqual([i["id"] for i in items], ["keep-me"])

    # ---------- 6. 归一化 ----------
    def test_normalize_keeps_tags_and_entry_origin(self):
        entries = S.normalize_remote_entries({"skills": SKILLS_V4}, "测试源", "见 install")
        by_id = {e["id"]: e for e in entries}
        self.assertEqual(by_id["test-gamma"]["origin"], "other/repo")
        self.assertEqual(by_id["test-gamma"]["tags"], ["Gamma"])
        self.assertEqual(by_id["test-alpha"]["install"], "git clone x")
        self.assertEqual(by_id["test-alpha"]["source"], "opensource")
        # 条目没有 install 时回退到源提示
        one = S.normalize_remote_entries({"skills": [{"id": "noinst"}]}, "测试源", "见 install")
        self.assertEqual(one[0]["install"], "见 install")
        self.assertEqual(one[0]["origin"], "测试源")

    # ---------- 7. 导出修订号 ----------
    def test_export_bumps_version_only_when_content_changes(self):
        skills = [{"id": "a", "source": "opensource"}, {"id": "b", "source": "local"}]
        S.export_open_source(skills, floor_version=0)
        v1 = self._export()["version"]
        S.export_open_source(skills, floor_version=0)          # 内容未变
        self.assertEqual(self._export()["version"], v1)
        changed = [{"id": "a", "source": "opensource"}, {"id": "c", "source": "opensource"}]
        S.export_open_source(changed, floor_version=0)         # 内容变了
        self.assertEqual(self._export()["version"], v1 + 1)
        # 只有 opensource 条目进目录
        self.assertEqual([s["id"] for s in self._export()["skills"]], ["a", "c"])

    def test_export_version_never_below_published_floor(self):
        S.export_open_source([{"id": "a", "source": "opensource"}], floor_version=7)
        self.assertGreaterEqual(self._export()["version"], 8)
        S.export_open_source([{"id": "a", "source": "opensource"}], floor_version=7)  # 幂等
        self.assertGreaterEqual(self._export()["version"], 8)

    def test_published_floor_picks_max_version(self):
        status = {
            "a": {"reason": "version-bumped", "version": 4, "hash": "x"},
            "b": {"reason": "rejected:version-not-increased", "version": 9, "hash": "y"},
            "c": {"reason": "network-error", "version": None, "hash": None},
        }
        self.assertEqual(S._published_floor(status), 9)
        self.assertEqual(S._published_floor({}), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
