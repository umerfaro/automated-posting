"""Durable optional idempotency. Independent processes share SQLite's transaction lock."""
from contextlib import contextmanager
import hashlib
import sqlite3
from pathlib import Path

from fastapi import HTTPException

from app.schemas import PostingResponse


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as connection:
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

    def claim(self, key, message):
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
            connection.execute("INSERT INTO requests(key,message_hash) VALUES (?,?)", (key, digest))
        return None

    def finish(self, key, response):
        with self.connect() as connection:
            connection.execute("UPDATE requests SET response=? WHERE key=?", (response.model_dump_json(), key))

    def account(self, platform):
        import json
        with self.connect() as connection:
            row = connection.execute("SELECT payload FROM accounts WHERE platform=?", (platform,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_account(self, platform, account):
        import json
        with self.connect() as connection:
            connection.execute("INSERT OR REPLACE INTO accounts VALUES (?,?)", (platform, json.dumps(account)))

    def add_state(self, state, platform, verifier):
        import time
        with self.connect() as connection:
            connection.execute("DELETE FROM oauth_states WHERE expires < ?", (time.time(),))
            connection.execute("INSERT INTO oauth_states VALUES (?,?,?,?)", (state, platform, time.time() + 600, verifier))

    def take_state(self, state, platform):
        import time
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT verifier FROM oauth_states WHERE state=? AND platform=? AND expires>?",
                                     (state, platform, time.time())).fetchone()
            connection.execute("DELETE FROM oauth_states WHERE state=?", (state,))
        if not row:
            raise ValueError("Sign-in expired or already used. Connect again.")
        return row[0]
