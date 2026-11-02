"""单机 SQLite 检查点与事件日志；所有网络/工具执行均在事务之外。"""

import json
import os
from pathlib import Path
import sqlite3
import re
import threading
import time

from .workspace_binding import WorkspaceError


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


class Store:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        # 一个数据库只有一个运行主机；进程退出后 OS 自动释放锁。
        self._lock_file = open(str(self.path) + ".lock", "a+b")
        self._lock_file.seek(0)
        if os.fstat(self._lock_file.fileno()).st_size == 0:
            self._lock_file.write(b"0")
            self._lock_file.flush()
        self._lock_file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self._lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._lock_file.close()
            raise RuntimeError("database already in use") from None
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, history TEXT NOT NULL DEFAULT '[]',
                created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
                request_id TEXT UNIQUE NOT NULL, prompt TEXT NOT NULL,
                state TEXT NOT NULL, status TEXT NOT NULL, updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES runs(id),
                data TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS event_run ON events(run_id,seq);
            CREATE TABLE IF NOT EXISTS workspaces (
                id TEXT PRIMARY KEY, path TEXT NOT NULL, identity TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS host_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        # 旧版数据库原地补列，已有会话和运行无需重新导入。
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(sessions)")}
        if "archived" not in columns:
            with self.db:
                self.db.execute("ALTER TABLE sessions ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")
        if "workspace_id" not in columns:
            with self.db:
                self.db.execute("ALTER TABLE sessions ADD COLUMN workspace_id TEXT")

    @staticmethod
    def _setting_text(value):
        if not isinstance(value, str):
            raise ValueError("invalid_host_setting")
        try:
            if len(value.encode("utf-8")) > 65536:
                raise ValueError("invalid_host_setting")
        except UnicodeError:
            raise ValueError("invalid_host_setting") from None
        return value

    @staticmethod
    def _setting_key(key):
        if not isinstance(key, str) or re.fullmatch(r"[a-z0-9_.-]{1,80}", key) is None:
            raise ValueError("invalid_host_setting")

    def host_setting(self, key):
        """Read one bounded opaque host value; do not expose the settings table."""
        self._setting_key(key)
        with self._lock:
            row = self.db.execute(
                "SELECT length(CAST(value AS BLOB)), CASE WHEN length(CAST(value AS BLOB))<=65536 "
                "THEN value ELSE NULL END FROM host_settings WHERE key=?", (key,)).fetchone()
            if row is None:
                return None
            if row[0] is None or row[0] > 65536:
                raise ValueError("invalid_host_setting")
            return self._setting_text(row[1])

    def compare_and_swap_host_setting(self, key, expected, value, *, updates=None):
        """CAS one value and commit dependent opaque settings in one transaction.

        A catalog revision and its protected credential must never be committed
        separately. None in updates deletes a setting; a failed CAS writes none.
        """
        self._setting_key(key)
        value = self._setting_text(value)
        if expected is not None:
            self._setting_text(expected)
        updates = dict(updates or {})
        for other, text in updates.items():
            self._setting_key(other)
            if other == key:
                raise ValueError("invalid_host_setting")
            if text is not None:
                self._setting_text(text)
        with self._lock, self.db:
            if expected is None:
                cursor = self.db.execute("INSERT OR IGNORE INTO host_settings(key,value) VALUES(?,?)", (key, value))
            else:
                cursor = self.db.execute("UPDATE host_settings SET value=? WHERE key=? AND value=?", (value, key, expected))
            if cursor.rowcount != 1:
                return False
            for other, text in updates.items():
                if text is None:
                    self.db.execute("DELETE FROM host_settings WHERE key=?", (other,))
                else:
                    self.db.execute("INSERT INTO host_settings(key,value) VALUES(?,?) "
                                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (other, text))
            return True

    @staticmethod
    def workspace_spec(spec):
        if (type(spec) is not dict or set(spec) != {"id", "path", "identity"}
                or not isinstance(spec["id"], str) or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", spec["id"]) is None
                or not isinstance(spec["path"], str) or not 1 <= len(spec["path"]) <= 4096
                or any(ord(char) < 32 for char in spec["path"])
                or not isinstance(spec["identity"], str) or re.fullmatch(r"[0-9a-f]{64}", spec["identity"]) is None):
            raise WorkspaceError("invalid_workspace")
        try:
            spec["path"].encode("utf-8")
        except UnicodeError:
            raise WorkspaceError("invalid_workspace") from None
        return dict(spec)

    def initialize_workspaces(self, startup):
        startup = self.workspace_spec(startup)
        with self._lock, self.db:
            row = self.db.execute("SELECT value FROM host_settings WHERE key='startup_workspace_id'").fetchone()
            if row is None:
                self.db.execute("INSERT INTO workspaces VALUES(?,?,?)", tuple(startup[k] for k in ("id", "path", "identity")))
                self.db.executemany("INSERT INTO host_settings VALUES(?,?)", (
                    ("startup_workspace_id", startup["id"]), ("selected_workspace_id", startup["id"]),
                    ("selection_revision", "0")))
            elif row[0] != startup["id"] or self.workspace(row[0]) != startup:
                raise WorkspaceError("workspace_changed")

    def workspace(self, identifier):
        with self._lock:
            row = self.db.execute("SELECT id,path,identity FROM workspaces WHERE id=?", (identifier,)).fetchone()
            if row is None:
                raise WorkspaceError("workspace_not_configured")
            return self.workspace_spec(dict(row))

    def workspaces(self):
        with self._lock:
            settings = dict(self.db.execute("SELECT key,value FROM host_settings"))
            if "startup_workspace_id" not in settings:
                raise WorkspaceError("workspace_disabled")
            return {"selected_id": settings["selected_workspace_id"],
                    "startup_id": settings["startup_workspace_id"], "revision": int(settings["selection_revision"]),
                    "items": [dict(row) for row in self.db.execute("SELECT id,path,identity FROM workspaces ORDER BY rowid DESC")]}

    def select_workspace(self, spec, expected_revision):
        spec = self.workspace_spec(spec)
        if type(expected_revision) is not int or expected_revision < 0:
            raise WorkspaceError("invalid_workspace")
        with self._lock:
            try:
                self.db.execute("BEGIN IMMEDIATE")
                current = self.workspaces()
                if current["revision"] != expected_revision:
                    raise WorkspaceError("workspace_conflict")
                old = next((item for item in current["items"] if item["id"] == spec["id"]), None)
                if old is not None and old != spec:
                    raise WorkspaceError("workspace_changed")
                if old is None:
                    if len(current["items"]) >= 32:
                        raise WorkspaceError("workspace_conflict")
                    self.db.execute("INSERT INTO workspaces VALUES(?,?,?)", tuple(spec[k] for k in ("id", "path", "identity")))
                self.db.execute("UPDATE host_settings SET value=? WHERE key='selected_workspace_id'", (spec["id"],))
                self.db.execute("UPDATE host_settings SET value=? WHERE key='selection_revision'", (str(expected_revision + 1),))
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise
            return self.workspaces()

    def sessions(self, include_archived=False):
        with self._lock:
            rows = self.db.execute(
                "SELECT id,title,created,archived,workspace_id FROM sessions WHERE (? OR archived=0) ORDER BY created DESC",
                (int(include_archived),))
            return [{**{key: row[key] for key in row.keys() if key != "workspace_id" or row[key] is not None},
                     "archived": bool(row["archived"])} for row in rows]

    def session(self, identifier):
        with self._lock:
            row = self.db.execute("SELECT * FROM sessions WHERE id=?", (identifier,)).fetchone()
            if row is None:
                raise KeyError(identifier)
            return {"id": row["id"], "title": row["title"], "created": row["created"],
                    "archived": bool(row["archived"]), "messages": json.loads(row["history"]),
                    **({"workspace_id": row["workspace_id"]} if row["workspace_id"] is not None else {})}

    def update_session(self, identifier, title, archived):
        with self._lock, self.db:
            self.db.execute("UPDATE sessions SET title=?,archived=? WHERE id=?",
                            (title, int(archived), identifier))

    def delete_session(self, identifier):
        with self._lock, self.db:
            # 按外键顺序在同一事务删除，不能留下半个会话。
            self.db.execute("DELETE FROM events WHERE run_id IN (SELECT id FROM runs WHERE session_id=?)",
                            (identifier,))
            self.db.execute("DELETE FROM runs WHERE session_id=?", (identifier,))
            self.db.execute("DELETE FROM sessions WHERE id=?", (identifier,))

    def get(self, identifier):
        with self._lock:
            row = self.db.execute("SELECT state FROM runs WHERE id=?", (identifier,)).fetchone()
            if row is None:
                raise KeyError(identifier)
            return json.loads(row[0])

    def request(self, request_id):
        with self._lock:
            row = self.db.execute("SELECT * FROM runs WHERE request_id=?", (request_id,)).fetchone()
            return dict(row) if row else None

    def runs(self, session_id=None):
        with self._lock:
            if session_id is None:
                rows = self.db.execute("SELECT state FROM runs ORDER BY updated DESC").fetchall()
            else:
                rows = self.db.execute("SELECT state FROM runs WHERE session_id=? ORDER BY updated DESC",
                                       (session_id,)).fetchall()
            return [json.loads(row[0]) for row in rows]

    def create(self, state, request_id, prompt, new_session=False):
        with self._lock, self.db:
            if new_session:
                self.db.execute("INSERT INTO sessions(id,title,created,workspace_id) VALUES(?,?,?,?)",
                                (state["session_id"], prompt[:60], time.time(), state.get("workspace_id")))
            self.db.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?)", (
                state["id"], state["session_id"], request_id, prompt,
                encode(state), state["status"], time.time()))

    def save(self, state, event=None):
        with self._lock, self.db:
            # 终态与会话追加同一事务；history_saved防止多次保存重复加入对话。
            # A host-selected tool result is data, not an assistant-authored
            # answer. Keep it in the run/trace without promoting it into chat.
            if state["status"] == "completed" and state.get("kind") != "tool" and not state.get("history_saved"):
                row = self.db.execute("SELECT history FROM sessions WHERE id=?",
                                      (state["session_id"],)).fetchone()
                history = json.loads(row[0])
                history.extend([{"role": "user", "text": state["prompt"]},
                                {"role": "assistant", "text": state["output"]}])
                self.db.execute("UPDATE sessions SET history=? WHERE id=?",
                                (encode(history[-200:]), state["session_id"]))
                state["history_saved"] = True
            self.db.execute("UPDATE runs SET state=?,status=?,updated=? WHERE id=?",
                            (encode(state), state["status"], time.time(), state["id"]))
            if event is not None:
                self.db.execute("INSERT INTO events(run_id,data) VALUES(?,?)",
                                (state["id"], encode({**event, "time": time.time()})))

    def event(self, run_id, data):
        with self._lock, self.db:
            data = {**data, "time": time.time()}
            self.db.execute("INSERT INTO events(run_id,data) VALUES(?,?)", (run_id, encode(data)))

    def events(self, run_id, after=0):
        with self._lock:
            rows = self.db.execute("SELECT seq,data FROM events WHERE run_id=? AND seq>? ORDER BY seq LIMIT 500",
                                   (run_id, after)).fetchall()
            return [{**json.loads(row["data"]), "seq": row["seq"]} for row in rows]

    def close(self):
        with self._lock:
            self.db.close()
            self._lock_file.close()
