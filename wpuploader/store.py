"""Local, crash-recoverable ledger with short transactions."""

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4
from typing import Iterator

from .protocol import ProtocolError, require_local

DDL = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS bundles (
 package_id TEXT PRIMARY KEY, producer_id TEXT NOT NULL, article_id TEXT NOT NULL,
 revision INTEGER NOT NULL, artifact_revision INTEGER NOT NULL,
 manifest_hash TEXT NOT NULL, bundle_hash TEXT NOT NULL, cache_path TEXT NOT NULL,
 metadata TEXT NOT NULL, UNIQUE(producer_id, article_id, revision, artifact_revision));
CREATE TABLE IF NOT EXISTS deliveries (
 target_id TEXT NOT NULL, package_id TEXT NOT NULL REFERENCES bundles(package_id),
 operation_key TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL,
 remote_post_id TEXT, remote_hash TEXT, remote_status TEXT, attempt_id TEXT,
 error TEXT, PRIMARY KEY(target_id, package_id));
CREATE TABLE IF NOT EXISTS source_claims (
 target_id TEXT NOT NULL, source_hash TEXT NOT NULL, producer_id TEXT NOT NULL,
 article_id TEXT NOT NULL, package_id TEXT NOT NULL,
 PRIMARY KEY(target_id, source_hash));
CREATE TABLE IF NOT EXISTS media_deliveries (
 target_id TEXT NOT NULL, package_id TEXT NOT NULL, asset_hash TEXT NOT NULL,
 state TEXT NOT NULL, remote_media_id TEXT, slug TEXT NOT NULL,
 error TEXT, PRIMARY KEY(target_id,package_id,asset_hash));
CREATE TABLE IF NOT EXISTS events (
 event_id TEXT PRIMARY KEY, target_id TEXT NOT NULL, package_id TEXT NOT NULL,
 sequence INTEGER NOT NULL, payload TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 0,
 UNIQUE(target_id,package_id,sequence));
"""


class Store:
    def __init__(self, directory: Path, *, initialize: bool = False) -> None:
        self.directory = require_local(directory)
        self.path = self.directory / "state.sqlite3"
        if not self.path.exists() and not initialize:
            raise ProtocolError("STATE_INIT_REQUIRED")
        self.directory.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        # DELETE avoids the WAL-reset defect on older standard-library runtimes.
        # One remote writer is serialized by an OS lock; no WAL is required.
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA busy_timeout=5000")
        if initialize:
            self.db.executescript(DDL)
            self.db.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version','1')")
        if (
            self.db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
            != "1"
        ):
            raise ProtocolError("STATE_SCHEMA_UNSUPPORTED")

        self.db.execute("INSERT OR IGNORE INTO meta VALUES ('instance_id',?)", (str(uuid4()),))

    def close(self) -> None:
        self.db.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield self.db
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def backup(self, path: Path) -> None:
        destination = sqlite3.connect(path)
        try:
            self.db.backup(destination)
        finally:
            destination.close()

    def status(self) -> list[dict]:
        return [
            dict(row)
            for row in self.db.execute(
                "SELECT target_id,package_id,state,remote_post_id,remote_hash,error FROM deliveries"
            )
        ]
