"""可控交错自测：验证参与页状态读取的快照语义与事务/连接释放。

不依赖运行中的服务：直接以 Flask 测试客户端驱动 app，
数据库使用独立临时文件（导入 app 前通过 DB_PATH 指定），不影响正式数据。

运行：python selftest.py
"""

import logging
import os
import sqlite3
import tempfile
import threading
import unittest

os.environ["DB_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="review-selftest-"), "app.db"
)

import app as app_module  # noqa: E402  必须在设置 DB_PATH 之后导入
from flask import g  # noqa: E402


# ---------------- 测试工具 ----------------


class ConnWrapper:
    """包装 sqlite3 连接：逐语句钩子（用于暂停/注入故障），并记录关闭时的事务状态。"""

    def __init__(self, real):
        self._real = real
        self.hook = None
        self.paused = False
        self.in_transaction_at_close = None

    def execute(self, sql, parameters=()):
        cur = self._real.execute(sql, parameters)
        if self.hook is not None:
            self.hook(self, sql)
        return cur

    def commit(self):
        return self._real.commit()

    def rollback(self):
        return self._real.rollback()

    def close(self):
        self.in_transaction_at_close = self._real.in_transaction
        return self._real.close()


class use_wrapped_db:
    """临时把 app.get_db 换成返回 ConnWrapper 的版本（连接参数与 g 缓存保持一致）。"""

    def __init__(self, hook=None):
        self.hook = hook
        self.wrappers = []
        self._orig = None

    def _get_db(self):
        if "db" not in g:
            conn = sqlite3.connect(app_module.DB_PATH, timeout=10)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            wrapper = ConnWrapper(conn)
            wrapper.hook = self.hook
            self.wrappers.append(wrapper)
            g.db = wrapper
        return g.db

    def __enter__(self):
        self._orig = app_module.get_db
        app_module.get_db = self._get_db
        return self

    def __exit__(self, *exc):
        app_module.get_db = self._orig
        return False


def new_client():
    return app_module.app.test_client()


def create_session(client):
    resp = client.post(
        "/api/sessions",
        json={
            "people": [
                {"name": "甲", "manuscript": "文本一"},
                {"name": "乙", "manuscript": "文本二"},
                {"name": "丙", "manuscript": "文本三"},
            ]
        },
    )
    assert resp.status_code == 200, resp.get_json()
    data = resp.get_json()
    host_token = data["host_url"].rsplit("/", 1)[-1]
    tokens = [p["url"].rsplit("/", 1)[-1] for p in data["people"]]
    return host_token, tokens


def submit_review(client, token, content, version):
    return client.post(
        f"/api/p/{token}/review", json={"content": content, "version": version}
    )


# ---------------- 用例 ----------------


class SnapshotReadTest(unittest.TestCase):
    """核心场景：读取暂停期间另一连接完成 改稿→提交新版意见→公布。"""

    def setUp(self):
        self.client = new_client()

    def test_read_snapshot_consistent_under_concurrent_writes(self):
        host_token, tokens = create_session(self.client)
        # 首轮：三人都对 v1 稿件提交了有效意见
        for tok, content in zip(tokens, ["甲评乙", "乙评丙", "丙评甲"]):
            resp = submit_review(self.client, tok, content, 1)
            self.assertEqual(resp.status_code, 200, resp.get_json())

        read_started = threading.Event()
        writes_done = threading.Event()
        armed = {"on": True}

        def pause_hook(wrapper, sql):
            s = sql.lstrip().upper()
            # 读出本人稿件（第一条 SELECT）后暂停，模拟"读出旧稿后"的交错点
            if armed["on"] and s.startswith("SELECT") and "FROM PARTICIPANTS" in s:
                armed["on"] = False
                wrapper.paused = True
                read_started.set()
                self.assertTrue(writes_done.wait(10), "写入侧未及时完成")

        result = {}

        def reader():
            result["resp"] = new_client().get(f"/api/p/{tokens[0]}")

        with use_wrapped_db(pause_hook) as db:
            t = threading.Thread(target=reader)
            t.start()
            self.assertTrue(read_started.wait(10), "读取未在第一条查询后暂停")

            # 读取暂停期间，另一连接依次完成合法写入（WAL 下不被读事务阻塞）
            resp = self.client.put(
                f"/api/p/{tokens[0]}/manuscript", json={"manuscript": "文本一（改）"}
            )
            self.assertEqual(resp.status_code, 200, resp.get_json())
            self.assertEqual(resp.get_json()["version"], 2)
            resp = submit_review(self.client, tokens[2], "丙评甲（新版）", 2)
            self.assertEqual(resp.status_code, 200, resp.get_json())
            resp = self.client.post(f"/api/h/{host_token}/publish")
            self.assertEqual(resp.status_code, 200, resp.get_json())

            writes_done.set()
            t.join(10)
            self.assertFalse(t.is_alive(), "读取线程未结束")

        reader_conn = next(w for w in db.wrappers if w.paused)

        resp = result["resp"]
        self.assertEqual(resp.status_code, 200, resp.get_json())
        data = resp.get_json()
        # 关键断言：允许完整返回公布前状态；不得拼出
        # published=true + 旧 version + received=null 的混合快照
        self.assertEqual(
            (data["published"], data["version"], data["received"]),
            (False, 1, None),
        )
        self.assertEqual(data["manuscript"], "文本一")
        self.assertEqual(data["assignment"]["manuscript"], "文本二")
        self.assertEqual(data["assignment"]["version"], 1)
        self.assertEqual(data["my_review"]["content"], "甲评乙")
        self.assertTrue(data["my_review"]["valid"])
        # 读取事务已结束、连接已释放
        self.assertIs(reader_conn.in_transaction_at_close, False)

        # 交错结束后再读：完整落在公布后快照，received 为当前稿件的有效意见
        data = self.client.get(f"/api/p/{tokens[0]}").get_json()
        self.assertEqual((data["published"], data["version"]), (True, 2))
        self.assertEqual(data["manuscript"], "文本一（改）")
        self.assertEqual(data["received"], "丙评甲（新版）")
        self.assertTrue(data["my_review"]["valid"])

    def test_journal_mode_is_wal(self):
        conn = sqlite3.connect(app_module.DB_PATH)
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(mode.lower(), "wal")

    def test_success_ends_transaction(self):
        _, tokens = create_session(self.client)
        with use_wrapped_db() as db:
            resp = self.client.get(f"/api/p/{tokens[0]}")
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertEqual(len(db.wrappers), 1)
        self.assertIs(db.wrappers[0].in_transaction_at_close, False)

    def test_invalid_token_ends_transaction(self):
        with use_wrapped_db() as db:
            resp = self.client.get("/api/p/不存在的链接")
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json(), {"error": "个人链接无效"})
        self.assertEqual(len(db.wrappers), 1)
        self.assertIs(db.wrappers[0].in_transaction_at_close, False)

    def test_read_error_ends_transaction(self):
        _, tokens = create_session(self.client)

        def boom(_wrapper, sql):
            if "FROM SESSIONS" in sql.lstrip().upper():
                raise RuntimeError("注入的读取故障")

        logger = logging.getLogger(app_module.app.name)
        old_level = logger.level
        logger.setLevel(logging.CRITICAL)  # 预期的 500，静默堆栈
        try:
            with use_wrapped_db(boom) as db:
                resp = self.client.get(f"/api/p/{tokens[0]}")
        finally:
            logger.setLevel(old_level)
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.get_json(), {"error": "服务器内部错误"})
        self.assertEqual(len(db.wrappers), 1)
        self.assertIs(db.wrappers[0].in_transaction_at_close, False)


