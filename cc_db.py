"""SQLite database layer for ClipCutter.

Schema + migrations + small row-mapping helpers + domain-specific DB helpers
(session_output_dir, snipcut_update). All callers open a connection via
get_db() and close it themselves.
"""

import re
import sqlite3

from cc_config import DB_PATH, OUTPUT_DIR


def get_db():
    """Open a new connection with FK enforcement and Row factory."""
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def row_to_dict(row):
    """Convert a sqlite3.Row (or None) to a plain dict."""
    if row is None:
        return None
    return dict(row)


def rows_to_list(rows):
    return [dict(r) for r in rows]


def init_db():
    """Create tables and set persistent PRAGMAs. Idempotent."""
    conn = get_db()
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            youtube_url TEXT NOT NULL,
            video_title TEXT DEFAULT '',
            gather_phase TEXT DEFAULT '',
            stream_captions TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS clips (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES sessions(id),
            note TEXT DEFAULT '',
            center_seconds INTEGER DEFAULT 0,
            window_seconds INTEGER DEFAULT 300,
            start_seconds INTEGER DEFAULT 0,
            end_seconds INTEGER DEFAULT 0,
            status TEXT DEFAULT 'queued',
            error_text TEXT DEFAULT '',
            raw_file TEXT DEFAULT '',
            transcript_json TEXT DEFAULT '',
            ai_suggestion_start REAL,
            ai_suggestion_end REAL,
            ai_reasoning TEXT DEFAULT '',
            final_start REAL,
            final_end REAL,
            export_file TEXT DEFAULT '',
            suggestion_id TEXT DEFAULT '',
            trimmed_transcript TEXT DEFAULT '',
            generated_title TEXT DEFAULT '',
            generated_description TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS suggestions (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES sessions(id),
            sort_order INTEGER DEFAULT 0,
            timestamp_seconds INTEGER DEFAULT 0,
            suggested_title TEXT DEFAULT '',
            reasoning TEXT DEFAULT '',
            confidence TEXT DEFAULT 'high',
            selected INTEGER DEFAULT 1,
            source TEXT DEFAULT 'ai',
            note TEXT DEFAULT '',
            window_seconds INTEGER DEFAULT 300,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.commit()

    # Migrations — each wrapped so re-running on an existing DB is a no-op.
    # Columns from deleted features (edl_path, markers_path, refined_*, mode,
    # resolve_status) are intentionally NOT dropped: SQLite can't drop columns
    # cleanly, and leaving them harmless avoids a destructive migration.
    for migration in [
        "ALTER TABLE clips ADD COLUMN generated_title TEXT DEFAULT ''",
        "ALTER TABLE clips ADD COLUMN generated_description TEXT DEFAULT ''",
        "ALTER TABLE clips ADD COLUMN suggestion_id TEXT DEFAULT ''",
        "ALTER TABLE clips ADD COLUMN trimmed_transcript TEXT DEFAULT ''",
        "ALTER TABLE sessions ADD COLUMN gather_phase TEXT DEFAULT ''",
        "ALTER TABLE sessions ADD COLUMN stream_captions TEXT DEFAULT ''",
        "ALTER TABLE snipcut_jobs ADD COLUMN transcript_path TEXT DEFAULT ''",
        "ALTER TABLE snipcut_jobs ADD COLUMN output_fps INTEGER DEFAULT 30",
        "ALTER TABLE snipcut_jobs ADD COLUMN metadata_json TEXT DEFAULT ''",
        "ALTER TABLE snipcut_jobs ADD COLUMN srt_path TEXT DEFAULT ''",
        "ALTER TABLE snipcut_jobs ADD COLUMN resolve_script TEXT DEFAULT ''",
    ]:
        try:
            conn.execute(migration)
        except sqlite3.OperationalError as e:
            if "duplicate column name" not in str(e):
                raise
    conn.commit()

    # SnipCut jobs table
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS snipcut_jobs (
            id TEXT PRIMARY KEY,
            input_path TEXT NOT NULL,
            input_filename TEXT DEFAULT '',
            status TEXT DEFAULT 'queued',
            error_text TEXT DEFAULT '',
            duration_seconds REAL DEFAULT 0,
            is_vfr INTEGER DEFAULT 0,
            width INTEGER DEFAULT 0,
            height INTEGER DEFAULT 0,
            cfr_output_path TEXT DEFAULT '',
            cfr_progress REAL DEFAULT 0,
            transcribe_progress REAL DEFAULT 0,
            transcript_json TEXT DEFAULT '',
            silence_gaps_json TEXT DEFAULT '',
            cuts_json TEXT DEFAULT '',
            analysis_reasoning TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.commit()

    conn.close()


# ---------- Domain-specific DB helpers ----------

_FS_UNSAFE = re.compile(r'[^\w\s-]')
_FS_WHITESPACE = re.compile(r'\s+')


def _sanitize_folder_name(raw: str) -> str:
    """Inline version of sanitize_note — avoids importing from cc_helpers
    to keep the cc_helpers -> cc_db dependency one-way."""
    safe = _FS_UNSAFE.sub('', raw).strip()
    safe = _FS_WHITESPACE.sub('_', safe)
    return safe or "clip"


def get_session_output_dir(session_id: str):
    """Return the per-session output folder inside ClipCutter_Clips, creating
    it if needed. Format: ~/ClipCutter_Clips/YYYY-MM-DD_Video_Title/"""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT video_title, created_at FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
    finally:
        conn.close()

    if row and row["video_title"]:
        date_str = (row["created_at"] or "")[:10]  # YYYY-MM-DD
        title_slug = _sanitize_folder_name(row["video_title"])[:50]
        folder_name = f"{date_str}_{title_slug}" if date_str else title_slug
    else:
        folder_name = session_id

    out = OUTPUT_DIR / folder_name
    out.mkdir(parents=True, exist_ok=True)
    return out


def snipcut_update(job_id: str, **fields):
    """Update a snipcut job's fields in DB. No-op if fields is empty."""
    if not fields:
        return
    conn = get_db()
    try:
        cols = ", ".join(f"{k} = ?" for k in fields.keys())
        values = list(fields.values()) + [job_id]
        conn.execute(f"UPDATE snipcut_jobs SET {cols} WHERE id = ?", values)
        conn.commit()
    finally:
        conn.close()
