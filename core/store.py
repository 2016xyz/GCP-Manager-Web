# -*- coding: utf-8 -*-
"""本地 SQLite 存储层：账号 / 实例密码 / 任务 / 日志 / 设置"""
import json
import os
import sqlite3
import threading
import time


class Store:
    def __init__(self, db_path):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        with self.lock:
            c = self.conn
            c.execute("""CREATE TABLE IF NOT EXISTS accounts(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT, project_id TEXT, key_path TEXT,
                proxy TEXT DEFAULT '', proxy_type TEXT DEFAULT 'HTTPS',
                label TEXT DEFAULT '', created_at REAL)""")
            c.execute("""CREATE TABLE IF NOT EXISTS vm_passwords(
                name TEXT PRIMARY KEY, ip TEXT, password TEXT, account_id TEXT,
                zone TEXT, machine_type TEXT, image_key TEXT, disk_type TEXT,
                disk_size_gb INTEGER, updated_at REAL)""")
            c.execute("""CREATE TABLE IF NOT EXISTS tasks(
                id TEXT PRIMARY KEY, kind TEXT, status TEXT, payload TEXT,
                result TEXT, message TEXT, created_at REAL, updated_at REAL)""")
            c.execute("""CREATE TABLE IF NOT EXISTS logs(
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, task_id TEXT, level TEXT, message TEXT)""")
            c.execute("""CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT)""")
            c.commit()

    # ---------------- accounts ----------------
    def add_account(self, email, project_id, key_path, proxy="", proxy_type="HTTPS", label=""):
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO accounts(email,project_id,key_path,proxy,proxy_type,label,created_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (email, project_id, key_path, proxy, proxy_type, label, time.time()))
            self.conn.commit()
            return cur.lastrowid

    def upsert_account_by_key(self, email, project_id, key_path, proxy="", proxy_type="HTTPS", label=""):
        with self.lock:
            row = self.conn.execute(
                "SELECT id FROM accounts WHERE key_path=? AND project_id=?",
                (key_path, project_id)).fetchone()
            if row:
                return row["id"], False
            return self.add_account(email, project_id, key_path, proxy, proxy_type, label), True

    def delete_account(self, acc_id):
        with self.lock:
            self.conn.execute("DELETE FROM accounts WHERE id=?", (acc_id,))
            self.conn.commit()

    def update_account(self, acc_id, **kw):
        fields = [k for k in ("email", "project_id", "key_path", "proxy", "proxy_type", "label") if k in kw]
        if not fields:
            return
        sql = "UPDATE accounts SET " + ",".join(f"{f}=?" for f in fields) + " WHERE id=?"
        with self.lock:
            self.conn.execute(sql, [kw[f] for f in fields] + [acc_id])
            self.conn.commit()

    def get_accounts(self):
        with self.lock:
            return [dict(r) for r in self.conn.execute("SELECT * FROM accounts ORDER BY id").fetchall()]

    def get_account(self, acc_id):
        with self.lock:
            r = self.conn.execute("SELECT * FROM accounts WHERE id=?", (acc_id,)).fetchone()
            return dict(r) if r else None

    # ---------------- vm passwords ----------------
    def save_vm(self, name, ip, password, account_id=None, zone=None, machine_type=None,
                image_key=None, disk_type=None, disk_size_gb=None):
        with self.lock:
            self.conn.execute(
                """INSERT INTO vm_passwords(name,ip,password,account_id,zone,machine_type,
                   image_key,disk_type,disk_size_gb,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(name) DO UPDATE SET ip=excluded.ip, password=excluded.password,
                   account_id=excluded.account_id, zone=excluded.zone,
                   machine_type=excluded.machine_type, image_key=excluded.image_key,
                   disk_type=excluded.disk_type, disk_size_gb=excluded.disk_size_gb,
                   updated_at=excluded.updated_at""",
                (name, ip, password, str(account_id) if account_id is not None else None, zone,
                 machine_type, image_key, disk_type, disk_size_gb, time.time()))
            self.conn.commit()

    def get_vm(self, name):
        with self.lock:
            r = self.conn.execute("SELECT * FROM vm_passwords WHERE name=?", (name,)).fetchone()
            return dict(r) if r else None

    def get_all_vms(self):
        with self.lock:
            return [dict(r) for r in self.conn.execute("SELECT * FROM vm_passwords").fetchall()]

    # ---------------- tasks ----------------
    def create_task(self, task_id, kind, payload=None):
        with self.lock:
            now = time.time()
            self.conn.execute(
                "INSERT INTO tasks(id,kind,status,payload,result,message,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (task_id, kind, "queued", json.dumps(payload or {}, ensure_ascii=False),
                 "", "", now, now))
            self.conn.commit()

    def update_task(self, task_id, **kw):
        fields = [k for k in ("status", "result", "message") if k in kw]
        if not fields:
            return
        sets = []
        args = []
        for f in fields:
            v = kw[f]
            if f in ("result",) and not isinstance(v, (str, type(None))):
                v = json.dumps(v, ensure_ascii=False)
            sets.append(f"{f}=?")
            args.append(v)
        sets.append("updated_at=?")
        args.append(time.time())
        args.append(task_id)
        with self.lock:
            self.conn.execute(f"UPDATE tasks SET {','.join(sets)} WHERE id=?", args)
            self.conn.commit()

    def get_task(self, task_id):
        with self.lock:
            r = self.conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            return self._task_row(r) if r else None

    def get_tasks(self, limit=200, kind=None):
        with self.lock:
            if kind:
                rows = self.conn.execute(
                    "SELECT * FROM tasks WHERE kind=? ORDER BY created_at DESC LIMIT ?",
                    (kind, limit)).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
            return [self._task_row(r) for r in rows]

    @staticmethod
    def _task_row(r):
        d = dict(r)
        for f in ("payload", "result"):
            try:
                d[f] = json.loads(d[f]) if d[f] else ({} if f == "payload" else None)
            except Exception:
                pass
        return d

    # ---------------- logs ----------------
    def add_log(self, message, task_id=None, level="info"):
        with self.lock:
            self.conn.execute("INSERT INTO logs(ts,task_id,level,message) VALUES(?,?,?,?)",
                              (time.time(), task_id, level, message))
            self.conn.commit()

    def get_logs(self, since_id=0, limit=500, task_id=None):
        with self.lock:
            if task_id:
                rows = self.conn.execute(
                    "SELECT * FROM logs WHERE id>? AND task_id=? ORDER BY id LIMIT ?",
                    (since_id, task_id, limit)).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT * FROM logs WHERE id>? ORDER BY id LIMIT ?", (since_id, limit)).fetchall()
            return [dict(r) for r in rows]

    def clear_logs(self):
        with self.lock:
            self.conn.execute("DELETE FROM logs")
            self.conn.commit()

    # ---------------- settings ----------------
    def set_setting(self, k, v):
        with self.lock:
            self.conn.execute("INSERT OR REPLACE INTO settings(k,v) VALUES(?,?)",
                              (k, json.dumps(v, ensure_ascii=False)))
            self.conn.commit()

    def get_setting(self, k, default=None):
        with self.lock:
            r = self.conn.execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone()
            if not r:
                return default
            try:
                return json.loads(r["v"])
            except Exception:
                return default

    def get_all_settings(self):
        with self.lock:
            out = {}
            for r in self.conn.execute("SELECT k,v FROM settings").fetchall():
                try:
                    out[r["k"]] = json.loads(r["v"])
                except Exception:
                    out[r["k"]] = r["v"]
            return out
