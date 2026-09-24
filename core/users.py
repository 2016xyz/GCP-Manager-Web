# -*- coding: utf-8 -*-
"""
用户 / 会话 / 审计 存储层
"""
import secrets
import sqlite3
import threading
import time

from .auth import hash_password, verify_password, ROLE_ADMIN, ROLES, SESSION_TTL


class UserStore:
    def __init__(self, store):
        self.store = store
        self.conn = store.conn
        self.lock = store.lock
        self._init_schema()

    def _init_schema(self):
        with self.lock:
            c = self.conn
            c.execute("""CREATE TABLE IF NOT EXISTS users(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                salt TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'operator',
                display_name TEXT DEFAULT '',
                disabled INTEGER DEFAULT 0,
                must_change_password INTEGER DEFAULT 0,
                last_login REAL,
                last_ip TEXT DEFAULT '',
                login_count INTEGER DEFAULT 0,
                failed_count INTEGER DEFAULT 0,
                created_at REAL,
                updated_at REAL,
                created_by TEXT DEFAULT 'system')""")
            c.execute("""CREATE TABLE IF NOT EXISTS sessions(
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                role TEXT NOT NULL,
                ip TEXT DEFAULT '',
                user_agent TEXT DEFAULT '',
                created_at REAL,
                last_seen REAL,
                expires_at REAL)""")
            c.execute("""CREATE TABLE IF NOT EXISTS audit(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL, username TEXT, ip TEXT, action TEXT,
                target TEXT DEFAULT '', detail TEXT DEFAULT '', ok INTEGER DEFAULT 1)""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(ts)")
            c.commit()

    # ------------------------------------------------------------------
    # 用户
    # ------------------------------------------------------------------
    def count_users(self):
        with self.lock:
            return self.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def create_user(self, username, password, role=ROLE_ADMIN, display_name="",
                    must_change=False, created_by="system"):
        username = (username or "").strip()
        if not username:
            raise ValueError("用户名不能为空")
        if role not in ROLES:
            raise ValueError(f"非法角色：{role}")
        pw_hash, salt = hash_password(password)
        now = time.time()
        with self.lock:
            try:
                cur = self.conn.execute(
                    """INSERT INTO users(username,password_hash,salt,role,display_name,
                       disabled,must_change_password,created_at,updated_at,created_by)
                       VALUES(?,?,?,?,?,0,?,?,?,?)""",
                    (username, pw_hash, salt, role, display_name,
                     1 if must_change else 0, now, now, created_by))
                self.conn.commit()
                return cur.lastrowid
            except sqlite3.IntegrityError:
                raise ValueError(f"用户名已存在：{username}")

    def get_user(self, user_id=None, username=None):
        with self.lock:
            if user_id is not None:
                r = self.conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            else:
                r = self.conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
            return dict(r) if r else None

    def list_users(self, include_disabled=True):
        with self.lock:
            sql = "SELECT * FROM users" + ("" if include_disabled else " WHERE disabled=0")
            rows = self.conn.execute(sql + " ORDER BY id").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d.pop("password_hash", None)
            d.pop("salt", None)
            out.append(d)
        return out

    def update_user(self, user_id, **kw):
        allowed = ("role", "display_name", "disabled", "must_change_password")
        fields = [k for k in allowed if k in kw]
        if not fields:
            return 0
        if "role" in kw and kw["role"] not in ROLES:
            raise ValueError(f"非法角色：{kw['role']}")
        sets = ",".join(f"{f}=?" for f in fields) + ",updated_at=?"
        args = [kw[f] for f in fields] + [time.time(), user_id]
        with self.lock:
            cur = self.conn.execute(f"UPDATE users SET {sets} WHERE id=?", args)
            self.conn.commit()
            return cur.rowcount

    def set_password(self, user_id, new_password, must_change=False):
        pw_hash, salt = hash_password(new_password)
        with self.lock:
            self.conn.execute(
                "UPDATE users SET password_hash=?,salt=?,must_change_password=?,updated_at=?"
                " WHERE id=?",
                (pw_hash, salt, 1 if must_change else 0, time.time(), user_id))
            self.conn.commit()

    def delete_user(self, user_id):
        with self.lock:
            self.conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            cur = self.conn.execute("DELETE FROM users WHERE id=?", (user_id,))
            self.conn.commit()
            return cur.rowcount

    def verify_login(self, username, password):
        """返回 (user_dict|None, 原因)"""
        u = self.get_user(username=username)
        if not u:
            # 用户不存在时也做一次哈希，避免用户名枚举的时间差
            hash_password(password or "x")
            return None, "用户名或密码错误"
        if u["disabled"]:
            return None, "该账号已被禁用"
        if not verify_password(password or "", u["password_hash"], u["salt"]):
            with self.lock:
                self.conn.execute("UPDATE users SET failed_count=failed_count+1 WHERE id=?",
                                  (u["id"],))
                self.conn.commit()
            return None, "用户名或密码错误"
        return u, "ok"

    def mark_login(self, user_id, ip):
        with self.lock:
            self.conn.execute(
                "UPDATE users SET last_login=?,last_ip=?,login_count=login_count+1,"
                "failed_count=0,updated_at=? WHERE id=?",
                (time.time(), ip or "", time.time(), user_id))
            self.conn.commit()

    # ------------------------------------------------------------------
    # 会话
    # ------------------------------------------------------------------
    def create_session(self, user, ip, user_agent, ttl=SESSION_TTL):
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self.lock:
            self.conn.execute(
                """INSERT INTO sessions(token,user_id,username,role,ip,user_agent,
                   created_at,last_seen,expires_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                (token, user["id"], user["username"], user["role"], ip or "",
                 (user_agent or "")[:300], now, now, now + ttl))
            self.conn.commit()
        return token, now + ttl

    def get_session(self, token):
        if not token:
            return None
        with self.lock:
            r = self.conn.execute(
                "SELECT s.*, u.disabled AS user_disabled, u.must_change_password AS must_change "
                "FROM sessions s LEFT JOIN users u ON u.id=s.user_id WHERE s.token=?",
                (token,)).fetchone()
        if not r:
            return None
        sess = dict(r)
        if sess["expires_at"] < time.time():
            self.revoke_session(token)
            return None
        if sess.get("user_disabled"):
            self.revoke_session(token)
            return None
        # 滑动续期（限频写库）
        if time.time() - (sess["last_seen"] or 0) > 300:
            with self.lock:
                self.conn.execute("UPDATE sessions SET last_seen=? WHERE token=?",
                                  (time.time(), token))
                self.conn.commit()
        return sess

    def revoke_session(self, token):
        with self.lock:
            self.conn.execute("DELETE FROM sessions WHERE token=?", (token,))
            self.conn.commit()

    def revoke_user_sessions(self, user_id):
        with self.lock:
            cur = self.conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            self.conn.commit()
            return cur.rowcount

    def list_sessions(self):
        with self.lock:
            rows = self.conn.execute(
                "SELECT token,user_id,username,role,ip,user_agent,created_at,last_seen,"
                "expires_at FROM sessions WHERE expires_at>? ORDER BY last_seen DESC",
                (time.time(),)).fetchall()
        return [dict(r) for r in rows]

    def purge_expired_sessions(self):
        with self.lock:
            cur = self.conn.execute("DELETE FROM sessions WHERE expires_at<?", (time.time(),))
            self.conn.commit()
            return cur.rowcount

    # ------------------------------------------------------------------
    # 审计
    # ------------------------------------------------------------------
    def audit(self, username, ip, action, target="", detail="", ok=True):
        with self.lock:
            self.conn.execute(
                "INSERT INTO audit(ts,username,ip,action,target,detail,ok) VALUES(?,?,?,?,?,?,?)",
                (time.time(), username or "-", ip or "-", action, target, detail, 1 if ok else 0))
            self.conn.commit()

    def get_audit(self, limit=200):
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM audit ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
