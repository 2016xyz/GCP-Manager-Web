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
            self._migrate()

    # ------------------------------------------------------------------
    # 增量迁移
    # ------------------------------------------------------------------
    # 用 ALTER TABLE 逐列补齐，老库升级时不会丢数据。
    # 之所以不用「新建表再搬数据」，是因为 SQLite 对已有表加列本来就支持，
    # 而且 vm_passwords 里存着 root 密码，搬表一旦中断风险太高。
    _MIGRATIONS = (
        # 实例备注：创建时可填，建完可改
        ("vm_passwords", "note", "TEXT DEFAULT ''"),
        # 实例真实创建时间（取自 GCP creation_timestamp），用于算「已用费用」
        ("vm_passwords", "created_at", "REAL"),
        # 会话建立后的首次记录时间，作为 created_at 取不到时的兜底
        ("vm_passwords", "first_seen", "REAL"),
        # 创建后自动执行的安装项（逗号分隔的预设 key），便于回溯「这台装了什么」
        ("vm_passwords", "installs", "TEXT DEFAULT ''"),
        # 账号备注（account 表里 label 字段早已存在，这里只为老库兜底）
        ("accounts", "label", "TEXT DEFAULT ''"),
    )

    def _migrate(self):
        """补齐后加字段；已存在则跳过（幂等）"""
        for table, column, decl in self._MIGRATIONS:
            try:
                cols = {r["name"] for r in
                        self.conn.execute(f"PRAGMA table_info({table})").fetchall()}
                if not cols:            # 表不存在（理论上不会）
                    continue
                if column not in cols:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            except sqlite3.OperationalError:
                # 并发启动时另一进程可能刚好加过同一列，忽略
                pass
        self.conn.commit()

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
                image_key=None, disk_type=None, disk_size_gb=None, note=None,
                created_at=None, installs=None):
        """
        写入 / 更新实例记录。

        note / installs / created_at 为 None 时**不覆盖已有值** ——
        因为创建流程里 save_vm 会被调用多次（创建后、命令执行后各一次），
        若每次都写 NULL，会把用户填的备注和 GCP 返回的创建时间冲掉。
        """
        with self.lock:
            now = time.time()
            row = self.conn.execute(
                "SELECT note, installs, created_at, first_seen FROM vm_passwords WHERE name=?",
                (name,)).fetchone()
            old = dict(row) if row else {}
            # created_at：优先用调用方给的（GCP 真实创建时间）；
            # 没给就保留原值；从没记过才用当前时间兜底。
            if created_at is None:
                created_at = old.get("created_at") or now
            self.conn.execute(
                """INSERT INTO vm_passwords(name,ip,password,account_id,zone,machine_type,
                   image_key,disk_type,disk_size_gb,note,installs,created_at,first_seen,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(name) DO UPDATE SET ip=excluded.ip, password=excluded.password,
                   account_id=excluded.account_id, zone=excluded.zone,
                   machine_type=excluded.machine_type, image_key=excluded.image_key,
                   disk_type=excluded.disk_type, disk_size_gb=excluded.disk_size_gb,
                   note=excluded.note, installs=excluded.installs,
                   created_at=excluded.created_at, updated_at=excluded.updated_at""",
                (name, ip, password, str(account_id) if account_id is not None else None, zone,
                 machine_type, image_key, disk_type, disk_size_gb,
                 old.get("note", "") if note is None else note,
                 old.get("installs", "") if installs is None else installs,
                 created_at,
                 old.get("first_seen") or now, now))
            self.conn.commit()

    def update_vm_note(self, name, note):
        """单独改备注（实例列表里直接编辑）"""
        with self.lock:
            cur = self.conn.execute(
                "UPDATE vm_passwords SET note=?, updated_at=? WHERE name=?",
                (note or "", time.time(), name))
            self.conn.commit()
            # 用 rowcount 判断是否命中记录：conn.total_changes 是累计值，不能用
            return cur.rowcount > 0

    def update_vm_installs(self, name, installs):
        """记录这台机器装了哪些预设（逗号分隔），便于回溯"""
        with self.lock:
            self.conn.execute(
                "UPDATE vm_passwords SET installs=?, updated_at=? WHERE name=?",
                (installs or "", time.time(), name))
            self.conn.commit()

    def update_vm_password(self, name, password):
        """更新 root 密码（重新设置后回写）"""
        with self.lock:
            self.conn.execute("UPDATE vm_passwords SET password=?, updated_at=? WHERE name=?",
                              (password or "", time.time(), name))
            self.conn.commit()

    def forget_vm(self, name):
        """实例已不存在时清掉本地记录"""
        with self.lock:
            self.conn.execute("DELETE FROM vm_passwords WHERE name=?", (name,))
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
