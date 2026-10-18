"""Opening the database read-only and loading the small tables into memory.

The server keeps `samples` (679 rows) and `genes` (78,932 rows) in Python, so
filtering samples and resolving gene names never touch SQL. Only expression
values are read from `expr`, by integer keys.
"""

import os
import threading
from pathlib import Path

import duckdb

from jtari_mcp.build_db import SCHEMA_VERSION

# <repo>/data/jtari.duckdb, overridable with JTARI_DB.
DEFAULT_DB = Path(__file__).resolve().parents[2] / "data" / "jtari.duckdb"


class DatabaseError(RuntimeError):
    """The database is missing or was built by an incompatible version."""


class Database:
    def __init__(self, path: Path):
        if not path.exists():
            raise DatabaseError(
                f"database not found at {path}. Build it with: uv run jtari-build-db"
            )
        self.path = path
        self._con = duckdb.connect(str(path), read_only=True)
        version = self._con.execute(
            "SELECT value FROM provenance_build WHERE key = 'schema_version'"
        ).fetchone()
        if version is None or int(version[0]) != SCHEMA_VERSION:
            raise DatabaseError(
                f"{path} has schema version {version and version[0]}, this server needs "
                f"{SCHEMA_VERSION}. Rebuild it with: uv run jtari-build-db"
            )
        self.samples = self._fetch_dicts("SELECT * FROM samples ORDER BY sample_idx")
        self.genes = self._fetch_dicts("SELECT * FROM genes ORDER BY gene_idx")
        self.datasets = self._fetch_dicts("SELECT * FROM datasets ORDER BY dataset")
        self.contrasts = self._fetch_dicts("SELECT * FROM contrasts ORDER BY contrast_id")
        self.contrast_members: dict[str, dict[str, list[int]]] = {}
        for cid, role, idx in self._con.execute(
            "SELECT contrast_id, role, sample_idx FROM contrast_members"
        ).fetchall():
            self.contrast_members.setdefault(cid, {}).setdefault(role, []).append(idx)
        self.docs = dict(self._con.execute("SELECT name, content FROM docs").fetchall())
        self.provenance_build = dict(
            self._con.execute("SELECT key, value FROM provenance_build").fetchall()
        )
        self.provenance_inputs = self._fetch_dicts(
            # mtime read as text: fetching TIMESTAMPTZ into Python would need pytz.
            "SELECT * REPLACE (CAST(mtime AS VARCHAR) AS mtime) FROM provenance_inputs "
            "ORDER BY role, dataset"
        )

    def _fetch_dicts(self, sql: str) -> list[dict]:
        cur = self._con.execute(sql)
        names = [d[0] for d in cur.description]
        return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]

    def expression(
        self, gene_idxs: list[int], sample_idxs: list[int], unit: str
    ) -> dict[tuple[int, int], float]:
        """(gene_idx, sample_idx) -> value for the requested genes and samples."""
        if unit not in ("tpm", "counts"):
            raise ValueError(unit)
        # The SDK runs tool calls on worker threads; a DuckDB connection must not be
        # used by two threads at once, but each thread may use its own cursor.
        cur = self._con.cursor()
        try:
            rows = cur.execute(
                f"SELECT gene_idx, sample_idx, {unit} FROM expr "
                "WHERE gene_idx IN (SELECT unnest(?)) AND sample_idx IN (SELECT unnest(?))",
                [gene_idxs, sample_idxs],
            ).fetchall()
        finally:
            cur.close()
        return {(g, s): v for g, s, v in rows}


_db: Database | None = None
_lock = threading.Lock()


def get_db() -> Database:
    """The shared Database, opened on first use (so the server starts even when the
    database hasn't been built yet, and reports that in the tool result instead)."""
    global _db
    with _lock:
        if _db is None:
            _db = Database(Path(os.environ.get("JTARI_DB", DEFAULT_DB)))
        return _db


def reset_db() -> None:
    """Forget the open database (tests point JTARI_DB at a fixture and call this)."""
    global _db
    with _lock:
        _db = None
