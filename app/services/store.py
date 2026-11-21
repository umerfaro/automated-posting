"""Durable optional idempotency. Independent processes share SQLite's transaction lock."""
from contextlib import contextmanager
import hashlib
import sqlite3
from pathlib import Path

from fastapi import HTTPException

from app.schemas import PostingResponse
from app.tenancy import workspace


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.vault = None
        self.saas = None
        with self.connect() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY, value TEXT NOT NULL)")
            connection.execute("CREATE TABLE IF NOT EXISTS accounts (platform TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            connection.execute("CREATE TABLE IF NOT EXISTS oauth_states (state TEXT PRIMARY KEY, platform TEXT, expires REAL, verifier TEXT)")
            connection.execute("""CREATE TABLE IF NOT EXISTS requests (
                key TEXT PRIMARY KEY, message_hash TEXT NOT NULL,
                response TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")

        Path(path).chmod(0o600)

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def scoped(self, key):
        return workspace.get() + ":" + key if workspace.get() else key

    def encode(self, value):
        return self.vault.encrypt(value.encode()).decode() if self.vault else value

    def decode(self, value):
        return self.vault.decrypt(value.encode()).decode() if self.vault and value.startswith("gAAAA") else value

    def claim(self, key, message, kind="text", title=""):
        original_key = key
        key = self.scoped(key)
        digest = hashlib.sha256(message.encode()).hexdigest()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute("SELECT message_hash, response FROM requests WHERE key=?", (key,)).fetchone()
            if previous:
                if previous[0] != digest:
                    raise HTTPException(409, "This Idempotency-Key was used for a different message.")
                if previous[1] is None:
                    raise HTTPException(409, "This request is in progress or was interrupted. Check platform accounts before retrying.")
                import json
                return json.loads(previous[1])
            if self.saas and workspace.get():
                self.saas.reserve(connection, workspace.get(), original_key, kind, title or message[:100])
            connection.execute("INSERT INTO requests(key,message_hash) VALUES (?,?)", (key, digest))
        return None

    def finish(self, key, response):
        statuses = {result.status for result in response.results}
        outcome = ('pending' if 'pending' in statuses else 'partial' if {'success','failed'} <= statuses
                   else 'published' if response.success else 'failed')
        key = self.scoped(key)
        with self.connect() as connection:
            connection.execute("UPDATE requests SET response=? WHERE key=?", (response.model_dump_json(), key))
            if self.saas and workspace.get():
                connection.execute("UPDATE publications SET response=?,status=? WHERE workspace=? AND request_key=?",
                                   (response.model_dump_json(), outcome, workspace.get(), key.split(":",1)[1]))

    def account(self, platform):
        platform = self.scoped(platform)
        import json
        with self.connect() as connection:
            row = connection.execute("SELECT payload FROM accounts WHERE platform=?", (platform,)).fetchone()
        return json.loads(self.decode(row[0])) if row else None

    def save_account(self, platform, account):
        platform = self.scoped(platform)
        import json
        with self.connect() as connection:
            connection.execute("INSERT OR REPLACE INTO accounts VALUES (?,?)", (platform, self.encode(json.dumps(account))))

    def add_state(self, state, platform, verifier):
        platform = self.scoped(platform)
        import time
        with self.connect() as connection:
            connection.execute("DELETE FROM oauth_states WHERE expires < ?", (time.time(),))
            connection.execute("INSERT INTO oauth_states VALUES (?,?,?,?)", (state, platform, time.time() + 600, verifier))

    def take_state(self, state, platform):
        platform = self.scoped(platform)
        import time
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT verifier FROM oauth_states WHERE state=? AND platform=? AND expires>?",
                                     (state, platform, time.time())).fetchone()
            connection.execute("DELETE FROM oauth_states WHERE state=? AND platform=?", (state, platform))
        if not row:
            raise ValueError("Sign-in expired or already used. Connect again.")
        return row[0]

    def settings(self):
        import json
        with self.connect() as connection:
            prefix = workspace.get() + ":" if workspace.get() else ""
            rows = (connection.execute("SELECT name,value FROM settings WHERE name LIKE ?", (prefix + '%',)).fetchall()
                    if prefix else connection.execute("SELECT name,value FROM settings WHERE instr(name, ':')=0").fetchall())
            return {name[len(prefix):]: json.loads(self.decode(value)) for name, value in rows
                    if (name.startswith(prefix) if prefix else ":" not in name)}

    def save_settings(self, values, platform=None):
        import json
        with self.connect() as connection:
            connection.executemany("INSERT OR REPLACE INTO settings VALUES (?,?)",
                                   [(self.scoped(key), self.encode(json.dumps(value))) for key, value in values.items()])
            if platform:
                platform = self.scoped(platform)
                connection.execute("DELETE FROM accounts WHERE platform=?", (platform,))
                connection.execute("DELETE FROM oauth_states WHERE platform=?", (platform,))
