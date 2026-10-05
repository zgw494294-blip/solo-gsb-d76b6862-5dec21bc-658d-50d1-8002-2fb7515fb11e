"""稿件匿名互评：Flask + SQLite + 原生 JavaScript。

规则要点：
- 主持人录入 3~8 人姓名与纯文本稿件，名单固定，按录入顺序循环互评（每人评下一人，末位评首位）。
- 主持链接与个人链接均为随机私密链接，无需注册。
- 参与者只能改自己的稿、读被分配的稿（不返回他人姓名/链接）、提交意见。
- 改稿立即使针对它的已交意见失效；提交绑定所读稿件版本，过期提交拒绝且不覆盖有效意见。
- 每篇当前稿都有有效意见时才可一次公布；公布后作者可见匿名意见，稿件与意见锁定。
- 所有写操作在 BEGIN IMMEDIATE 事务内"先检查后变更"，拒绝即整体回滚，不留部分变更。
"""

import os
import secrets
import sqlite3

from flask import Flask, g, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "data", "app.db"))
PORT = int(os.environ.get("PORT", "8000"))

MIN_PEOPLE = 3
MAX_PEOPLE = 8
MAX_NAME_LEN = 50
MAX_TEXT_LEN = 100000

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    host_token TEXT NOT NULL UNIQUE,
    published INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS participants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    token TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    position INTEGER NOT NULL,
    manuscript TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    UNIQUE (session_id, position)
);
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    reviewer_id INTEGER NOT NULL REFERENCES participants(id),
    target_id INTEGER NOT NULL REFERENCES participants(id),
    target_version INTEGER NOT NULL,
    content TEXT NOT NULL,
    valid INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (session_id, reviewer_id)
);
"""

app = Flask(__name__)


def init_db():
    """建库建表（幂等），数据落盘在 DB_PATH，重启后保留。"""
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


init_db()


def get_db():
    if "db" not in g:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        g.db = conn
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


@app.errorhandler(Exception)
def handle_exception(e):
    if isinstance(e, HTTPException):
        return e
    app.logger.exception("unhandled error")
    return jsonify({"error": "服务器内部错误"}), 500


def err(message, status=400, **extra):
    payload = {"error": message}
    payload.update(extra)
    return jsonify(payload), status


def get_target(conn, session_id, position):
    """循环分配：position 处的人评 (position+1) % n 处的人，末位评首位。"""
    n = conn.execute(
        "SELECT COUNT(*) AS c FROM participants WHERE session_id=?", (session_id,)
    ).fetchone()["c"]
    next_pos = (position + 1) % n
    return conn.execute(
        "SELECT * FROM participants WHERE session_id=? AND position=?",
        (session_id, next_pos),
    ).fetchone()


def find_session_by_host(conn, host_token):
    return conn.execute(
        "SELECT * FROM sessions WHERE host_token=?", (host_token,)
    ).fetchone()


def find_participant(conn, token):
    return conn.execute(
        "SELECT * FROM participants WHERE token=?", (token,)
    ).fetchone()


def find_review_by_reviewer(conn, session_id, reviewer_id):
    return conn.execute(
        "SELECT * FROM reviews WHERE session_id=? AND reviewer_id=?",
        (session_id, reviewer_id),
    ).fetchone()


def review_is_current(review_row, target_row):
    return (
        review_row is not None
        and review_row["valid"]
        and review_row["target_version"] == target_row["version"]
    )


# ---------------- 页面 ----------------

@app.get("/")
def index_page():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/h/<token>")
def host_page(token):
    return send_from_directory(app.static_folder, "host.html")


@app.get("/p/<token>")
def participant_page(token):
    return send_from_directory(app.static_folder, "participant.html")


# ---------------- 创建会话 ----------------

@app.post("/api/sessions")
def create_session():
    data = request.get_json(silent=True) or {}
    people = data.get("people")
    if not isinstance(people, list) or not (MIN_PEOPLE <= len(people) <= MAX_PEOPLE):
        return err(f"需要 {MIN_PEOPLE}～{MAX_PEOPLE} 人")

    cleaned, names = [], []
    for p in people:
        if not isinstance(p, dict):
            return err("数据格式不正确")
        name = p.get("name")
        text = p.get("manuscript")
        if not isinstance(name, str) or not isinstance(text, str):
            return err("姓名与稿件必须是字符串")
        name = name.strip()
        if not name or len(name) > MAX_NAME_LEN:
            return err(f"姓名不能为空且不超过 {MAX_NAME_LEN} 字")
        if not text.strip() or len(text) > MAX_TEXT_LEN:
            return err(f"「{name}」的稿件不能为空且不超过 {MAX_TEXT_LEN} 字")
        cleaned.append((name, text))
        names.append(name)
    if len(set(names)) != len(names):
        return err("姓名不能重复")

    host_token = secrets.token_urlsafe(24)
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute("INSERT INTO sessions (host_token) VALUES (?)", (host_token,))
        session_id = cur.lastrowid
        links = []
        for i, (name, text) in enumerate(cleaned):
            token = secrets.token_urlsafe(24)
            conn.execute(
                "INSERT INTO participants (session_id, token, name, position, manuscript)"
                " VALUES (?,?,?,?,?)",
                (session_id, token, name, i, text),
            )
            links.append({"name": name, "url": f"/p/{token}"})
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return jsonify({"host_url": f"/h/{host_token}", "people": links})


# ---------------- 主持端 ----------------

@app.get("/api/h/<host_token>")
def host_status(host_token):
    conn = get_db()
    session = find_session_by_host(conn, host_token)
    if session is None:
        return err("主持链接无效", 404)
    parts = conn.execute(
        "SELECT * FROM participants WHERE session_id=? ORDER BY position",
        (session["id"],),
    ).fetchall()
    reviewers = []
    can_publish = not session["published"]
    for p in parts:
        target = get_target(conn, session["id"], p["position"])
        review = find_review_by_reviewer(conn, session["id"], p["id"])
        submitted = review_is_current(review, target)
        if not submitted:
            can_publish = False
        reviewers.append({"name": p["name"], "submitted": submitted})
    return jsonify(
        {
            "published": bool(session["published"]),
            "can_publish": can_publish,
            "reviewers": reviewers,
            "links": [
                {"name": p["name"], "url": f"/p/{p['token']}"} for p in parts
            ],
        }
    )


@app.post("/api/h/<host_token>/publish")
def publish(host_token):
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        session = find_session_by_host(conn, host_token)
        if session is None:
            conn.rollback()
            return err("主持链接无效", 404)
        if session["published"]:
            conn.rollback()
            return err("已经公布过了", 409)
        parts = conn.execute(
            "SELECT * FROM participants WHERE session_id=? ORDER BY position",
            (session["id"],),
        ).fetchall()
        missing = []
        for p in parts:
            review = conn.execute(
                "SELECT 1 FROM reviews WHERE target_id=? AND valid=1 AND target_version=?",
                (p["id"], p["version"]),
            ).fetchone()
            if review is None:
                missing.append(p["name"])
        if missing:
            conn.rollback()
            return err("仍有稿件缺少针对当前版本的有效意见，不能公布", 409, missing=missing)
        conn.execute("UPDATE sessions SET published=1 WHERE id=?", (session["id"],))
        conn.commit()
        return jsonify({"ok": True})
    except Exception:
        conn.rollback()
        raise


# ---------------- 参与端 ----------------

@app.get("/api/p/<token>")
def participant_state(token):
    """参与页数据：不含他人姓名与任何私密链接；公布前不含收到的意见。"""
    conn = get_db()
    me = find_participant(conn, token)
    if me is None:
        return err("个人链接无效", 404)
    session = conn.execute(
        "SELECT * FROM sessions WHERE id=?", (me["session_id"],)
    ).fetchone()
    published = bool(session["published"])
    target = get_target(conn, me["session_id"], me["position"])
    my_review = find_review_by_reviewer(conn, me["session_id"], me["id"])

    resp = {
        "name": me["name"],
        "manuscript": me["manuscript"],
        "version": me["version"],
        "published": published,
        # 匿名：不返回被评稿件作者的姓名
        "assignment": {
            "manuscript": target["manuscript"],
            "version": target["version"],
        },
        "my_review": None,
        "received": None,
    }
    if my_review is not None:
        resp["my_review"] = {
            "content": my_review["content"],
            "version": my_review["target_version"],
            "valid": review_is_current(my_review, target),
        }
    if published:
        # 匿名：只返回意见内容，不返回评审人任何信息
        row = conn.execute(
            "SELECT content FROM reviews WHERE target_id=? AND valid=1 AND target_version=?",
            (me["id"], me["version"]),
        ).fetchone()
        resp["received"] = row["content"] if row else None
    return jsonify(resp)


@app.put("/api/p/<token>/manuscript")
def edit_manuscript(token):
    data = request.get_json(silent=True) or {}
    text = data.get("manuscript")
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_LEN:
        return err(f"稿件不能为空且不超过 {MAX_TEXT_LEN} 字")

    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        me = find_participant(conn, token)
        if me is None:
            conn.rollback()
            return err("个人链接无效", 404)
        session = conn.execute(
            "SELECT published FROM sessions WHERE id=?", (me["session_id"],)
        ).fetchone()
        if session["published"]:
            conn.rollback()
            return err("已公布，稿件不可再修改", 409)
        # 同一事务内：改稿 + 版本号 +1 + 使针对该稿的已交意见失效（其他意见保留）
        conn.execute(
            "UPDATE participants SET manuscript=?, version=version+1 WHERE id=?",
            (text, me["id"]),
        )
        conn.execute(
            "UPDATE reviews SET valid=0 WHERE target_id=? AND valid=1", (me["id"],)
        )
        new_version = conn.execute(
            "SELECT version FROM participants WHERE id=?", (me["id"],)
        ).fetchone()["version"]
        conn.commit()
        return jsonify({"ok": True, "version": new_version})
    except Exception:
        conn.rollback()
        raise


@app.post("/api/p/<token>/review")
def submit_review(token):
    data = request.get_json(silent=True) or {}
    content = data.get("content")
    version = data.get("version")
    if not isinstance(content, str) or not content.strip() or len(content) > MAX_TEXT_LEN:
        return err(f"意见不能为空且不超过 {MAX_TEXT_LEN} 字")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        return err("缺少所读稿件的版本号")

    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        me = find_participant(conn, token)
        if me is None:
            conn.rollback()
            return err("个人链接无效", 404)
        session = conn.execute(
            "SELECT published FROM sessions WHERE id=?", (me["session_id"],)
        ).fetchone()
        if session["published"]:
            conn.rollback()
            return err("已公布，意见不可再提交或修改", 409)
        target = get_target(conn, me["session_id"], me["position"])
        # 提交绑定所读稿件版本：过期提交整体拒绝，不覆盖任何已有意见
        if target["version"] != version:
            conn.rollback()
            return err(
                "稿件已更新，请阅读最新版本后再提交",
                409,
                current_version=target["version"],
            )
        conn.execute(
            """
            INSERT INTO reviews (session_id, reviewer_id, target_id, target_version, content, valid, updated_at)
            VALUES (?,?,?,?,?,1,datetime('now'))
            ON CONFLICT(session_id, reviewer_id)
            DO UPDATE SET content=excluded.content,
                          target_version=excluded.target_version,
                          valid=1,
                          updated_at=datetime('now')
            """,
            (me["session_id"], me["id"], target["id"], version, content),
        )
        conn.commit()
        return jsonify({"ok": True})
    except Exception:
        conn.rollback()
        raise


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, threaded=True)
