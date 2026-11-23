"""SQLite locally or durable PostgreSQL online. Public queries never return workbooks."""
from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import json
import sqlite3
from publication import checked_public_views

DDL = """
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY, title TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY, event_id TEXT NOT NULL REFERENCES events(event_id), pathway TEXT NOT NULL,
    after_trade INTEGER NOT NULL CHECK(after_trade IN (0,1)), snapshot TEXT NOT NULL,
    reference TEXT NOT NULL, report_hash TEXT NOT NULL, published_at TEXT NOT NULL,
    published INTEGER NOT NULL CHECK(published IN (0,1)), result TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sources (
    report_hash TEXT PRIMARY KEY, filename TEXT NOT NULL, raw BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS visible_runs ON runs (published,event_id,published_at);
"""

class Connection:
    def __init__(self, connection, pg):
        self.connection, self.pg = connection, pg
    def execute(self, sql, params=()):
        return self.connection.execute(sql.replace("?", "%s") if self.pg else sql, params)


class Repository:
    def __init__(self, db: Path, dsn: str = ""):
        self.db, self.dsn = Path(db), dsn
        self._pool = None
        if not dsn:
            self.db.parent.mkdir(parents=True, exist_ok=True)
        else:
            from psycopg.rows import dict_row
            from psycopg_pool import ConnectionPool
            self._pool = ConnectionPool(
                conninfo=dsn, min_size=0, max_size=4, timeout=10, max_idle=120,
                kwargs={"connect_timeout":8, "row_factory":dict_row, "prepare_threshold":None},
                open=False,
            )
            self._pool.open()
        try:
            with self.connect() as c:
                if not dsn:
                    c.execute("PRAGMA journal_mode=WAL")
                else:
                    c.execute("SELECT pg_advisory_xact_lock(517296487)")
                ddl = DDL.replace(" BLOB", " BYTEA") if dsn else DDL
                for statement in ddl.split(";"):
                    if statement.strip():
                        c.execute(statement)
        except BaseException:
            self.close()
            raise

    def close(self):
        if self._pool is not None:
            self._pool.close()

    @contextmanager
    def connect(self):
        if self._pool is not None:
            with self._pool.connection() as c:
                yield Connection(c, True)
            return
        c = sqlite3.connect(self.db, timeout=15)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        try:
            with c:
                yield Connection(c, False)
        finally:
            c.close()

    def title(self, event, default):
        with self.connect() as c:
            row = c.execute("SELECT title FROM events WHERE event_id=?", (event,)).fetchone()
        return row["title"] if row else default

    def titles(self):
        with self.connect() as c:
            return {r["event_id"]:r["title"] for r in c.execute("SELECT event_id,title FROM events")}

    def get_pair(self, a_id, b_id):
        with self.connect() as c:
            result = {r["id"]:json.loads(r["result"]) for r in c.execute(
                "SELECT id,result FROM runs WHERE published=1 AND id IN (?,?)", (a_id,b_id)
            )}
        return result.get(a_id),result.get(b_id)

    def catalogue(self, private=False):
        where = "" if private else "WHERE r.published=1"
        with self.connect() as c:
            return [dict(row) for row in c.execute(
                "SELECT r.id,r.event_id,r.pathway,r.after_trade,r.snapshot,r.reference,r.published_at,r.published,e.title "
                "FROM runs r JOIN events e ON e.event_id=r.event_id "
                + where + " ORDER BY r.published_at DESC,r.id"
            )]

    def get(self, run_id, private=False):
        sql = "SELECT result FROM runs WHERE id=?" + ("" if private else " AND published=1")
        with self.connect() as c:
            row = c.execute(sql, (run_id,)).fetchone()
        return json.loads(row["result"]) if row else None

    def publish(self, title, result, filename, workbook, visibility=None):
        if visibility is not None:
            visibility = checked_public_views(visibility)
            result = {**result, "public_views": visibility}
        title = title.strip()
        if not title or len(title) > 110:
            raise ValueError("Enter a Scenathon name of 1–110 characters.")
        digest = sha256(workbook).hexdigest()
        if digest != result["report_hash"]:
            raise ValueError("The workbook changed after validation. Run the preview again.")
        identity = [result[k] for k in ("event", "pathway", "after_trade", "snapshot", "reference_fingerprint", "report_hash")]
        run_id = sha256(json.dumps(identity,ensure_ascii=False).encode()).hexdigest()[:24]
        name = filename.replace("\\","/").split("/")[-1][:180] or "scenathon.xlsx"
        when = datetime.now(timezone.utc).isoformat(timespec="seconds")
        payload = json.dumps(result, allow_nan=False, ensure_ascii=False, separators=(",", ":"))
        with self.connect() as c:
            c.execute("INSERT INTO events VALUES (?,?) ON CONFLICT(event_id) DO UPDATE SET title=excluded.title",
                      (result["event"], title))
            archived = c.execute("SELECT 1 FROM sources WHERE report_hash=?", (digest,)).fetchone()
            if not archived:
                c.execute("INSERT INTO sources VALUES (?,?,?) ON CONFLICT(report_hash) DO NOTHING",
                          (digest, name, workbook))
            cur = c.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
                            (run_id, result["event"], result["pathway"], int(result["after_trade"]), result["snapshot"],
                             result["reference_fingerprint"], digest, when, 1, payload))
            if cur.rowcount == 0:
                if visibility is not None:
                    self._write_public_views(c, run_id, visibility)
                c.execute("UPDATE runs SET published=1, published_at=? WHERE id=? AND published=0",
                          (when,run_id))
                return run_id, False
        return run_id, True

    def _write_public_views(self, c, run_id, visibility):
        sql = "SELECT result FROM runs WHERE id=?" + (" FOR UPDATE" if c.pg else "")
        row = c.execute(sql, (run_id,)).fetchone()
        if row is None:
            raise ValueError("Snapshot no longer exists.")
        saved = json.loads(row["result"])
        saved["public_views"] = visibility
        result = json.dumps(saved, allow_nan=False, ensure_ascii=False, separators=(",", ":"))
        c.execute("UPDATE runs SET result=? WHERE id=?", (result, run_id))

    def set_public_views(self, run_id, visibility):
        visibility = checked_public_views(visibility)
        with self.connect() as c:
            if not c.pg:
                c.execute("BEGIN IMMEDIATE")
            self._write_public_views(c, run_id, visibility)

    def hide(self, run_id):
        with self.connect() as c:
            c.execute("UPDATE runs SET published=0 WHERE id=?", (run_id,))

    def restore(self, run_id):
        with self.connect() as c:
            row = c.execute("SELECT id FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise ValueError("Snapshot no longer exists.")
            stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
            c.execute("UPDATE runs SET published=1, published_at=? WHERE id=?",
                      (stamp, run_id))

    def delete(self, run_id):
        """Delete a stored snapshot, preserving sources referenced by other runs."""
        with self.connect() as c:
            if not c.pg:
                c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT report_hash FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise ValueError("Snapshot no longer exists.")
            digest = row["report_hash"]
            c.execute("DELETE FROM runs WHERE id=?", (run_id,))
            remaining = c.execute("SELECT 1 FROM runs WHERE report_hash=? LIMIT 1",
                                  (digest,)).fetchone()
            if remaining is None:
                c.execute("DELETE FROM sources WHERE report_hash=?", (digest,))

    def source(self, run_id):
        with self.connect() as c:
            row = c.execute("SELECT s.filename,s.raw FROM runs r JOIN sources s ON s.report_hash=r.report_hash WHERE r.id=?",
                            (run_id,)).fetchone()
        return (row["filename"], row["raw"]) if row else None