class ExistingRulesTest(unittest.TestCase):
    """回归：匿名隔离、改稿失效、过期拒绝、公布门槛与公布冻结等既有规则不变。"""

    def setUp(self):
        self.client = new_client()

    def test_rules_unchanged(self):
        host_token, tokens = create_session(self.client)
        t_jia, t_yi, t_bing = tokens

        # 匿名隔离：响应不含他人姓名与任何链接字段；公布前不含收到的意见
        resp = self.client.get(f"/api/p/{t_jia}")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertNotIn("乙", body)
        self.assertNotIn("丙", body)
        self.assertNotIn("token", body)
        self.assertNotIn("url", body)
        self.assertIsNone(resp.get_json()["received"])

        # 改稿失效：乙改稿后，甲针对 v1 的意见失效
        self.assertEqual(submit_review(self.client, t_jia, "甲评乙", 1).status_code, 200)
        resp = self.client.put(
            f"/api/p/{t_yi}/manuscript", json={"manuscript": "文本二（改）"}
        )
        self.assertEqual(resp.status_code, 200)

        # 过期拒绝：绑定旧版本提交被 409 拒绝，且不覆盖已有意见
        resp = submit_review(self.client, t_jia, "甲评乙（过期）", 1)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.get_json()["current_version"], 2)
        data = self.client.get(f"/api/p/{t_jia}").get_json()
        self.assertEqual(data["my_review"]["content"], "甲评乙")
        self.assertFalse(data["my_review"]["valid"])
        # 绑定新版本后提交成功
        self.assertEqual(
            submit_review(self.client, t_jia, "甲评乙（新版）", 2).status_code, 200
        )

        # 公布门槛：仍有稿件缺有效意见时 409
        resp = self.client.post(f"/api/h/{host_token}/publish")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(set(resp.get_json()["missing"]), {"甲", "丙"})
        self.assertEqual(submit_review(self.client, t_yi, "乙评丙", 1).status_code, 200)
        self.assertEqual(submit_review(self.client, t_bing, "丙评甲", 1).status_code, 200)
        resp = self.client.post(f"/api/h/{host_token}/publish")
        self.assertEqual(resp.status_code, 200)

        # 公布冻结：重复公布、改稿、提交意见均 409
        self.assertEqual(
            self.client.post(f"/api/h/{host_token}/publish").status_code, 409
        )
        self.assertEqual(
            self.client.put(
                f"/api/p/{t_jia}/manuscript", json={"manuscript": "冻结后改稿"}
            ).status_code,
            409,
        )
        self.assertEqual(submit_review(self.client, t_jia, "冻结后提交", 2).status_code, 409)

        # 公布后：作者看到针对当前版本的有效意见
        data = self.client.get(f"/api/p/{t_jia}").get_json()
        self.assertTrue(data["published"])
        self.assertEqual(data["received"], "丙评甲")

        # 无效链接 404
        self.assertEqual(self.client.get("/api/p/无效").status_code, 404)
        self.assertEqual(self.client.get("/api/h/无效").status_code, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
