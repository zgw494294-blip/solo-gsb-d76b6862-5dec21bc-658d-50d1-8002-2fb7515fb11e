"""GET /api/p/<token> 快照一致性自测（标准库 unittest + 线程可控交错）。

复现路径：读连接已取到本人旧稿（v1）但尚未读公布状态/意见时暂停；
另一连接依次完成 改稿(v2) -> 对 v2 提交新意见 -> 公布；读连接继续。

修复前会拼出 published=true、旧 version=1、received=null（页面显示“（无）”）；
修复后读连接返回的是读事务开始时的同一快照（公布前完整状态，received 允许为 null）。

直接运行：python3 tests/test_snapshot.py
"""

import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 在导入 app 前指定独立临时库，避免污染开发数据
_tmp = tempfile.mkdtemp(prefix="peer-review-test-")
os.environ["DB_PATH"] = os.path.join(_tmp, "test.db")
os.environ.setdefault("PORT", "8011")

import app as app_module  # noqa: E402


class TrackingConnection(sqlite3.Connection):
    """记录关闭时是否仍有未结束事务，用于校验“结束事务、释放连接”。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.closed_by_teardown = False
        self.in_transaction_at_close = None

    def close(self):
        self.closed_by_teardown = True
        self.in_transaction_at_close = self.in_transaction
        super().close()


_real_connect = sqlite3.connect
_opened_connections = []


def tracking_connect(database, *args, **kwargs):
    conn = _real_connect(database, *args, factory=TrackingConnection, **kwargs)
    _opened_connections.append(conn)
    return conn


def create_session(client):
    # 稿件正文避开姓名用字，便于断言“不返回他人姓名”
    people = [
        {"name": "甲", "manuscript": "苹果初稿"},
        {"name": "乙", "manuscript": "香蕉初稿"},
        {"name": "丙", "manuscript": "橙子初稿"},
    ]
    rv = client.post("/api/sessions", json={"people": people})
    assert rv.status_code == 200, rv.get_data(as_text=True)
    data = rv.get_json()
    host_token = data["host_url"].rsplit("/", 1)[-1]
    tokens = [u["url"].rsplit("/", 1)[-1] for u in data["people"]]
    return host_token, tokens


def submit_review(client, token, content, version):
    rv = client.post(
        f"/api/p/{token}/review",
        json={"content": content, "version": version},
    )
    assert rv.status_code == 200, rv.get_data(as_text=True)


def assert_snapshot_invariant(test, s, where):
    """一次响应内：公布状态、本人稿件版本、分配稿件、意见必须互相同一快照。

    公布会话的当前稿件必有有效意见（公布门槛保证），因此 published 时
    received 绝不能为 None；否则就是“旧版本 + 新公布标志”的拼凑响应。
    """
    test.assertIsInstance(s["version"], int, where)
    test.assertIsInstance(s["assignment"]["version"], int, where)
    if s["published"]:
        test.assertIsNotNone(
            s["received"],
            f"{where}: published=true 但 received=null（跨快照拼凑），完整响应={s}",
        )


class ParticipantSnapshotTests(unittest.TestCase):
    def setUp(self):
        _opened_connections.clear()
        app_module.app.config["TESTING"] = True
        self.client = app_module.app.test_client()
        self.host, (self.a, self.b, self.c) = create_session(self.client)

    def tearDown(self):
        # 每个请求的连接都应被 teardown 释放
        for conn in _opened_connections:
            self.assertTrue(
                conn.closed_by_teardown,
                "存在未释放的数据库连接",
            )
            self.assertFalse(
                conn.in_transaction_at_close,
                "连接关闭时仍有未结束的事务（未 commit/rollback）",
            )

    # ---- 常规路径 ----

    def test_wal_mode(self):
        conn = sqlite3.connect(os.environ["DB_PATH"])
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(mode.lower(), "wal")

    def test_prepublish_isolation(self):
        """公布前：不泄露收到的意见；无他人姓名/链接。"""
        submit_review(self.client, self.c, "丙对甲 v1 的意见", 1)
        rv = self.client.get(f"/api/p/{self.a}")
        s = rv.get_json()
        self.assertEqual(rv.status_code, 200)
        self.assertFalse(s["published"])
        self.assertIsNone(s["received"])
        raw = json.dumps(s, ensure_ascii=False)
        self.assertNotIn("丙对甲", raw)
        self.assertNotIn("乙", raw)
        self.assertNotIn(self.b, raw)
        self.assertNotIn(self.c, raw)

    def test_published_state_is_consistent(self):
        """顺序流程公布后：每人收到针对当前稿件的有效意见，响应自洽。"""
        submit_review(self.client, self.a, "甲对乙 v1 的意见", 1)
        submit_review(self.client, self.b, "乙对丙 v1 的意见", 1)
        submit_review(self.client, self.c, "丙对甲 v1 的意见", 1)
        rv = self.client.post(f"/api/h/{self.host}/publish")
        self.assertEqual(rv.status_code, 200, rv.get_data(as_text=True))

        for token, expect in [
            (self.a, "丙对甲 v1 的意见"),
            (self.b, "甲对乙 v1 的意见"),
            (self.c, "乙对丙 v1 的意见"),
        ]:
            s = self.client.get(f"/api/p/{token}").get_json()
            self.assertTrue(s["published"])
            self.assertEqual(s["received"], expect)
            self.assertTrue(s["my_review"]["valid"])
            assert_snapshot_invariant(self, s, "顺序公布后")

        # 公布后冻结：改稿/提交均 409，字段与状态码不变
        rv = self.client.put(
            f"/api/p/{self.a}/manuscript", json={"manuscript": "甲想改"}
        )
        self.assertEqual(rv.status_code, 409)
        rv = self.client.post(
            f"/api/p/{self.a}/review", json={"content": "x", "version": 1}
        )
        self.assertEqual(rv.status_code, 409)

    def test_invalid_link_404_and_connection_released(self):
        rv = self.client.get("/api/p/not-a-real-token")
        self.assertEqual(rv.status_code, 404)
        self.assertIn("个人链接无效", rv.get_json()["error"])

    def test_read_exception_rolls_back(self):
        """读取中途异常：仍须回滚（结束事务）并由 teardown 释放连接，返回 500。"""
        original = app_module.get_target

        def boom(*_a, **_kw):
            raise RuntimeError("simulated read failure")

        app_module.get_target = boom
        try:
            rv = self.client.get(f"/api/p/{self.a}")
        finally:
            app_module.get_target = original
        self.assertEqual(rv.status_code, 500)

    # ---- 核心：可控交错 ----

    def test_interleaved_edit_review_publish_keeps_one_snapshot(self):
        # 初始：甲评乙 v1（读者本人已交意见），乙评丙 v1
        submit_review(self.client, self.a, "甲对乙 v1 的意见", 1)
        submit_review(self.client, self.b, "乙对丙 v1 的意见", 1)

        reader_ready = threading.Event()
        release_reader = threading.Event()
        reader_thread = None

        # 在读连接拿到本人旧稿之后、继续读其余状态之前挂起
        original_find = app_module.find_participant

        def gated_find(conn, token):
            row = original_find(conn, token)
            if row is not None and threading.current_thread() is reader_thread:
                reader_ready.set()
                self.assertTrue(release_reader.wait(timeout=10), "读线程未被放行")
            return row

        reader_result = {}

        def reader_job():
            rv = self.client.get(f"/api/p/{self.a}")
            reader_result["status"] = rv.status_code
            reader_result["json"] = rv.get_json()

        app_module.find_participant = gated_find
        try:
            reader_thread = threading.Thread(target=reader_job, daemon=True)
            reader_thread.start()
            self.assertTrue(reader_ready.wait(timeout=10), "读线程未到达挂起点")

            # 读暂停期间，另一连接必须能立刻完成合法写入（WAL：读不阻塞写）
            # 1) 甲（另一连接/标签页）改稿 v1 -> v2，针对 v1 的意见失效
            t0 = time.monotonic()
            rv = self.client.put(
                f"/api/p/{self.a}/manuscript",
                json={"manuscript": "苹果新稿 v2"},
            )
            edit_elapsed = time.monotonic() - t0
            self.assertEqual(rv.status_code, 200, rv.get_data(as_text=True))
            self.assertEqual(rv.get_json()["version"], 2)
            self.assertLess(
                edit_elapsed,
                3.0,
                "读暂停期间写改稿被阻塞超过 busy_timeout，说明读操作误拿了写锁",
            )

            # 2) 丙对当前 v2 提交新意见（旧 v1 意见已失效）
            submit_review(self.client, self.c, "丙对甲 v2 的意见", 2)

            # 3) 三篇当前稿均有有效意见：乙 v1<-甲、丙 v1<-乙、甲 v2<-丙
            rv = self.client.post(f"/api/h/{self.host}/publish")
            self.assertEqual(rv.status_code, 200, rv.get_data(as_text=True))

            # 放行暂停中的读连接
            release_reader.set()
            reader_thread.join(timeout=10)
        finally:
            app_module.find_participant = original_find
            release_reader.set()
            reader_thread.join(timeout=5)

        self.assertFalse(reader_thread.is_alive(), "读线程卡住")
        self.assertEqual(reader_result["status"], 200)
        snap = reader_result["json"]

        # 关键断言：响应必须是同一快照。
        # 修复前：published=true + version=1 + received=null（页面显示“（无）”）
        # 修复后：读事务开始于改稿之前，返回完整的公布前快照
        if not snap["published"]:
            self.assertEqual(snap["version"], 1)
            self.assertEqual(snap["manuscript"], "苹果初稿")
            self.assertIsNone(snap["received"])
            self.assertEqual(snap["assignment"]["version"], 1)
            self.assertTrue(snap["my_review"]["valid"])
        assert_snapshot_invariant(self, snap, "交错读响应")

        # 交错结束后的新读：必须看到公布后的自洽新快照
        fresh = self.client.get(f"/api/p/{self.a}").get_json()
        self.assertTrue(fresh["published"])
        self.assertEqual(fresh["version"], 2)
        self.assertEqual(fresh["manuscript"], "苹果新稿 v2")
        self.assertEqual(fresh["received"], "丙对甲 v2 的意见")
        self.assertEqual(fresh["assignment"]["version"], 1)  # 乙未改稿
        self.assertTrue(fresh["my_review"]["valid"])
        assert_snapshot_invariant(self, fresh, "交错后新读")

    def test_interleaved_target_edit_keeps_assignment_consistent(self):
        """读取暂停期间被分配稿件（乙）改稿：assignment 与 my_review 必须同快照。

        修复前可能拼出 assignment v2 + my_review（仍对 v1 判 valid=true）。
        """
        submit_review(self.client, self.a, "甲对乙 v1 的意见", 1)

        reader_ready = threading.Event()
        release_reader = threading.Event()
        reader_thread = None
        original_find = app_module.find_participant

        def gated_find(conn, token):
            row = original_find(conn, token)
            if row is not None and threading.current_thread() is reader_thread:
                reader_ready.set()
                self.assertTrue(release_reader.wait(timeout=10), "读线程未被放行")
            return row

        reader_result = {}

        def reader_job():
            rv = self.client.get(f"/api/p/{self.a}")
            reader_result["status"] = rv.status_code
            reader_result["json"] = rv.get_json()

        app_module.find_participant = gated_find
        try:
            reader_thread = threading.Thread(target=reader_job, daemon=True)
            reader_thread.start()
            self.assertTrue(reader_ready.wait(timeout=10), "读线程未到达挂起点")

            # 乙改稿 v1 -> v2（甲的意见在同一写事务里失效）
            rv = self.client.put(
                f"/api/p/{self.b}/manuscript",
                json={"manuscript": "香蕉新稿 v2"},
            )
            self.assertEqual(rv.status_code, 200, rv.get_data(as_text=True))

            release_reader.set()
            reader_thread.join(timeout=10)
        finally:
            app_module.find_participant = original_find
            release_reader.set()
            reader_thread.join(timeout=5)

        snap = reader_result["json"]
        self.assertEqual(reader_result["status"], 200)
        # 只能是两种自洽快照之一：
        if snap["assignment"]["version"] == 1:
            self.assertEqual(snap["assignment"]["manuscript"], "香蕉初稿")
            self.assertTrue(snap["my_review"]["valid"])
            self.assertEqual(snap["my_review"]["version"], 1)
        else:
            self.assertEqual(snap["assignment"]["version"], 2)
            self.assertEqual(snap["assignment"]["manuscript"], "香蕉新稿 v2")
            self.assertFalse(snap["my_review"]["valid"])
            self.assertEqual(snap["my_review"]["version"], 1)

    def test_concurrent_stress_mixed_snapshot_is_valid(self):
        """多线程混合改稿/提交/读取：每个已公布响应必须自洽，写不被读阻塞。"""
        import random

        rng = random.Random(20261005)
        errors = []
        stop = threading.Event()

        # 先补齐另外两条意见（v1 下全部有效，便于 mutator 循环失效/重交）
        submit_review(self.client, self.b, "乙对丙 v1 的意见", 1)
        submit_review(self.client, self.c, "丙对甲 v1 的意见", 1)

        def reader(token):
            while not stop.is_set():
                rv = self.client.get(f"/api/p/{token}")
                if rv.status_code != 200:
                    errors.append(f"reader {token}: {rv.status_code}")
                    return
                s = rv.get_json()
                try:
                    assert_snapshot_invariant(self, s, f"压力读 {token}")
                except AssertionError as e:
                    errors.append(str(e))
                    return
                if s["published"]:
                    return

        def mutator(token):
            # 既是作者（改自己的稿），又是评审（读分配稿并按最新版本交意见）
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline and not stop.is_set():
                if self.client.put(
                    f"/api/p/{token}/manuscript",
                    json={"manuscript": f"稿 {rng.randrange(10 ** 6)}"},
                ).status_code not in (200, 409):
                    errors.append(f"edit {token} 非预期状态")
                    return
                time.sleep(0.002)
                state = self.client.get(f"/api/p/{token}").get_json()
                rv = self.client.post(
                    f"/api/p/{token}/review",
                    json={
                        "content": f"意见 {rng.randrange(10 ** 6)}",
                        "version": state["assignment"]["version"],
                    },
                )
                if rv.status_code not in (200, 409):
                    errors.append(f"review {token}: {rv.status_code}")
                    return
                time.sleep(0.002)

        tokens = [self.a, self.b, self.c]
        threads = [threading.Thread(target=reader, args=(t,)) for t in tokens]
        threads += [threading.Thread(target=mutator, args=(t,)) for t in tokens]
        for t in threads:
            t.start()
        for t in threads[3:]:
            t.join(timeout=30)
        stop.set()
        for t in threads[:3]:
            t.join(timeout=10)
        self.assertEqual(errors, [])

        # 全部写入停止后：按当前版本补齐仍失效的意见，再公布（原门槛规则不变）
        for _ in range(10):
            missing = False
            for token in tokens:
                state = self.client.get(f"/api/p/{token}").get_json()
                if not state["my_review"] or not state["my_review"]["valid"]:
                    missing = True
                    rv = self.client.post(
                        f"/api/p/{token}/review",
                        json={
                            "content": "补齐意见",
                            "version": state["assignment"]["version"],
                        },
                    )
                    self.assertIn(rv.status_code, (200, 409))
            if not missing:
                break
        rv = self.client.post(f"/api/h/{self.host}/publish")
        self.assertEqual(rv.status_code, 200, rv.get_data(as_text=True))
        for token in tokens:
            s = self.client.get(f"/api/p/{token}").get_json()
            assert_snapshot_invariant(self, s, f"终态 {token}")
            self.assertIsNotNone(s["received"])


if __name__ == "__main__":
    # 让 get_db() 走带追踪的 connect（测试期间）
    app_module.sqlite3.connect = tracking_connect
    unittest.main(verbosity=2)
