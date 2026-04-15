#!/usr/bin/env python3
"""
ClipCutter v2 — AI-powered livestream clip editor.
Paste timestamps, get transcribed clips with AI sizzle reel suggestions.
"""

import os
import re
import sys
import json
import shutil
import sqlite3
import subprocess
import threading
import uuid
from pathlib import Path

# ---------- PATH Setup ----------
# Ensure Homebrew binaries (ffmpeg, etc.) are findable even when launched from .app
for _p in ["/opt/homebrew/bin", "/usr/local/bin"]:
    if _p not in os.environ.get("PATH", ""):
        os.environ["PATH"] = _p + ":" + os.environ.get("PATH", "")

# ---------- Dependency Bootstrap ----------

def check_and_install_pip_deps():
    """Auto-install Python dependencies on first run."""
    required = {"flask": "flask", "webview": "pywebview", "whisper": "openai-whisper", "anthropic": "anthropic", "faster_whisper": "faster-whisper"}
    missing = []
    for import_name, pip_name in required.items():
        try:
            __import__(import_name)
        except ImportError:
            missing.append(pip_name)
    if missing:
        print(f"  Installing: {', '.join(missing)}...")
        cmd = [sys.executable, "-m", "pip", "install", *missing, "-q"]
        try:
            subprocess.check_call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except subprocess.CalledProcessError:
            subprocess.check_call(cmd + ["--break-system-packages"],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print("  Done.")

def check_system_deps():
    """Check for yt-dlp and ffmpeg, return list of missing."""
    missing = []
    if not shutil.which("ffmpeg"):
        missing.append("ffmpeg")
    if not shutil.which("yt-dlp"):
        try:
            subprocess.run([sys.executable, "-m", "yt_dlp", "--version"],
                           capture_output=True, check=True)
        except (subprocess.CalledProcessError, FileNotFoundError):
            missing.append("yt-dlp")
    return missing

def get_ytdlp_cmd():
    """Return the correct command to invoke yt-dlp."""
    base = ["yt-dlp"] if shutil.which("yt-dlp") else [sys.executable, "-m", "yt_dlp"]
    # yt-dlp 2026+ requires a JS runtime + challenge solver for YouTube
    if shutil.which("node"):
        base += ["--js-runtimes", "node", "--remote-components", "ejs:github"]
    return base

# Run bootstrap before importing flask/webview
check_and_install_pip_deps()

from flask import Flask, Response, render_template_string, request, jsonify, send_file
import struct
import webview

app = Flask(__name__)

# ---------- Paths ----------

APP_DIR = Path.home() / ".clipcutter"
APP_DIR.mkdir(exist_ok=True)
DB_PATH = APP_DIR / "clipcutter.db"
CONFIG_PATH = APP_DIR / "config.json"
SESSIONS_DIR = APP_DIR / "sessions"
SESSIONS_DIR.mkdir(exist_ok=True)
OUTPUT_DIR = Path.home() / "ClipCutter_Clips"
OUTPUT_DIR.mkdir(exist_ok=True)

# ---------- Config ----------

DEFAULT_CONFIG = {
    "api_key": "",
    "default_clip_window": 5,
    "target_duration": 60,
    "whisper_model": "base",
    "output_dir": str(OUTPUT_DIR),
    "channel_profile": "",
    "streambuddy_url": "",
    "streambuddy_token": "",
}

def load_config():
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH) as f:
                saved = json.load(f)
            merged = {**DEFAULT_CONFIG, **saved}
            return merged
        except (json.JSONDecodeError, IOError):
            pass
    return dict(DEFAULT_CONFIG)

def save_config(config):
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)

# ---------- Database ----------

def get_db():
    """Get a database connection."""
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def init_db():
    """Create tables and set persistent PRAGMAs."""
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

    # Schema migrations — idempotent, commit once after all statements
    for migration in [
        "ALTER TABLE clips ADD COLUMN generated_title TEXT DEFAULT ''",
        "ALTER TABLE clips ADD COLUMN generated_description TEXT DEFAULT ''",
        "ALTER TABLE clips ADD COLUMN suggestion_id TEXT DEFAULT ''",
        "ALTER TABLE clips ADD COLUMN trimmed_transcript TEXT DEFAULT ''",
        "ALTER TABLE sessions ADD COLUMN gather_phase TEXT DEFAULT ''",
        "ALTER TABLE sessions ADD COLUMN stream_captions TEXT DEFAULT ''",
        "ALTER TABLE snipcut_jobs ADD COLUMN edl_path TEXT DEFAULT ''",
        "ALTER TABLE snipcut_jobs ADD COLUMN resolve_status TEXT DEFAULT ''",
        "ALTER TABLE snipcut_jobs ADD COLUMN transcript_path TEXT DEFAULT ''",
        "ALTER TABLE snipcut_jobs ADD COLUMN output_fps INTEGER DEFAULT 30",
        "ALTER TABLE snipcut_jobs ADD COLUMN markers_path TEXT DEFAULT ''",
        "ALTER TABLE snipcut_jobs ADD COLUMN mode TEXT DEFAULT 'full'",
        "ALTER TABLE snipcut_jobs ADD COLUMN refined_edl_path TEXT DEFAULT ''",
        "ALTER TABLE snipcut_jobs ADD COLUMN refined_markers_path TEXT DEFAULT ''",
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

        CREATE TABLE IF NOT EXISTS snipcut_markers (
            id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL,
            sort_order INTEGER NOT NULL,
            start_seconds REAL NOT NULL,
            end_seconds REAL NOT NULL,
            reason TEXT NOT NULL,
            content TEXT DEFAULT '',
            decision TEXT DEFAULT 'pending',
            FOREIGN KEY (job_id) REFERENCES snipcut_jobs(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_snipcut_markers_job
            ON snipcut_markers(job_id, sort_order);
    """)
    conn.commit()

    conn.close()

def row_to_dict(row):
    """Convert a sqlite3.Row to a plain dict."""
    if row is None:
        return None
    return dict(row)

def rows_to_list(rows):
    return [dict(r) for r in rows]

# ---------- Helpers ----------

def sanitize_note(note: str) -> str:
    """Convert a clip note to a filesystem-safe string."""
    safe = re.sub(r'[^\w\s-]', '', note).strip()
    safe = re.sub(r'\s+', '_', safe)
    return safe or "clip"

def get_session_output_dir(session_id: str) -> Path:
    """Return the per-session output folder inside ClipCutter_Clips, creating it if needed.
    Format: ~/ClipCutter_Clips/YYYY-MM-DD_Video_Title/"""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT video_title, created_at FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
    finally:
        conn.close()

    if row and row["video_title"]:
        date_str = (row["created_at"] or "")[:10]  # YYYY-MM-DD
        title_slug = sanitize_note(row["video_title"])[:50]
        folder_name = f"{date_str}_{title_slug}" if date_str else title_slug
    else:
        folder_name = session_id

    out = OUTPUT_DIR / folder_name
    out.mkdir(parents=True, exist_ok=True)
    return out

def extract_trimmed_transcript(transcript_data: dict, trim_start: float, trim_end: float) -> str:
    """Return transcript text for segments that overlap the trim range."""
    return " ".join(
        seg["text"].strip()
        for seg in transcript_data.get("segments", [])
        if seg["end"] > trim_start and seg["start"] < trim_end
    )

def resolve_trim_range(clip: dict) -> tuple:
    """Return (trim_start, trim_end) — final_start/end > AI suggestion > full window."""
    fs, ai_s = clip.get("final_start"), clip.get("ai_suggestion_start")
    fe, ai_e = clip.get("final_end"), clip.get("ai_suggestion_end")
    start = fs if fs is not None else (ai_s if ai_s is not None else 0)
    end = fe if fe is not None else (ai_e if ai_e is not None else (clip.get("window_seconds") or 300))
    return start, end

CLAUDE_HAIKU = "claude-haiku-4-5-20251001"
CLAUDE_SONNET = "claude-sonnet-4-20250514"

def call_claude(api_key: str, prompt: str, max_tokens: int = 300, model: str = CLAUDE_HAIKU) -> dict:
    """Call Claude, strip optional code fences, and return parsed JSON."""
    import anthropic
    text = anthropic.Anthropic(api_key=api_key).messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    ).content[0].text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    return json.loads(text)

# ---------- Phase A: Scan ----------

def parse_json3_captions(data: dict) -> str:
    """Convert YouTube json3 auto-caption events to a deduplicated timestamped transcript."""
    segments = []
    seen = set()
    for event in data.get("events", []):
        segs = event.get("segs", [])
        if not segs:
            continue
        text = "".join(s.get("utf8", "") for s in segs).replace("\n", " ").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        t_s = event.get("tStartMs", 0) / 1000
        mins, secs = int(t_s // 60), int(t_s % 60)
        segments.append(f"[{mins}:{secs:02d}] {text}")
    return "\n".join(segments)


def scan_session(session_id: str):
    """Phase A: download YouTube auto-captions, analyze with Claude, store suggestions."""
    conn = get_db()
    try:
        session = row_to_dict(conn.execute(
            "SELECT id, youtube_url FROM sessions WHERE id = ?", (session_id,)
        ).fetchone())
        if not session:
            return

        url = session["youtube_url"]
        config = load_config()
        api_key = config.get("api_key", "")

        conn.execute("UPDATE sessions SET gather_phase = 'scanning' WHERE id = ?", (session_id,))
        conn.commit()

        session_dir = SESSIONS_DIR / session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        captions_base = session_dir / "captions"

        ytdlp_cmd = get_ytdlp_cmd()

        # Step 1: fetch video title (separate call — --print causes early exit)
        title_result = subprocess.run(
            [*ytdlp_cmd, "--print", "title", "--no-playlist", url],
            capture_output=True, text=True, timeout=30
        )
        video_title = title_result.stdout.strip().splitlines()[0] if title_result.stdout.strip() else ""

        conn.execute("UPDATE sessions SET video_title = ? WHERE id = ?", (video_title, session_id))
        conn.commit()

        # Step 2: download auto-captions (no --print, so yt-dlp actually writes the file)
        cmd = [
            *ytdlp_cmd,
            "--write-auto-subs",
            "--sub-lang", "en",
            "--skip-download",
            "--sub-format", "json3",
            "-o", str(captions_base),
            "--no-playlist",
            url,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)

        # Find captions file (yt-dlp appends language code)
        captions_file = None
        for suffix in [".en.json3", ".en-US.json3", ".en-GB.json3"]:
            candidate = Path(str(captions_base) + suffix)
            if candidate.exists():
                captions_file = candidate
                break

        if not captions_file:
            conn.execute(
                "UPDATE sessions SET gather_phase = 'no_captions', video_title = ? WHERE id = ?",
                (video_title, session_id)
            )
            conn.commit()
            return

        with open(captions_file) as f:
            transcript_text = parse_json3_captions(json.load(f))

        conn.execute(
            "UPDATE sessions SET stream_captions = ?, video_title = ? WHERE id = ?",
            (transcript_text, video_title, session_id)
        )
        conn.commit()

        # Without API key, skip analysis — user can still add segments manually
        if not api_key or not transcript_text.strip():
            conn.execute("UPDATE sessions SET gather_phase = 'selecting' WHERE id = ?", (session_id,))
            conn.commit()
            return

        target_duration = config.get("target_duration", 60)
        prompt = f"""You are a YouTube Shorts editor for a finance content creator's livestream.

Analyze this full livestream transcript and identify the 5-10 strongest moments that would work as standalone YouTube Shorts (~{target_duration - 15}-{target_duration + 15} seconds each).

Look for moments that have:
- A clear, complete thesis or insight (not mid-thought)
- Specific data points, numbers, or analysis
- Natural conviction in the delivery
- Standalone clarity — a viewer with no context should still follow

FULL LIVESTREAM TRANSCRIPT (timestamps in [MM:SS]):
{transcript_text[:14000]}

Respond in JSON only, no other text:
{{"suggestions": [
  {{
    "timestamp_seconds": <int — center point in the stream>,
    "title": "<punchy clip title>",
    "reasoning": "<1-2 sentences why this moment is clippable>",
    "confidence": "high" | "medium"
  }}
]}}

Order by quality (strongest first). Aim for 5-8 suggestions."""

        suggestions_data = call_claude(api_key, prompt, max_tokens=2000, model=CLAUDE_SONNET)

        for i, s in enumerate(suggestions_data.get("suggestions", [])):
            conn.execute(
                """INSERT INTO suggestions
                   (id, session_id, sort_order, timestamp_seconds, suggested_title, reasoning, confidence)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (uuid.uuid4().hex[:10], session_id, i,
                 int(s.get("timestamp_seconds", 0)),
                 s.get("title", ""),
                 s.get("reasoning", ""),
                 s.get("confidence", "high"))
            )

        conn.execute("UPDATE sessions SET gather_phase = 'selecting' WHERE id = ?", (session_id,))
        conn.commit()

    except Exception as e:
        conn.execute(
            "UPDATE sessions SET gather_phase = 'error', video_title = ? WHERE id = ?",
            (f"Scan failed: {str(e)[:200]}", session_id)
        )
        conn.commit()
    finally:
        conn.close()


# ---------- Phase B: Collect ----------

def download_clip(clip_id: str, url: str):
    """Download one clip segment and chain to transcription. Runs in its own thread."""
    conn = get_db()
    try:
        clip = row_to_dict(conn.execute(
            "SELECT id, session_id, note, start_seconds, end_seconds FROM clips WHERE id = ?",
            (clip_id,)
        ).fetchone())
        if not clip:
            return

        conn.execute("UPDATE clips SET status = 'downloading' WHERE id = ?", (clip_id,))
        conn.commit()

        session_dir = SESSIONS_DIR / clip["session_id"] / "raw"
        session_dir.mkdir(parents=True, exist_ok=True)

        safe_note = sanitize_note(clip["note"] or "clip")
        output_path = session_dir / f"{safe_note}_{clip_id[:6]}.mp4"
        section_arg = f"*{seconds_to_hms(clip['start_seconds'])}-{seconds_to_hms(clip['end_seconds'])}"

        cmd = [
            *get_ytdlp_cmd(),
            "--download-sections", section_arg,
            "--force-keyframes-at-cuts",
            "-f", "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4]/best",
            "--merge-output-format", "mp4",
            "-o", str(output_path),
            "--no-playlist",
            url,
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        combined_output = (result.stdout or "") + (result.stderr or "")
        if result.returncode != 0:
            print(f"  Download failed (rc={result.returncode}). Output tail: {combined_output[-300:]}")
            if "could not open encoder" in combined_output.lower() or "aac" in combined_output.lower():
                # AAC encoder failure — retry with simpler download approach
                print(f"  Retrying: single-format download, no keyframe forcing")
                # Clean up partial file from failed attempt
                if output_path.exists():
                    try: output_path.unlink()
                    except OSError: pass
                cmd_retry = [
                    *get_ytdlp_cmd(),
                    "--download-sections", section_arg,
                    "-f", "best[height<=1080][ext=mp4]/best",
                    "--no-playlist",
                    "-o", str(output_path),
                    url,
                ]
                result = subprocess.run(cmd_retry, capture_output=True, text=True, timeout=600)
                if result.returncode != 0:
                    print(f"  Retry also failed: {(result.stdout or '')[-200:]}{(result.stderr or '')[-200:]}")

        if result.returncode == 0:
            conn.execute(
                "UPDATE clips SET status = 'downloaded', raw_file = ? WHERE id = ?",
                (str(output_path), clip_id)
            )
            conn.commit()
            conn.close()
            transcribe_clip(clip_id)
        else:
            error_msg = result.stderr[-400:] if result.stderr else "Download failed"
            conn.execute(
                "UPDATE clips SET status = 'error', error_text = ? WHERE id = ?",
                (error_msg, clip_id)
            )
            conn.commit()
            conn.close()
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        conn.execute(
            "UPDATE clips SET status = 'error', error_text = ? WHERE id = ?",
            (str(exc)[:300], clip_id)
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        try:
            conn.execute(
                "UPDATE clips SET status = 'error', error_text = ? WHERE id = ?",
                (f"Download failed: {str(exc)[:300]}", clip_id)
            )
            conn.commit()
        finally:
            conn.close()


def gather_session(session_id: str):
    """Phase B: create clip records from selected suggestions, start parallel downloads."""
    conn = get_db()
    url = None
    clip_ids = []
    try:
        session = row_to_dict(conn.execute(
            "SELECT id, youtube_url FROM sessions WHERE id = ?", (session_id,)
        ).fetchone())
        if not session:
            return

        url = session["youtube_url"]
        config = load_config()
        default_window = config.get("default_clip_window", 5) * 60

        suggestions = rows_to_list(conn.execute(
            "SELECT * FROM suggestions WHERE session_id = ? AND selected = 1 ORDER BY sort_order",
            (session_id,)
        ).fetchall())

        if not suggestions:
            return

        for s in suggestions:
            clip_id = uuid.uuid4().hex[:10]
            window = s.get("window_seconds") or default_window
            half = window // 2
            start = max(0, s["timestamp_seconds"] - half)
            end = s["timestamp_seconds"] + half
            note = s["suggested_title"] or s["note"] or "clip"
            conn.execute(
                """INSERT INTO clips
                   (id, session_id, suggestion_id, note, center_seconds,
                    window_seconds, start_seconds, end_seconds, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued')""",
                (clip_id, session_id, s["id"], note,
                 s["timestamp_seconds"], window, start, end)
            )
            clip_ids.append(clip_id)

        conn.execute("UPDATE sessions SET gather_phase = 'collecting' WHERE id = ?", (session_id,))
        conn.commit()
    finally:
        conn.close()

    # Parallel downloads — one thread per clip
    for clip_id in clip_ids:
        threading.Thread(target=download_clip, args=(clip_id, url), daemon=True).start()


# ---------- Whisper ----------

_whisper_model = None
_whisper_model_name = None
_whisper_lock = threading.Lock()

def get_whisper_model():
    """Lazy-load Whisper model (thread-safe). Reloads if model name changed in settings."""
    global _whisper_model, _whisper_model_name
    config = load_config()
    requested = config.get("whisper_model", "base")
    if _whisper_model is None or _whisper_model_name != requested:
        with _whisper_lock:
            if _whisper_model is None or _whisper_model_name != requested:
                import whisper
                models_dir = str(APP_DIR / "whisper_models")
                print(f"  Loading Whisper model: {requested}...")
                _whisper_model = whisper.load_model(requested, download_root=models_dir)
                _whisper_model_name = requested
                print(f"  Whisper model ready.")
    return _whisper_model

_whisper_inference_lock = threading.Lock()

def _run_whisper(audio_path: str, model_name: str = None) -> dict:
    """Run Whisper transcription. Serialized — PyTorch models are not thread-safe."""
    import whisper
    models_dir = str(APP_DIR / "whisper_models")

    if model_name is None:
        model_name = load_config().get("whisper_model", "base")

    model = get_whisper_model()
    with _whisper_inference_lock:
        try:
            return model.transcribe(audio_path, word_timestamps=True, language="en")
        except RuntimeError as e:
            if "size of tensor" in str(e) and model_name != "base":
                print(f"  Whisper {model_name} failed with tensor error, retrying with base...")
                fallback = whisper.load_model("base", download_root=models_dir)
                return fallback.transcribe(audio_path, word_timestamps=True, language="en")
            raise


def transcribe_clip(clip_id: str):
    """Transcribe a downloaded clip using Whisper. Updates DB with results."""
    should_analyze = False
    conn = get_db()
    try:
        clip = row_to_dict(conn.execute("SELECT id, raw_file FROM clips WHERE id = ?", (clip_id,)).fetchone())
        if not clip or not clip["raw_file"]:
            if clip:
                conn.execute(
                    "UPDATE clips SET status = 'error', error_text = 'No raw file for transcription' WHERE id = ?",
                    (clip_id,)
                )
                conn.commit()
            return

        conn.execute("UPDATE clips SET status = 'transcribing' WHERE id = ?", (clip_id,))
        conn.commit()

        result = _run_whisper(clip["raw_file"])

        transcript_data = {
            "text": result.get("text", ""),
            "segments": [],
        }
        for seg in result.get("segments", []):
            if seg is None:
                continue
            segment = {
                "start": seg.get("start", 0),
                "end": seg.get("end", 0),
                "text": seg.get("text", ""),
                "words": [],
            }
            for w in seg.get("words", []) or []:
                if w is None:
                    continue
                segment["words"].append({
                    "word": w.get("word", ""),
                    "start": w.get("start", 0),
                    "end": w.get("end", 0),
                })
            transcript_data["segments"].append(segment)

        conn.execute(
            "UPDATE clips SET status = 'transcribed', transcript_json = ? WHERE id = ?",
            (json.dumps(transcript_data), clip_id)
        )
        conn.commit()
        should_analyze = True
    except Exception as e:
        conn.execute(
            "UPDATE clips SET status = 'error', error_text = ? WHERE id = ?",
            (f"Transcription failed: {str(e)[:400]}", clip_id)
        )
        conn.commit()
    finally:
        conn.close()

    if should_analyze:
        analyze_clip(clip_id)

# ---------- AI Analysis ----------

def analyze_clip(clip_id: str):
    """Use Claude to identify the best sizzle reel segment from a transcript."""
    conn = get_db()
    try:
        clip = row_to_dict(conn.execute(
            "SELECT id, transcript_json, start_seconds FROM clips WHERE id = ?", (clip_id,)
        ).fetchone())
        config = load_config()
        api_key = config.get("api_key", "")

        # Skip analysis if no transcript or no API key
        if not clip or not clip["transcript_json"] or not api_key:
            if clip:
                conn.execute("UPDATE clips SET status = 'ready' WHERE id = ?", (clip_id,))
                conn.commit()
            return

        conn.execute("UPDATE clips SET status = 'analyzing' WHERE id = ?", (clip_id,))
        conn.commit()

        transcript_data = json.loads(clip["transcript_json"])
        target_duration = config.get("target_duration", 60)
        clip_start = clip["start_seconds"] or 0

        # Format transcript with absolute timestamps for Claude
        formatted_lines = []
        for seg in transcript_data.get("segments", []):
            abs_start = clip_start + seg["start"]
            mins = int(abs_start // 60)
            secs = int(abs_start % 60)
            formatted_lines.append(f"[{mins}:{secs:02d}] {seg['text'].strip()}")
        formatted_transcript = "\n".join(formatted_lines)

        prompt = f"""You are a YouTube Shorts editor for a finance content creator.

Given this transcript from a livestream segment, identify the single best continuous segment (~{target_duration - 15}-{target_duration + 15} seconds) that would work as a standalone YouTube Short.

The ideal segment:
- Has a clear, complete thought or thesis
- Includes specific data points, numbers, or analysis
- Starts and ends at natural sentence boundaries
- Would make a viewer want to watch more
- Works without additional context

TRANSCRIPT (with timestamps relative to the full stream):
{formatted_transcript}

Respond in JSON only, no other text:
{{"start_seconds": <float, seconds from start of this clip>, "end_seconds": <float, seconds from start of this clip>, "duration_seconds": <float>, "reasoning": "<1-2 sentences explaining why this is the strongest segment>", "hook": "<the opening line that grabs attention>"}}"""

        suggestion = call_claude(api_key, prompt, max_tokens=400)

        conn.execute(
            """UPDATE clips SET status = 'ready',
               ai_suggestion_start = ?, ai_suggestion_end = ?, ai_reasoning = ?
               WHERE id = ?""",
            (suggestion["start_seconds"], suggestion["end_seconds"],
             suggestion.get("reasoning", ""), clip_id)
        )
        conn.commit()
    except Exception as e:
        # AI failure shouldn't block workflow — set to ready with error note
        conn.execute(
            "UPDATE clips SET status = 'ready', ai_reasoning = ? WHERE id = ?",
            (f"AI analysis failed: {str(e)[:400]}", clip_id)
        )
        conn.commit()
    finally:
        conn.close()

# ---------- Copy Generation ----------

def generate_clip_copy(clip_id: str) -> tuple | None:
    """Use Claude Haiku to write a title and description for a trimmed clip.
    Returns (title, description) on success, None on failure."""
    conn = get_db()
    try:
        clip = row_to_dict(conn.execute(
            "SELECT id, transcript_json, trimmed_transcript, final_start, final_end, "
            "ai_suggestion_start, ai_suggestion_end, window_seconds "
            "FROM clips WHERE id = ?", (clip_id,)
        ).fetchone())
        config = load_config()
        api_key = config.get("api_key", "")

        if not clip or not clip["transcript_json"] or not api_key:
            return None

        # Prefer pre-stored trimmed transcript; fall back to on-the-fly extraction
        trimmed_text = (clip.get("trimmed_transcript") or "").strip()
        if not trimmed_text:
            trim_start, trim_end = resolve_trim_range(clip)
            transcript_data = json.loads(clip["transcript_json"])
            trimmed_text = extract_trimmed_transcript(transcript_data, trim_start, trim_end)
        if not trimmed_text.strip():
            return None

        channel_profile = config.get("channel_profile", "").strip()
        profile_section = f"Channel profile: {channel_profile}\n\n" if channel_profile else ""

        prompt = f"""{profile_section}You are a social media copywriter. Write a title, description, and tags for this YouTube Short.

TRANSCRIPT:
{trimmed_text}

Rules:
- Title: hook-first, ≤60 characters, no clickbait. Start with a relevant emoji.
- Description: 2-3 sentences written in FIRST PERSON (I/we/my — the creator is posting this, not a third party). Conversational, works for both YouTube and X/Twitter.
- Tags: 5-8 hashtags relevant to the topics discussed. Include ticker symbols as hashtags (e.g. #SOFI #BMNR #NBIS). Mix topic tags (#investing #earnings #stockmarket) with ticker tags.

Respond in JSON only, no other text:
{{"title": "...", "description": "...", "tags": "..."}}"""

        result = call_claude(api_key, prompt, max_tokens=400, model=CLAUDE_SONNET)
        title = result.get("title", "")
        description = result.get("description", "")
        tags = result.get("tags", "")
        # Combine description + tags into one copyable block
        if tags:
            description = f"{description}\n\n{tags}"
        conn.execute(
            "UPDATE clips SET generated_title = ?, generated_description = ? WHERE id = ?",
            (title, description, clip_id)
        )
        conn.commit()
        return title, description
    except Exception as e:
        print(f"  Copy generation failed: {e}")
        return None
    finally:
        conn.close()

# ---------- Export ----------

def ass_timestamp(seconds: float) -> str:
    """Convert seconds to ASS timestamp format: H:MM:SS.cc"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    cs = int((seconds % 1) * 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"

def generate_ass_captions(transcript_json: str, trim_start: float, trim_end: float) -> str:
    """Generate ASS subtitle file from Whisper word timestamps for a trimmed region."""
    data = json.loads(transcript_json)

    # Collect words within trim range
    words = []
    for seg in data.get("segments", []):
        for w in seg.get("words", []):
            if w["end"] > trim_start and w["start"] < trim_end:
                words.append({
                    "word": w["word"].strip(),
                    "start": max(0, w["start"] - trim_start),
                    "end": min(trim_end - trim_start, w["end"] - trim_start),
                })

    # Group words into lines (~7 words per line)
    lines = []
    current_line = []
    for w in words:
        current_line.append(w)
        if len(current_line) >= 7:
            lines.append(current_line)
            current_line = []
    if current_line:
        lines.append(current_line)

    # ASS header
    ass = """[Script Info]
Title: ClipCutter Captions
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,58,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,4,0,2,40,40,80,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    for line_words in lines:
        if not line_words:
            continue
        start = line_words[0]["start"]
        end = line_words[-1]["end"]
        text = " ".join(w["word"] for w in line_words)
        ass += f"Dialogue: 0,{ass_timestamp(start)},{ass_timestamp(end)},Default,,0,0,0,,{text}\n"

    return ass

def export_clip(clip_id: str, captions: bool = True, vertical: bool = True):
    """Export a clip as a final Short: trim, crop vertical, burn captions."""
    conn = get_db()
    try:
        clip = row_to_dict(conn.execute(
            "SELECT id, session_id, note, raw_file, final_start, final_end, "
            "ai_suggestion_start, ai_suggestion_end, transcript_json, window_seconds "
            "FROM clips WHERE id = ?", (clip_id,)
        ).fetchone())
        if not clip or not clip["raw_file"]:
            if clip:
                conn.execute(
                    "UPDATE clips SET status = 'error', error_text = 'No raw file for export' WHERE id = ?",
                    (clip_id,)
                )
                conn.commit()
            return

        conn.execute("UPDATE clips SET status = 'exporting' WHERE id = ?", (clip_id,))
        conn.commit()

        trim_start, trim_end = resolve_trim_range(clip)

        out_dir = get_session_output_dir(clip["session_id"])
        safe_note = sanitize_note(clip["note"] or "clip")
        output_name = f"{safe_note}_{clip_id[:6]}.mp4"
        output_path = out_dir / output_name

        # Build ffmpeg filter chain
        vfilters = []
        ass_path = None

        if captions and clip["transcript_json"]:
            ass_content = generate_ass_captions(clip["transcript_json"], trim_start, trim_end)
            # Write to /tmp with UUID name to avoid path escaping issues (spaces, colons)
            ass_path = Path(f"/tmp/clipcutter_{clip_id}.ass")
            ass_path.write_text(ass_content)

        if vertical:
            vfilters.append("crop=ih*9/16:ih")
            vfilters.append("scale=1080:1920")

        if ass_path:
            vfilters.append(f"ass={ass_path}")

        cmd = [
            "ffmpeg", "-y",
            "-i", clip["raw_file"],
            "-ss", str(trim_start),
            "-to", str(trim_end),
        ]

        if vfilters:
            cmd += ["-vf", ",".join(vfilters)]

        cmd += [
            "-c:v", "libx264", "-preset", "fast", "-crf", "20",
            "-c:a", "aac", "-b:a", "192k",
            str(output_path),
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

        if result.returncode == 0:
            conn.execute(
                "UPDATE clips SET status = 'exported', export_file = ? WHERE id = ?",
                (str(output_path), clip_id)
            )
        else:
            error_msg = result.stderr[-500:] if result.stderr else "ffmpeg export failed"
            print(f"  Export error: {error_msg}")
            conn.execute(
                "UPDATE clips SET status = 'ready', error_text = ? WHERE id = ?",
                (f"Export failed: {error_msg[:400]}", clip_id)
            )
        conn.commit()
    except Exception as e:
        conn.execute(
            "UPDATE clips SET status = 'ready', error_text = ? WHERE id = ?",
            (f"Export failed: {str(e)[:400]}", clip_id)
        )
        conn.commit()
    finally:
        conn.close()

def export_all_clips(session_id: str, captions: bool = True, vertical: bool = True):
    """Export all ready clips in a session sequentially."""
    conn = get_db()
    try:
        clips = rows_to_list(conn.execute(
            "SELECT id FROM clips WHERE session_id = ? AND status = 'ready'",
            (session_id,)
        ).fetchall())
    finally:
        conn.close()
    for clip in clips:
        export_clip(clip["id"], captions=captions, vertical=vertical)

# ---------- Retry ----------

def retry_clip(clip_id: str):
    """Re-attempt a failed clip: re-download if needed, then transcribe + analyze."""
    conn = get_db()
    try:
        clip = row_to_dict(conn.execute(
            "SELECT id, session_id, raw_file, start_seconds, end_seconds, note "
            "FROM clips WHERE id = ?", (clip_id,)
        ).fetchone())
        if not clip:
            return

        raw_exists = clip["raw_file"] and Path(clip["raw_file"]).exists()

        if raw_exists:
            # Raw file exists — skip download, go straight to transcription
            conn.execute(
                "UPDATE clips SET status = 'downloaded', error_text = '' WHERE id = ?",
                (clip_id,)
            )
            conn.commit()
            conn.close()
            transcribe_clip(clip_id)
        else:
            # Need to re-download
            conn.execute(
                "UPDATE clips SET status = 'downloading', error_text = '' WHERE id = ?",
                (clip_id,)
            )
            conn.commit()

            session = row_to_dict(conn.execute(
                "SELECT youtube_url FROM sessions WHERE id = ?",
                (clip["session_id"],)
            ).fetchone())
            if not session:
                conn.close()
                return

            session_dir = SESSIONS_DIR / clip["session_id"] / "raw"
            session_dir.mkdir(parents=True, exist_ok=True)

            start_hms = seconds_to_hms(clip["start_seconds"])
            end_hms = seconds_to_hms(clip["end_seconds"])
            safe_note = sanitize_note(clip["note"] or "clip")
            filename = f"{safe_note}_{clip_id[:6]}.mp4"
            output_path = session_dir / filename
            section_arg = f"*{start_hms}-{end_hms}"

            ytdlp_cmd = get_ytdlp_cmd()
            cmd = [
                *ytdlp_cmd,
                "--download-sections", section_arg,
                "--force-keyframes-at-cuts",
                "-f", "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4]/best",
                "--merge-output-format", "mp4",
                "-o", str(output_path),
                "--no-playlist",
                session["youtube_url"],
            ]

            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
                if result.returncode == 0:
                    conn.execute(
                        "UPDATE clips SET status = 'downloaded', raw_file = ? WHERE id = ?",
                        (str(output_path), clip_id)
                    )
                    conn.commit()
                    conn.close()
                    transcribe_clip(clip_id)
                else:
                    error_msg = result.stderr[-500:] if result.stderr else "Download failed"
                    conn.execute(
                        "UPDATE clips SET status = 'error', error_text = ? WHERE id = ?",
                        (error_msg, clip_id)
                    )
                    conn.commit()
                    conn.close()
            except (subprocess.TimeoutExpired, FileNotFoundError) as e:
                conn.execute(
                    "UPDATE clips SET status = 'error', error_text = ? WHERE id = ?",
                    (str(e)[:400], clip_id)
                )
                conn.commit()
                conn.close()
            return
    except Exception as e:
        conn.execute(
            "UPDATE clips SET status = 'error', error_text = ? WHERE id = ?",
            (f"Retry failed: {str(e)[:400]}", clip_id)
        )
        conn.commit()
    finally:
        try:
            conn.close()
        except Exception:
            pass

# ---------- Timestamp Parser ----------

def parse_timestamp(raw: str) -> int:
    raw = raw.strip().lower()

    colon_match = re.match(r'^(\d+):(\d{1,2}):(\d{2})$', raw)
    if colon_match:
        h, m, s = int(colon_match.group(1)), int(colon_match.group(2)), int(colon_match.group(3))
        return h * 3600 + m * 60 + s

    colon_match2 = re.match(r'^(\d+):(\d{2})$', raw)
    if colon_match2:
        a, b = int(colon_match2.group(1)), int(colon_match2.group(2))
        if a > 9:
            return a * 60 + b
        else:
            return a * 3600 + b * 60

    total = 0
    hr_match = re.search(r'(\d+)\s*(?:hrs?|hours?|h)\b', raw)
    min_match = re.search(r'(\d+)\s*(?:mins?|minutes?|m)\b', raw)
    sec_match = re.search(r'(\d+)\s*(?:secs?|seconds?|s)\b', raw)

    if hr_match or min_match or sec_match:
        if hr_match:
            total += int(hr_match.group(1)) * 3600
        if min_match:
            total += int(min_match.group(1)) * 60
        if sec_match:
            total += int(sec_match.group(1))
        return total

    raise ValueError(f"Could not parse timestamp: '{raw}'")


def seconds_to_hms(s: int) -> str:
    h = s // 3600
    m = (s % 3600) // 60
    sec = s % 60
    return f"{h:02d}:{m:02d}:{sec:02d}"


def parse_clip_entries(text: str, default_duration: int = 300) -> list:
    clips = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue

        duration = default_duration
        duration_match = re.search(r'\|\s*(\d+)\s*(?:mins?|minutes?|m)\s*$', line)
        if duration_match:
            duration = int(duration_match.group(1)) * 60
            line = line[:duration_match.start()].strip()
        else:
            duration_match_s = re.search(r'\|\s*(\d+)\s*(?:secs?|seconds?|s)\s*$', line)
            if duration_match_s:
                duration = int(duration_match_s.group(1))
                line = line[:duration_match_s.start()].strip()

        split_match = re.split(r'\s*[-–]\s*', line, maxsplit=1)
        if len(split_match) == 2:
            ts_raw, note = split_match
        else:
            ts_raw = split_match[0]
            note = "clip"

        try:
            center_sec = parse_timestamp(ts_raw)
        except ValueError:
            continue

        half = duration // 2
        start = max(0, center_sec - half)
        end = center_sec + half

        safe_note = sanitize_note(note)

        clips.append({
            "note": note.strip(),
            "safe_note": safe_note,
            "center": center_sec,
            "start": start,
            "end": end,
            "duration": duration,
            "start_hms": seconds_to_hms(start),
            "end_hms": seconds_to_hms(end),
            "center_hms": seconds_to_hms(center_sec),
        })

    return clips


# ---------- Download Worker ----------

def run_session_downloads(session_id: str):
    """Download all clips for a session, updating DB status as we go."""
    conn = get_db()
    session = row_to_dict(conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone())
    if not session:
        conn.close()
        return

    clips = rows_to_list(conn.execute(
        "SELECT * FROM clips WHERE session_id = ? ORDER BY center_seconds",
        (session_id,)
    ).fetchall())

    session_dir = SESSIONS_DIR / session_id / "raw"
    session_dir.mkdir(parents=True, exist_ok=True)

    ytdlp_cmd = get_ytdlp_cmd()
    url = session["youtube_url"]

    for clip in clips:
        clip_id = clip["id"]

        # Update status to downloading
        conn.execute("UPDATE clips SET status = 'downloading' WHERE id = ?", (clip_id,))
        conn.commit()

        start_hms = seconds_to_hms(clip["start_seconds"])
        end_hms = seconds_to_hms(clip["end_seconds"])
        safe_note = sanitize_note(clip["note"])
        filename = f"{safe_note}_{clip_id[:6]}.mp4"
        output_path = session_dir / filename
        section_arg = f"*{start_hms}-{end_hms}"

        cmd = [
            *ytdlp_cmd,
            "--download-sections", section_arg,
            "--force-keyframes-at-cuts",
            "-f", "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4]/best",
            "--merge-output-format", "mp4",
            "-o", str(output_path),
            "--no-playlist",
            url,
        ]

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            if result.returncode == 0:
                conn.execute(
                    "UPDATE clips SET status = 'downloaded', raw_file = ? WHERE id = ?",
                    (str(output_path), clip_id)
                )
                conn.commit()
                conn.close()
                transcribe_clip(clip_id)
                conn = get_db()
                continue
            else:
                error_msg = result.stderr[-500:] if result.stderr else "Unknown error"
                conn.execute(
                    "UPDATE clips SET status = 'error', error_text = ? WHERE id = ?",
                    (error_msg, clip_id)
                )
        except subprocess.TimeoutExpired:
            conn.execute("UPDATE clips SET status = 'error', error_text = 'Timed out (10 min)' WHERE id = ?", (clip_id,))
        except FileNotFoundError:
            conn.execute("UPDATE clips SET status = 'error', error_text = 'yt-dlp not found' WHERE id = ?", (clip_id,))

        conn.commit()

    conn.close()


# ---------- SnipCut: AI-Assisted Rough Cut Pipeline ----------

def snipcut_probe(input_path: str) -> dict:
    """Probe video file for duration, fps, VFR status, resolution."""
    cmd = ["ffprobe", "-v", "quiet", "-print_format", "json",
           "-show_format", "-show_streams", input_path]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {result.stderr[:200]}")

    data = json.loads(result.stdout)
    video_stream = next((s for s in data.get("streams", []) if s["codec_type"] == "video"), None)
    if not video_stream:
        raise RuntimeError("No video stream found.")

    def parse_rate(rate_str):
        try:
            num, den = map(int, rate_str.split("/"))
            return num / den if den else 0
        except Exception:
            return 0

    r_fps = parse_rate(video_stream.get("r_frame_rate", "0/1"))
    avg_fps = parse_rate(video_stream.get("avg_frame_rate", "0/1"))
    is_vfr = abs(r_fps - avg_fps) > 1.0

    return {
        "duration": float(data.get("format", {}).get("duration", 0)),
        "fps": r_fps,
        "width": int(video_stream.get("width", 0)),
        "height": int(video_stream.get("height", 0)),
        "is_vfr": is_vfr,
        "size_mb": os.path.getsize(input_path) / (1024 * 1024),
    }


def _snipcut_update(job_id: str, **fields):
    """Update a snipcut job's fields in DB."""
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


def snipcut_convert_cfr(input_path: str, output_path: str, job_id: str, target_fps: int = 30) -> tuple:
    """Convert to CFR or copy bit-for-bit if already CFR.
    Returns (output_path, actual_fps) — actual_fps is the source fps when copying,
    or target_fps when re-encoding."""
    info = snipcut_probe(input_path)
    duration_us = info["duration"] * 1_000_000

    if not info["is_vfr"]:
        # Already CFR — copy (zero quality loss). Output keeps source fps.
        _snipcut_update(job_id, cfr_progress=10.0)
        shutil.copy2(input_path, output_path)
        _snipcut_update(job_id, cfr_progress=100.0, cfr_output_path=output_path)
        return output_path, round(info["fps"])

    cmd = [
        "ffmpeg", "-i", input_path,
        "-vsync", "cfr", "-r", str(target_fps),
        "-c:v", "libx264", "-preset", "medium", "-crf", "16",
        "-c:a", "copy", "-movflags", "+faststart",
        "-progress", "pipe:1", "-y", output_path,
    ]
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)

    for line in process.stdout:
        line = line.strip()
        if line.startswith("out_time_us="):
            try:
                current_us = int(line.split("=")[1])
                if duration_us > 0:
                    pct = min(98.0, (current_us / duration_us) * 100)
                    _snipcut_update(job_id, cfr_progress=pct)
            except (ValueError, ZeroDivisionError):
                pass
    process.wait()
    if process.returncode != 0:
        stderr = process.stderr.read() if process.stderr else "Unknown"
        if os.path.exists(output_path):
            os.remove(output_path)
        raise RuntimeError(f"FFmpeg CFR conversion failed: {stderr[-300:]}")

    _snipcut_update(job_id, cfr_progress=100.0, cfr_output_path=output_path)
    return output_path, target_fps


def snipcut_extract_audio(input_path: str, audio_path: str):
    """Extract 16kHz mono PCM WAV for Whisper from the raw input."""
    cmd = ["ffmpeg", "-y", "-i", input_path,
           "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
           audio_path]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        raise RuntimeError(f"Audio extract failed: {result.stderr[-300:]}")


_faster_whisper_model = None
_faster_whisper_lock = threading.Lock()


def _get_faster_whisper():
    global _faster_whisper_model
    if _faster_whisper_model is None:
        with _faster_whisper_lock:
            if _faster_whisper_model is None:
                from faster_whisper import WhisperModel
                models_dir = str(APP_DIR / "faster_whisper_models")
                print("  Loading faster-whisper medium model...")
                _faster_whisper_model = WhisperModel("medium", device="cpu", compute_type="int8",
                                                     download_root=models_dir)
                print("  faster-whisper ready.")
    return _faster_whisper_model


def snipcut_transcribe(audio_path: str, job_id: str, duration: float) -> list:
    """Transcribe with faster-whisper, word-level. Returns list of {word, start, end}."""
    model = _get_faster_whisper()
    segments, _ = model.transcribe(audio_path, language="en", word_timestamps=True)

    words = []
    for seg in segments:
        if seg.words:
            for w in seg.words:
                words.append({"word": w.word.strip(), "start": float(w.start), "end": float(w.end)})
            # Progress: latest end / duration
            if duration > 0 and seg.end:
                pct = min(98.0, (seg.end / duration) * 100)
                _snipcut_update(job_id, transcribe_progress=pct)
    _snipcut_update(job_id, transcribe_progress=100.0)
    return words


def snipcut_detect_silence(words: list, threshold: float = 2.5) -> list:
    """Find gaps between words longer than threshold seconds."""
    gaps = []
    for i in range(len(words) - 1):
        gap_start = words[i]["end"]
        gap_end = words[i + 1]["start"]
        duration = gap_end - gap_start
        if duration > threshold:
            gaps.append({"start": round(gap_start, 2), "end": round(gap_end, 2), "duration": round(duration, 2)})
    return gaps


def snipcut_detect_silence_ffmpeg(audio_path: str, threshold_db: int = -35,
                                   min_duration: float = 2.5) -> list:
    """Run ffmpeg's silencedetect filter and return gaps as [{start, end, duration}].

    Used by silence-only mode to skip Whisper transcription entirely —
    much faster for 'just kill dead air' workflows.
    """
    cmd = [
        "ffmpeg", "-i", audio_path,
        "-af", f"silencedetect=n={threshold_db}dB:d={min_duration}",
        "-f", "null", "-",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    # silencedetect writes to stderr
    stderr = result.stderr

    gaps = []
    current_start = None
    for line in stderr.splitlines():
        line = line.strip()
        if "silence_start:" in line:
            try:
                current_start = float(line.split("silence_start:")[1].strip().split()[0])
            except (ValueError, IndexError):
                current_start = None
        elif "silence_end:" in line and current_start is not None:
            try:
                end_part = line.split("silence_end:")[1].strip().split("|")[0].strip()
                end = float(end_part.split()[0])
                duration = end - current_start
                if duration >= min_duration:
                    gaps.append({
                        "start": round(current_start, 2),
                        "end": round(end, 2),
                        "duration": round(duration, 2),
                    })
            except (ValueError, IndexError):
                pass
            current_start = None
    return gaps


def _snipcut_analyze_chunk(words_chunk: list, api_key: str) -> list:
    """Analyze one chunk of words (~10 min) with Claude. Returns list of cuts."""
    def fmt(w):
        m = int(w["start"] // 60)
        s = w["start"] % 60
        return f"[{m}:{s:05.2f}] {w['word']}"

    transcript_text = "\n".join(fmt(w) for w in words_chunk)

    prompt = f"""You are a video editor analyzing a transcript with word-level timestamps. The speaker is recording a talking-head video about finance/stocks.

Identify segments to CUT from the video.

CUT these:
1. **Filler words**: um, uh, ah, like (as filler), you know, I mean, sort of, kind of, basically, actually, right (as filler tag), so (sentence-starter filler)
2. **Repeated takes**: speaker starts a thought, stumbles/restarts. ALWAYS keep the SECOND attempt, CUT the FIRST.

Do NOT cut: intentional pauses, "like" as comparison, rhetorical questions, natural speech rhythm.

TRANSCRIPT:
{transcript_text}

Respond with a JSON array of cuts ONLY — no wrapper object, just the array:
[
  {{"start": 12.45, "end": 12.90, "reason": "filler", "content": "um"}},
  {{"start": 45.20, "end": 52.80, "reason": "repeated_take", "content": "First attempt of: ..."}}
]

If nothing needs cutting, return: []"""

    import anthropic
    text = anthropic.Anthropic(api_key=api_key).messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=16000,
        messages=[{"role": "user", "content": prompt}],
    ).content[0].text.strip()

    # Extract JSON array
    match = re.search(r'\[[\s\S]*\]', text)
    if match:
        text = match.group(0)

    try:
        result = json.loads(text)
        return result if isinstance(result, list) else result.get("cuts", [])
    except json.JSONDecodeError:
        # Try to fix common issues: trailing comma before ]
        cleaned = re.sub(r',\s*\]', ']', text)
        cleaned = re.sub(r',\s*\}', '}', cleaned)
        try:
            result = json.loads(cleaned)
            return result if isinstance(result, list) else result.get("cuts", [])
        except json.JSONDecodeError:
            print(f"  SnipCut: JSON parse failed for chunk. Response: {text[:200]}")
            return []


def snipcut_analyze(words: list, api_key: str) -> dict:
    """Analyze transcript in ~10-minute chunks, merge results."""
    if not api_key or not words:
        return {"cuts": [], "reasoning": "No API key or transcript" if not api_key else "Empty transcript"}

    # Chunk words into ~10 min segments with 30s overlap
    chunk_duration = 600  # 10 minutes
    overlap = 30  # seconds
    chunks = []
    chunk_start_idx = 0

    while chunk_start_idx < len(words):
        chunk_end_time = words[chunk_start_idx]["start"] + chunk_duration
        chunk_end_idx = chunk_start_idx
        while chunk_end_idx < len(words) and words[chunk_end_idx]["start"] < chunk_end_time:
            chunk_end_idx += 1
        # Add overlap for context
        overlap_end = chunk_end_time + overlap
        actual_end_idx = chunk_end_idx
        while actual_end_idx < len(words) and words[actual_end_idx]["start"] < overlap_end:
            actual_end_idx += 1
        chunks.append(words[chunk_start_idx:actual_end_idx])
        chunk_start_idx = chunk_end_idx  # next chunk starts where this one ended (not overlap)

    all_cuts = []
    for i, chunk in enumerate(chunks):
        print(f"  SnipCut: analyzing chunk {i+1}/{len(chunks)} ({len(chunk)} words, {chunk[0]['start']:.0f}s-{chunk[-1]['end']:.0f}s)")
        cuts = _snipcut_analyze_chunk(chunk, api_key)
        all_cuts.extend(cuts)

    # Deduplicate cuts that might overlap from chunk boundaries
    seen = set()
    unique = []
    for c in all_cuts:
        try:
            key = (round(float(c["start"]), 1), round(float(c["end"]), 1))
            if key not in seen:
                seen.add(key)
                unique.append(c)
        except (KeyError, ValueError, TypeError):
            continue

    reasoning = f"Analyzed {len(chunks)} chunk{'s' if len(chunks) > 1 else ''}, found {len(unique)} cuts"
    return {"cuts": unique, "reasoning": reasoning}


def snipcut_generate_metadata(words: list, api_key: str) -> dict:
    """Generate a YouTube title, description, and tags from the transcript."""
    if not api_key or not words:
        return {}

    # Build a condensed transcript (first ~8 min + last ~2 min for context)
    full_text = " ".join(w["word"] for w in words)
    # Truncate to ~6000 chars to stay well within context limits
    if len(full_text) > 6000:
        full_text = full_text[:4500] + "\n\n[...middle trimmed...]\n\n" + full_text[-1500:]

    prompt = f"""You are a YouTube content strategist. Given this transcript from a talking-head video about finance/stocks, generate metadata for publishing.

TRANSCRIPT:
{full_text}

Respond with a JSON object ONLY — no markdown, no explanation:
{{
  "title": "Compelling YouTube title (50-70 chars, no clickbait)",
  "description": "2-3 sentence description summarizing the key points. Write in third person. Include a call to action at the end.",
  "tags": ["tag1", "tag2", "...up to 12 relevant tags for YouTube SEO"]
}}"""

    try:
        import anthropic
        text = anthropic.Anthropic(api_key=api_key).messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}],
        ).content[0].text.strip()

        match = re.search(r'\{[\s\S]*\}', text)
        if match:
            text = match.group(0)
        result = json.loads(text)
        if isinstance(result, dict) and "title" in result:
            return result
    except Exception as e:
        print(f"  SnipCut: metadata generation failed: {e}")
    return {}


def snipcut_seconds_to_tc(seconds: float, fps: int = 30) -> str:
    """Convert seconds to HH:MM:SS:FF timecode for EDL."""
    total_frames = round(seconds * fps)
    ff = total_frames % fps
    total_seconds = total_frames // fps
    ss = total_seconds % 60
    total_minutes = total_seconds // 60
    mm = total_minutes % 60
    hh = total_minutes // 60
    return f"{hh:02d}:{mm:02d}:{ss:02d}:{ff:02d}"


def snipcut_snap_cuts_to_words(cuts: list, words: list, buffer_seconds: float = 0.05) -> list:
    """Snap each cut's start/end to actual Whisper word boundaries.

    Claude's timestamps are drawn from Whisper word timings but can drift
    ±100-200ms, occasionally slicing mid-word. This function:
      1. Finds all words whose midpoint falls inside the cut range.
      2. Snaps cut.start -> first matched word.start, cut.end -> last word.end.
      3. Adds a small breathing buffer (default 50ms) without eating adjacent words.
      4. Drops cuts that become invalid (start >= end).

    Cuts with no matching words (e.g. pure silence gaps) are left untouched.
    """
    if not words or not cuts:
        return cuts

    # Pre-sort words by start time for binary search
    sorted_words = sorted(words, key=lambda w: w.get("start", 0))
    starts = [w["start"] for w in sorted_words]

    import bisect
    snapped = []
    for c in cuts:
        try:
            c_start = float(c["start"])
            c_end = float(c["end"])
        except (KeyError, ValueError, TypeError):
            continue
        if c_end <= c_start:
            continue

        # Find words whose MIDPOINT falls inside [c_start, c_end]
        matched = []
        # Narrow the search: words whose start >= c_start - max_word_len, up to c_end
        lo = bisect.bisect_left(starts, c_start - 2.0)  # widen a bit for safety
        for i in range(lo, len(sorted_words)):
            w = sorted_words[i]
            if w["start"] > c_end:
                break
            mid = (w["start"] + w["end"]) / 2
            if c_start <= mid <= c_end:
                matched.append((i, w))

        if matched:
            first_idx, first_word = matched[0]
            last_idx, last_word = matched[-1]
            new_start = first_word["start"]
            new_end = last_word["end"]

            # Apply buffer without eating adjacent words
            prev_word = sorted_words[first_idx - 1] if first_idx > 0 else None
            next_word = sorted_words[last_idx + 1] if last_idx + 1 < len(sorted_words) else None
            new_start = max(new_start - buffer_seconds,
                            (prev_word["end"] + buffer_seconds) if prev_word else 0.0)
            new_end = new_end + buffer_seconds
            if next_word:
                new_end = min(new_end, next_word["start"] - buffer_seconds)

            if new_end > new_start:
                c = dict(c)  # don't mutate original
                c["start"] = round(new_start, 3)
                c["end"] = round(new_end, 3)
                snapped.append(c)
        else:
            # No matched words — keep as-is (likely a silence gap that was already clean)
            snapped.append(c)

    return snapped


def snipcut_merge_cuts(ai_cuts: list, silence_gaps: list, min_merge_gap: float = 0.2) -> list:
    """Merge AI cuts + silence gaps into a unified sorted cut list.
    Adjacent/overlapping cuts are consolidated."""
    all_cuts = []
    for c in ai_cuts:
        try:
            all_cuts.append({
                "start": float(c["start"]), "end": float(c["end"]),
                "reason": c.get("reason", "filler"),
            })
        except (KeyError, ValueError, TypeError):
            continue
    for g in silence_gaps:
        try:
            all_cuts.append({
                "start": float(g["start"]), "end": float(g["end"]),
                "reason": "silence",
            })
        except (KeyError, ValueError, TypeError):
            continue

    if not all_cuts:
        return []

    # Sort by start, then merge overlapping/adjacent
    all_cuts.sort(key=lambda c: c["start"])
    merged = [all_cuts[0]]
    for c in all_cuts[1:]:
        last = merged[-1]
        if c["start"] <= last["end"] + min_merge_gap:
            last["end"] = max(last["end"], c["end"])
            # Combine reasons if different
            if c["reason"] != last["reason"] and "merged" not in last["reason"]:
                last["reason"] = "merged"
        else:
            merged.append(c)
    return merged


def snipcut_compute_keeps(cuts: list, total_duration: float) -> list:
    """Given sorted merged cuts, return the inverse 'keep' segments."""
    keeps = []
    cursor = 0.0
    for c in cuts:
        if c["start"] > cursor:
            keeps.append({"start": cursor, "end": c["start"]})
        cursor = max(cursor, c["end"])
    if cursor < total_duration:
        keeps.append({"start": cursor, "end": total_duration})
    # Filter out tiny segments (< 0.1s)
    return [k for k in keeps if (k["end"] - k["start"]) > 0.1]


def snipcut_generate_clean_transcript(words: list, cuts: list, output_path: str):
    """Reconstruct transcript text with cut words removed.
    A word is removed if its midpoint falls inside any cut range."""
    sorted_cuts = sorted(cuts, key=lambda c: c["start"])

    def in_any_cut(midpoint: float) -> bool:
        for c in sorted_cuts:
            if c["start"] <= midpoint <= c["end"]:
                return True
            if c["start"] > midpoint:
                break
        return False

    kept_words = []
    for w in words:
        mid = (w["start"] + w["end"]) / 2
        if not in_any_cut(mid):
            kept_words.append(w["word"].strip())

    # Simple reflow: join words with spaces, wrap at ~80 chars
    text = " ".join(kept_words)
    lines = []
    current = []
    current_len = 0
    for word in text.split():
        if current_len + len(word) + 1 > 80 and current:
            lines.append(" ".join(current))
            current = [word]
            current_len = len(word)
        else:
            current.append(word)
            current_len += len(word) + 1
    if current:
        lines.append(" ".join(current))

    Path(output_path).write_text("\n".join(lines) + "\n")


SNIPCUT_MODES = ("full", "silence_only")
SNIPCUT_DECISIONS = ("pending", "keep", "cut")

SNIPCUT_REASON_EMOJI = {
    "silence": "🔵",
    "filler": "🟡",
    "repeated_take": "🔴",
    "merged": "🟣",
    "cut": "⚪",
}

SNIPCUT_REASON_COLOR = {
    "silence": "#3b82f6",
    "filler": "#eab308",
    "repeated_take": "#ef4444",
    "merged": "#a855f7",
    "cut": "#7a7a88",
}


def snipcut_reason_emoji(reason: str) -> str:
    return SNIPCUT_REASON_EMOJI.get(reason or "cut", "⚪")


def snipcut_generate_markers_txt(merged_cuts: list, output_path: str, title: str,
                                  duration: float, fps: int = 30):
    """Human-readable marker list — timecodes + reasons + content snippets.
    Non-destructive alternative to the cuts EDL: the user imports the CFR video
    manually, opens this file alongside, and jumps to each timecode to review."""
    def fmt_mm_ss(seconds: float) -> str:
        m = int(seconds // 60)
        s = int(seconds % 60)
        return f"{m}:{s:02d}"

    total_flagged = sum(max(0, c["end"] - c["start"]) for c in merged_cuts)
    lines = [
        f"SnipCut Markers — {title}",
        f"Video: {fmt_mm_ss(duration)} @ {fps}fps · {len(merged_cuts)} markers · {fmt_mm_ss(total_flagged)} flagged",
        "",
        "Legend:  🔵 Silence   🟡 Filler   🔴 Repeated take   🟣 Merged",
        "",
        "Review each marker in DaVinci Resolve — jump to the timecode and cut manually if needed.",
        "",
    ]
    for c in merged_cuts:
        tc = snipcut_seconds_to_tc(c["start"], fps)
        reason_raw = (c.get("reason") or "cut")
        emoji = snipcut_reason_emoji(reason_raw)
        if reason_raw == "silence":
            gap = c["end"] - c["start"]
            reason = f"SILENCE {gap:.1f}s"
            content = "(gap)"
        else:
            reason = reason_raw.upper().replace("_", " ")
            content_raw = (c.get("content") or "").strip()
            content = f'"{content_raw[:100]}"' if content_raw else ""
        lines.append(f" {emoji} {tc}  {reason:<16} {content}")
    Path(output_path).write_text("\n".join(lines) + "\n")


def snipcut_generate_edl(keeps: list, edl_path: str, title: str, source_name: str = "AX", fps: int = 30):
    """Write a CMX 3600 EDL file.
    Each 'keep' segment becomes an event; record timecodes are sequential."""
    lines = [f"TITLE: {title}", "FCM: NON-DROP FRAME", ""]
    record_cursor = 0.0
    for i, keep in enumerate(keeps, start=1):
        src_in = snipcut_seconds_to_tc(keep["start"], fps)
        src_out = snipcut_seconds_to_tc(keep["end"], fps)
        rec_in = snipcut_seconds_to_tc(record_cursor, fps)
        duration = keep["end"] - keep["start"]
        record_cursor += duration
        rec_out = snipcut_seconds_to_tc(record_cursor, fps)
        # Event format: EDIT# REEL CHANNELS TRANSITION SRC_IN SRC_OUT REC_IN REC_OUT
        lines.append(f"{i:03d}  {source_name:<8} AA/V  C        {src_in} {src_out} {rec_in} {rec_out}")
    lines.append("")
    Path(edl_path).write_text("\n".join(lines))


def _get_resolve_script_module():
    """Load DaVinciResolveScript. Returns module or None if unavailable."""
    script_api = "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting"
    script_lib = "/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion/fusionscript.so"
    if not os.path.exists(script_api):
        return None
    os.environ.setdefault("RESOLVE_SCRIPT_API", script_api)
    os.environ.setdefault("RESOLVE_SCRIPT_LIB", script_lib)
    modules_dir = os.path.join(script_api, "Modules")
    if modules_dir not in sys.path:
        sys.path.insert(0, modules_dir)
    try:
        import DaVinciResolveScript as dvr  # type: ignore
        return dvr
    except ImportError:
        return None


def snipcut_open_in_resolve(cfr_path: str, edl_path: str, project_name: str) -> dict:
    """Launch/connect to DaVinci Resolve and import the clip + EDL.
    Returns {ok, message} or {error, message}."""
    if not os.path.exists(cfr_path):
        return {"error": "CFR file not found"}
    if not os.path.exists(edl_path):
        return {"error": "EDL file not found"}

    dvr = _get_resolve_script_module()
    if dvr is None:
        return {"error": "DaVinci Resolve scripting module not found. Is Resolve installed?"}

    # Connect, or launch if not running
    resolve = dvr.scriptapp("Resolve")
    if resolve is None:
        # Try launching Resolve
        subprocess.Popen(["open", "-a", "DaVinci Resolve"])
        # Wait up to 45s for API to respond
        import time
        for _ in range(45):
            time.sleep(1)
            resolve = dvr.scriptapp("Resolve")
            if resolve is not None:
                break
        if resolve is None:
            return {"error": "DaVinci Resolve didn't respond. Is External Scripting enabled in Preferences → System?"}

    try:
        pm = resolve.GetProjectManager()
        if pm is None:
            return {"error": "Could not get ProjectManager"}

        # Create project with unique name if needed
        final_name = project_name
        project = pm.CreateProject(final_name)
        if project is None:
            # Name already taken — try appending a number
            for n in range(2, 20):
                candidate = f"{project_name} ({n})"
                project = pm.CreateProject(candidate)
                if project is not None:
                    final_name = candidate
                    break
            if project is None:
                # Try loading the existing project
                if pm.LoadProject(project_name):
                    project = pm.GetCurrentProject()
                    final_name = project_name

        if project is None:
            return {"error": f"Could not create or open project '{project_name}'"}

        mp = project.GetMediaPool()
        if mp is None:
            return {"error": "Could not get MediaPool"}

        # Import the CFR video
        media = mp.ImportMedia([cfr_path])
        if not media:
            return {"error": "Failed to import CFR file into media pool"}

        # Import EDL as a timeline
        timeline = mp.ImportTimelineFromFile(edl_path)
        if timeline is None:
            return {"error": "Project created + media imported, but EDL import failed. Import manually from File → Import Timeline → Pre-Conformed EDL."}

        return {"ok": True, "project": final_name, "message": f"Opened in DaVinci Resolve as project '{final_name}'"}
    except Exception as e:
        return {"error": f"Resolve API error: {str(e)[:200]}"}


def snipcut_populate_markers_table(job_id: str, merged_cuts: list):
    """Insert one row into snipcut_markers per merged cut, all with decision='pending'.
    Clears any existing rows for this job first so retries produce a clean set."""
    rows = []
    for i, c in enumerate(merged_cuts):
        try:
            start = float(c.get("start", 0))
            end = float(c.get("end", 0))
        except (TypeError, ValueError):
            continue
        if end <= start:
            continue
        reason = (c.get("reason") or "cut").strip() or "cut"
        content = (c.get("content") or "").strip()[:500]
        rows.append((uuid.uuid4().hex[:12], job_id, i,
                     round(start, 3), round(end, 3), reason, content))

    conn = get_db()
    try:
        conn.execute("DELETE FROM snipcut_markers WHERE job_id = ?", (job_id,))
        conn.executemany(
            "INSERT INTO snipcut_markers (id, job_id, sort_order, start_seconds, end_seconds, "
            "reason, content, decision) VALUES (?, ?, ?, ?, ?, ?, 'pending')",
            rows
        )
        conn.commit()
    finally:
        conn.close()


def snipcut_generate_srt(merged_cuts: list, output_path: str):
    """Generate an SRT subtitle file — one short entry per flagged moment.
    Import into Resolve as a subtitle track for visual timeline markers."""
    lines = []
    for i, c in enumerate(merged_cuts, start=1):
        start = c.get("start", 0)
        end = c.get("end", 0)
        # Clamp subtitle to 2 seconds max so it doesn't cover other content
        sub_end = min(start + 2.0, end)
        reason = (c.get("reason") or "cut").upper().replace("_", " ")
        content = (c.get("content") or "").strip()
        if reason == "SILENCE":
            label = f"[SILENCE {end - start:.1f}s]"
        elif content:
            label = f"[{reason}] {content[:60]}"
        else:
            label = f"[{reason}]"
        lines.append(str(i))
        lines.append(f"{_srt_tc(start)} --> {_srt_tc(sub_end)}")
        lines.append(label)
        lines.append("")
    Path(output_path).write_text("\n".join(lines), encoding="utf-8")


def _srt_tc(seconds: float) -> str:
    """Format seconds as SRT timecode: HH:MM:SS,mmm"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def snipcut_generate_resolve_script(merged_cuts: list, output_fps: int,
                                     cfr_path: str = "", srt_path: str = "",
                                     project_name: str = "") -> str:
    """Generate a Lua script for Resolve's Console (Workspace → Console).
    Creates project, imports media, builds timeline, adds markers, imports SRT."""
    color_map = {
        "silence": "Cyan",
        "filler": "Yellow",
        "repeated_take": "Red",
        "merged": "Purple",
        "cut": "Blue",
    }

    # Escape paths for Lua string literals
    def lua_str(s):
        return s.replace("\\", "\\\\").replace('"', '\\"')

    marker_lua = []
    for c in merged_cuts:
        start = c.get("start", 0)
        end = c.get("end", 0)
        reason = (c.get("reason") or "cut")
        color = color_map.get(reason, "Blue")
        duration_frames = max(1, round((end - start) * output_fps))
        frame_id = round(start * output_fps)
        content = (c.get("content") or "").strip().replace('"', '\\"')[:80]
        name = reason.upper().replace("_", " ")
        note = f"gap {end - start:.1f}s" if reason == "silence" else (content or name)
        marker_lua.append(
            f'tl:AddMarker({frame_id}, "{color}", "{name}", "{note}", {duration_frames})'
        )

    lines = [
        "-- SnipCut: paste into Resolve Console (Workspace > Console)",
        'resolve = Resolve()',
        'pm = resolve:GetProjectManager()',
        '',
        f'proj = pm:CreateProject("{lua_str(project_name)}")',
        'if not proj then',
        '  proj = pm:GetCurrentProject()',
        f'  print("Project exists, using: " .. proj:GetName())',
        'end',
        '',
    ]

    if cfr_path:
        lines += [
            f'ms = resolve:GetMediaStorage()',
            f'ms:AddItemListToMediaPool({{"{lua_str(cfr_path)}"}})',
            'mp = proj:GetMediaPool()',
            'clips = mp:GetRootFolder():GetClipList()',
            'if #clips > 0 then',
            '  tl = mp:CreateTimelineFromClips("Timeline 1", clips)',
            '  print("Timeline created with " .. #clips .. " clip(s)")',
        ]
    else:
        lines += [
            'tl = proj:GetCurrentTimeline()',
            'if tl then',
        ]

    if marker_lua:
        for m in marker_lua:
            lines.append(f'  {m}')
        lines.append(f'  print("Added {len(marker_lua)} markers")')

    if srt_path:
        lines += [
            f'  tl:ImportIntoTimeline("{lua_str(srt_path)}", {{}})',
            '  print("SRT subtitles imported")',
        ]

    lines += [
        'else',
        '  print("No timeline — import media first")',
        'end',
    ]

    return "\n".join(lines)


def snipcut_finalize_outputs(job_id: str, input_path: str, cfr_output: str,
                              ai_cuts: list, silence_gaps: list, words: list,
                              duration: float, output_fps: int,
                              write_transcript: bool = True) -> dict:
    """Generate EDL + markers.txt + optional clean transcript for a processed job.
    Re-probes CFR file for its real fps as a final safety check. Returns paths dict."""
    try:
        cfr_info = snipcut_probe(cfr_output)
        actual_fps = round(cfr_info["fps"]) or output_fps
        if actual_fps != output_fps:
            print(f"  SnipCut: CFR file is {actual_fps}fps (expected {output_fps}) — using actual")
            output_fps = actual_fps
            _snipcut_update(job_id, output_fps=output_fps)
    except Exception as e:
        print(f"  SnipCut: couldn't re-probe CFR ({e}), using {output_fps}fps")

    merged_cuts = snipcut_merge_cuts(ai_cuts, silence_gaps)
    keeps = snipcut_compute_keeps(merged_cuts, duration)
    title = Path(input_path).stem

    edl_path = str(Path(cfr_output).with_suffix(".edl"))
    snipcut_generate_edl(keeps, edl_path, title, fps=output_fps)

    markers_path = str(Path(input_path).with_name(f"{title}_markers.txt"))
    snipcut_generate_markers_txt(merged_cuts, markers_path, title, duration, fps=output_fps)

    transcript_path = ""
    if write_transcript and words:
        transcript_path = str(Path(input_path).with_name(f"{title}_transcript.txt"))
        snipcut_generate_clean_transcript(words, merged_cuts, transcript_path)

    srt_path = str(Path(input_path).with_name(f"{title}_markers.srt"))
    snipcut_generate_srt(merged_cuts, srt_path)

    resolve_script = snipcut_generate_resolve_script(
        merged_cuts, output_fps,
        cfr_path=cfr_output, srt_path=srt_path,
        project_name=title,
    )

    snipcut_populate_markers_table(job_id, merged_cuts)

    return {"edl_path": edl_path, "markers_path": markers_path,
            "transcript_path": transcript_path, "srt_path": srt_path,
            "resolve_script": resolve_script}


def snipcut_process(job_id: str):
    """Main orchestrator with checkpoint resume.
    Reads DB state to skip completed steps — if CFR + transcript already exist
    from a previous run that errored later, jumps straight to analysis."""
    import concurrent.futures

    conn = get_db()
    try:
        job = row_to_dict(conn.execute(
            "SELECT * FROM snipcut_jobs WHERE id = ?", (job_id,)
        ).fetchone())
    finally:
        conn.close()

    if not job:
        return

    input_path = job["input_path"]
    cfr_output = str(Path(input_path).with_name(f"{Path(input_path).stem}_cfr{Path(input_path).suffix}"))
    audio_path = str(APP_DIR / f"snipcut_audio_{job_id}.wav")
    mode = (job.get("mode") or "full").strip() or "full"

    try:
        # ── Checkpoint: probe ──
        has_probe = job["duration_seconds"] and job["duration_seconds"] > 0
        if has_probe:
            info = {"duration": job["duration_seconds"], "is_vfr": bool(job["is_vfr"]),
                    "width": job["width"], "height": job["height"]}
            print(f"  SnipCut: resuming — probe data exists ({info['duration']:.0f}s)")
        else:
            _snipcut_update(job_id, status="probing")
            info = snipcut_probe(input_path)
            _snipcut_update(job_id, duration_seconds=info["duration"],
                            is_vfr=1 if info["is_vfr"] else 0,
                            width=info["width"], height=info["height"])

        # ── Silence-only fast mode: skip Whisper + Claude entirely ──
        if mode == "silence_only":
            has_cfr = job["cfr_output_path"] and os.path.exists(job["cfr_output_path"])
            output_fps = job.get("output_fps") or 30

            if not has_cfr:
                _snipcut_update(job_id, status="processing")
                cfr_output, output_fps = snipcut_convert_cfr(input_path, cfr_output, job_id)
                _snipcut_update(job_id, output_fps=output_fps)
            else:
                cfr_output = job["cfr_output_path"]
                if not job.get("output_fps"):
                    try:
                        cfr_info = snipcut_probe(cfr_output)
                        output_fps = round(cfr_info["fps"]) or 30
                        _snipcut_update(job_id, output_fps=output_fps)
                    except Exception:
                        pass

            has_silence = job["silence_gaps_json"] and len(job["silence_gaps_json"]) > 2
            if has_silence:
                silence_gaps = json.loads(job["silence_gaps_json"])
                print(f"  SnipCut (silence-only): resuming — {len(silence_gaps)} gaps exist")
            else:
                _snipcut_update(job_id, status="detecting_silence", transcribe_progress=50.0)
                snipcut_extract_audio(input_path, audio_path)
                silence_gaps = snipcut_detect_silence_ffmpeg(audio_path, threshold_db=-35, min_duration=2.5)
                _snipcut_update(job_id, silence_gaps_json=json.dumps(silence_gaps),
                                transcribe_progress=100.0)
                if os.path.exists(audio_path):
                    try: os.remove(audio_path)
                    except OSError: pass

            _snipcut_update(job_id, cuts_json="[]", status="generating_edl")
            paths = snipcut_finalize_outputs(
                job_id, input_path, cfr_output,
                ai_cuts=[], silence_gaps=silence_gaps, words=[],
                duration=info["duration"], output_fps=output_fps,
                write_transcript=False,
            )
            _snipcut_update(job_id, edl_path=paths["edl_path"],
                            markers_path=paths["markers_path"],
                            srt_path=paths["srt_path"],
                            resolve_script=paths["resolve_script"],
                            status="done")
            return

        # ── Checkpoint: CFR + transcript ──
        has_cfr = job["cfr_output_path"] and os.path.exists(job["cfr_output_path"])
        has_transcript = job["transcript_json"] and len(job["transcript_json"]) > 10

        # Determine output fps — needed for EDL timecode generation
        output_fps = job.get("output_fps") or 30

        if has_cfr and has_transcript:
            cfr_output = job["cfr_output_path"]
            words = json.loads(job["transcript_json"])
            # Re-probe to confirm fps (in case an older job is missing output_fps)
            if not job.get("output_fps"):
                try:
                    cfr_info = snipcut_probe(cfr_output)
                    output_fps = round(cfr_info["fps"]) or 30
                    _snipcut_update(job_id, output_fps=output_fps)
                except Exception:
                    pass
            print(f"  SnipCut: resuming — CFR + transcript exist ({len(words)} words, {output_fps}fps)")
        else:
            # Need to run at least one of CFR or transcribe
            _snipcut_update(job_id, status="processing")

            if has_cfr:
                cfr_output = job["cfr_output_path"]
                print(f"  SnipCut: resuming — CFR exists, only transcribing")
            elif os.path.exists(cfr_output):
                # CFR file exists on disk but wasn't recorded in DB
                _snipcut_update(job_id, cfr_output_path=cfr_output, cfr_progress=100.0)
                has_cfr = True
                print(f"  SnipCut: found existing CFR file, skipping conversion")

            # If CFR already exists (by any means), probe it for fps
            if has_cfr and not job.get("output_fps"):
                try:
                    cfr_info = snipcut_probe(cfr_output)
                    output_fps = round(cfr_info["fps"]) or 30
                    _snipcut_update(job_id, output_fps=output_fps)
                except Exception:
                    pass

            def cfr_task():
                return snipcut_convert_cfr(input_path, cfr_output, job_id)

            def transcribe_task():
                snipcut_extract_audio(input_path, audio_path)
                return snipcut_transcribe(audio_path, job_id, info["duration"])

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                futures = {}
                if not has_cfr:
                    futures["cfr"] = pool.submit(cfr_task)
                if not has_transcript:
                    futures["tx"] = pool.submit(transcribe_task)

                if "cfr" in futures:
                    _, output_fps = futures["cfr"].result()
                    _snipcut_update(job_id, output_fps=output_fps)
                if "tx" in futures:
                    words = futures["tx"].result()
                elif has_transcript:
                    words = json.loads(job["transcript_json"])

            # Clean up audio
            if os.path.exists(audio_path):
                try: os.remove(audio_path)
                except OSError: pass

            _snipcut_update(job_id, transcript_json=json.dumps(words))

        # ── Checkpoint: silence gaps ──
        has_silence = job["silence_gaps_json"] and len(job["silence_gaps_json"]) > 2
        if has_silence:
            silence_gaps = json.loads(job["silence_gaps_json"])
            print(f"  SnipCut: resuming — silence gaps exist ({len(silence_gaps)} gaps)")
        else:
            silence_gaps = snipcut_detect_silence(words, threshold=2.5)
            _snipcut_update(job_id, silence_gaps_json=json.dumps(silence_gaps))

        # ── Checkpoint: Claude analysis ──
        has_cuts = job["cuts_json"] and len(job["cuts_json"]) > 2
        if has_cuts:
            ai_cuts = json.loads(job["cuts_json"])
            print(f"  SnipCut: resuming — AI cuts exist ({len(ai_cuts)} cuts)")
        else:
            _snipcut_update(job_id, status="analyzing")
            api_key = load_config().get("api_key", "")
            result = snipcut_analyze(words, api_key)
            # Snap to Whisper word edges with 50ms buffer so we never slice mid-word.
            ai_cuts = snipcut_snap_cuts_to_words(result.get("cuts", []), words)
            _snipcut_update(job_id, cuts_json=json.dumps(ai_cuts),
                            analysis_reasoning=result.get("reasoning", ""))

        # ── Checkpoint: metadata (title/desc/tags) ──
        has_metadata = job.get("metadata_json") and len(job.get("metadata_json", "")) > 10
        if not has_metadata:
            api_key = load_config().get("api_key", "")
            metadata = snipcut_generate_metadata(words, api_key)
            if metadata:
                _snipcut_update(job_id, metadata_json=json.dumps(metadata))
                print(f"  SnipCut: generated metadata — {metadata.get('title', '')[:60]}")

        _snipcut_update(job_id, status="generating_edl")
        paths = snipcut_finalize_outputs(
            job_id, input_path, cfr_output,
            ai_cuts=ai_cuts, silence_gaps=silence_gaps, words=words,
            duration=info["duration"], output_fps=output_fps,
            write_transcript=True,
        )
        _snipcut_update(job_id, edl_path=paths["edl_path"],
                        transcript_path=paths["transcript_path"],
                        markers_path=paths["markers_path"],
                        srt_path=paths["srt_path"],
                        resolve_script=paths["resolve_script"],
                        status="done")

    except Exception as e:
        if os.path.exists(audio_path):
            try: os.remove(audio_path)
            except OSError: pass
        _snipcut_update(job_id, status="error", error_text=str(e)[:400])


# ---------- Flask Routes ----------

@app.route("/")
def index():
    # Read from file on each request so HTML/JS/CSS changes are live without restart
    for candidate in [Path.cwd() / "index.html", APP_DIR / "index.html"]:
        if candidate.exists():
            return candidate.read_text()
    return "index.html not found", 404

@app.route("/api/check")
def api_check():
    missing = check_system_deps()
    return jsonify({"ok": len(missing) == 0, "missing": missing})

# -- Config --

@app.route("/api/config", methods=["GET"])
def api_get_config():
    config = load_config()
    if config.get("api_key"):
        key = config["api_key"]
        config["api_key_preview"] = key[:8] + "..." + key[-4:] if len(key) > 12 else "***"
        config["has_api_key"] = True
    else:
        config["api_key_preview"] = ""
        config["has_api_key"] = False
    del config["api_key"]
    return jsonify(config)

@app.route("/api/config", methods=["PUT"])
def api_update_config():
    config = load_config()
    data = request.json
    for key in ["api_key", "default_clip_window", "target_duration", "whisper_model", "output_dir", "channel_profile", "streambuddy_url", "streambuddy_token"]:
        if key in data:
            config[key] = data[key]
    save_config(config)
    return jsonify({"ok": True})

# -- Sessions --

@app.route("/api/sessions", methods=["POST"])
def api_create_session():
    data = request.json
    url = data.get("url", "").strip()
    text = data.get("text", "").strip()
    if not url:
        return jsonify({"error": "YouTube URL is required"}), 400

    session_id = uuid.uuid4().hex[:12]
    config = load_config()
    default_duration = config.get("default_clip_window", 5) * 60

    # If timestamps are provided, skip Phase A scan — go straight to clips
    if text:
        parsed = parse_clip_entries(text, default_duration)
        if not parsed:
            return jsonify({"error": "No valid timestamps found"}), 400

        conn = get_db()
        try:
            conn.execute(
                "INSERT INTO sessions (id, youtube_url, gather_phase) VALUES (?, ?, 'collecting')",
                (session_id, url)
            )
            clip_ids = []
            for clip in parsed:
                clip_id = uuid.uuid4().hex[:10]
                conn.execute(
                    """INSERT INTO clips (id, session_id, note, center_seconds, window_seconds,
                       start_seconds, end_seconds, status)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 'queued')""",
                    (clip_id, session_id, clip["note"], clip["center"],
                     clip["duration"], clip["start"], clip["end"])
                )
                clip_ids.append(clip_id)
            conn.commit()
        finally:
            conn.close()

        # Parallel downloads
        for clip_id in clip_ids:
            threading.Thread(target=download_clip, args=(clip_id, url), daemon=True).start()

        return jsonify({"session_id": session_id, "clip_count": len(parsed), "mode": "direct"})

    # No timestamps — do Phase A scan
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO sessions (id, youtube_url, gather_phase) VALUES (?, ?, 'scanning')",
            (session_id, url)
        )
        conn.commit()
    finally:
        conn.close()

    threading.Thread(target=scan_session, args=(session_id,), daemon=True).start()
    return jsonify({"session_id": session_id, "mode": "scan"})

@app.route("/api/sessions", methods=["GET"])
def api_list_sessions():
    conn = get_db()
    try:
        rows = conn.execute("""
            SELECT s.*,
                   COUNT(c.id) AS clip_count,
                   SUM(CASE WHEN c.status = 'error' THEN 1 ELSE 0 END) AS error_count,
                   SUM(CASE WHEN c.status IN ('downloading','transcribing','analyzing','exporting') THEN 1 ELSE 0 END) AS active_count,
                   SUM(CASE WHEN c.status IN ('downloaded','transcribed','ready','exported') THEN 1 ELSE 0 END) AS done_count
            FROM sessions s
            LEFT JOIN clips c ON c.session_id = s.id
            GROUP BY s.id
            ORDER BY s.created_at DESC
        """).fetchall()
        # Fetch all clips for all sessions in one query (avoid N+1)
        all_clips = rows_to_list(conn.execute(
            """SELECT id, session_id, note, status, final_start, final_end,
                      window_seconds, export_file, generated_title
               FROM clips ORDER BY center_seconds"""
        ).fetchall())

        # Group clips by session_id
        clips_by_session = {}
        for c in all_clips:
            sid = c["session_id"]
            if sid not in clips_by_session:
                clips_by_session[sid] = []
            # Compute trimmed duration
            if c["final_start"] is not None and c["final_end"] is not None:
                c["trimmed_duration"] = round(c["final_end"] - c["final_start"])
            else:
                c["trimmed_duration"] = None
            clips_by_session[sid].append(c)

        sessions = []
        for r in rows:
            s = dict(r)
            # Remove large stream_captions from listing response
            s.pop("stream_captions", None)
            phase = s.get("gather_phase", "")
            if phase == "scanning":
                s["overall_status"] = "scanning"
            elif phase in ("selecting", "no_captions"):
                s["overall_status"] = "ready to gather"
            elif s["done_count"] == s["clip_count"] and s["clip_count"] > 0:
                s["overall_status"] = "done"
            elif s["error_count"] > 0:
                s["overall_status"] = "error"
            elif s["active_count"] > 0:
                s["overall_status"] = "processing"
            else:
                s["overall_status"] = "queued"
            s["clips"] = clips_by_session.get(s["id"], [])
            sessions.append(s)
    finally:
        conn.close()
    return jsonify({"sessions": sessions})

@app.route("/api/sessions/<session_id>")
def api_get_session(session_id):
    conn = get_db()
    try:
        session = row_to_dict(conn.execute(
            "SELECT id, youtube_url, video_title, gather_phase, created_at FROM sessions WHERE id = ?",
            (session_id,)
        ).fetchone())
        if not session:
            return jsonify({"error": "Session not found"}), 404

        clips = rows_to_list(conn.execute(
            """SELECT id, session_id, note, center_seconds, window_seconds,
               start_seconds, end_seconds, status, error_text, raw_file,
               ai_suggestion_start, ai_suggestion_end, ai_reasoning,
               final_start, final_end, export_file, created_at,
               generated_title, generated_description
               FROM clips WHERE session_id = ? ORDER BY center_seconds""",
            (session_id,)
        ).fetchall())
    finally:
        conn.close()

    for c in clips:
        c["start_hms"] = seconds_to_hms(c["start_seconds"])
        c["end_hms"] = seconds_to_hms(c["end_seconds"])
        c["center_hms"] = seconds_to_hms(c["center_seconds"])
        c["duration_min"] = round(c["window_seconds"] / 60, 1)

    session["clips"] = clips
    return jsonify(session)

@app.route("/api/clips/<clip_id>")
def api_get_clip(clip_id):
    conn = get_db()
    try:
        clip = row_to_dict(conn.execute(
            "SELECT * FROM clips WHERE id = ?", (clip_id,)
        ).fetchone())
    finally:
        conn.close()
    if not clip:
        return jsonify({"error": "Clip not found"}), 404
    clip["start_hms"] = seconds_to_hms(clip["start_seconds"])
    clip["end_hms"] = seconds_to_hms(clip["end_seconds"])
    clip["center_hms"] = seconds_to_hms(clip["center_seconds"])
    return jsonify(clip)

@app.route("/api/clips/<clip_id>/transcript")
def api_get_transcript(clip_id):
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT transcript_json FROM clips WHERE id = ?", (clip_id,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return jsonify({"error": "Clip not found"}), 404
    raw = row["transcript_json"]
    if not raw:
        return jsonify({"error": "No transcript available"}), 404
    return Response(raw, mimetype="application/json")

@app.route("/api/clips/<clip_id>/analyze", methods=["POST"])
def api_analyze_clip(clip_id):
    conn = get_db()
    try:
        clip = row_to_dict(conn.execute("SELECT id, status FROM clips WHERE id = ?", (clip_id,)).fetchone())
    finally:
        conn.close()
    if not clip:
        return jsonify({"error": "Clip not found"}), 404
    thread = threading.Thread(target=analyze_clip, args=(clip_id,), daemon=True)
    thread.start()
    return jsonify({"ok": True, "message": "Analysis started"})

@app.route("/api/clips/<clip_id>/video")
def api_clip_video(clip_id):
    conn = get_db()
    try:
        row = conn.execute("SELECT raw_file FROM clips WHERE id = ?", (clip_id,)).fetchone()
    finally:
        conn.close()
    if not row or not row["raw_file"]:
        return jsonify({"error": "No video file"}), 404
    try:
        return send_file(row["raw_file"], mimetype="video/mp4", conditional=True)
    except OSError:
        return jsonify({"error": "Video file not found"}), 404

@app.route("/api/clips/<clip_id>/waveform")
def api_clip_waveform(clip_id):
    conn = get_db()
    try:
        row = conn.execute("SELECT raw_file, session_id FROM clips WHERE id = ?", (clip_id,)).fetchone()
    finally:
        conn.close()
    if not row or not row["raw_file"]:
        return jsonify({"error": "No video file"}), 404

    # Check cache
    cache_dir = SESSIONS_DIR / row["session_id"] / "waveforms"
    cache_file = cache_dir / f"{clip_id}.json"
    if cache_file.exists():
        return Response(cache_file.read_text(), mimetype="application/json")

    # Generate waveform: extract mono audio as raw f32le samples
    raw_file = row["raw_file"]
    cmd = [
        "ffmpeg", "-i", raw_file,
        "-ac", "1", "-ar", "8000",
        "-f", "f32le", "-"
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=30)
        if result.returncode != 0:
            print(f"  ffmpeg waveform error: {result.stderr.decode(errors='replace')[:500]}")
            return jsonify({"error": "ffmpeg waveform extraction failed"}), 500
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return jsonify({"error": "ffmpeg not available"}), 500

    # Parse raw float samples and downsample to ~800 points
    raw_bytes = result.stdout
    num_samples = len(raw_bytes) // 4
    if num_samples == 0:
        return jsonify({"peaks": [], "duration": 0})

    samples = struct.unpack(f"<{num_samples}f", raw_bytes)
    target_points = 800
    chunk_size = max(1, num_samples // target_points)
    peaks = []
    for i in range(0, num_samples, chunk_size):
        chunk = samples[i:i + chunk_size]
        peaks.append(max(abs(s) for s in chunk))

    # Normalize
    max_peak = max(peaks) if peaks else 1.0
    if max_peak > 0:
        peaks = [round(p / max_peak, 3) for p in peaks]

    duration = num_samples / 8000.0  # sample rate is 8000
    waveform_data = json.dumps({"peaks": peaks, "duration": round(duration, 2)})

    # Cache
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(waveform_data)

    return Response(waveform_data, mimetype="application/json")

@app.route("/api/clips/<clip_id>/trim", methods=["PUT"])
def api_save_trim(clip_id):
    data = request.json
    start = data.get("start")
    end = data.get("end")
    if start is None or end is None:
        return jsonify({"error": "start and end required"}), 400
    conn = get_db()
    try:
        conn.execute(
            "UPDATE clips SET final_start = ?, final_end = ? WHERE id = ?",
            (float(start), float(end), clip_id)
        )
        conn.commit()
        # Compute and store trimmed transcript for faster copy generation later
        row = conn.execute(
            "SELECT raw_file, note, transcript_json FROM clips WHERE id = ?", (clip_id,)
        ).fetchone()
        if row and row["transcript_json"]:
            trimmed_tx = extract_trimmed_transcript(
                json.loads(row["transcript_json"]), float(start), float(end)
            )
            conn.execute(
                "UPDATE clips SET trimmed_transcript = ? WHERE id = ?",
                (trimmed_tx, clip_id)
            )
            conn.commit()
        # Cut a trimmed copy to the clips folder (fast stream-copy, no re-encode)
        row = conn.execute("SELECT raw_file, note, session_id FROM clips WHERE id = ?", (clip_id,)).fetchone()
        if row and row["raw_file"] and Path(row["raw_file"]).exists():
            out_dir = get_session_output_dir(row["session_id"])
            safe_note = sanitize_note(row["note"] or "clip")
            trimmed_path = out_dir / f"{safe_note}_{clip_id[:6]}_trimmed.mp4"
            cmd = [
                "ffmpeg", "-y",
                "-i", row["raw_file"],
                "-ss", str(float(start)),
                "-to", str(float(end)),
                "-c", "copy",
                str(trimmed_path),
            ]
            threading.Thread(
                target=lambda: subprocess.run(cmd, capture_output=True, timeout=60),
                daemon=True,
            ).start()
    finally:
        conn.close()
    return jsonify({"ok": True})

@app.route("/api/clips/<clip_id>/export", methods=["POST"])
def api_export_clip(clip_id):
    data = request.json or {}
    captions = data.get("captions", True)
    vertical = data.get("vertical", True)
    conn = get_db()
    try:
        clip = row_to_dict(conn.execute("SELECT id, status FROM clips WHERE id = ?", (clip_id,)).fetchone())
    finally:
        conn.close()
    if not clip:
        return jsonify({"error": "Clip not found"}), 404
    thread = threading.Thread(target=export_clip, args=(clip_id, captions, vertical), daemon=True)
    thread.start()
    return jsonify({"ok": True, "message": "Export started"})

@app.route("/api/sessions/<session_id>/export-all", methods=["POST"])
def api_export_all(session_id):
    data = request.json or {}
    captions = data.get("captions", True)
    vertical = data.get("vertical", True)
    thread = threading.Thread(target=export_all_clips, args=(session_id, captions, vertical), daemon=True)
    thread.start()
    return jsonify({"ok": True, "message": "Batch export started"})

@app.route("/api/clips/<clip_id>/retry", methods=["POST"])
def api_retry_clip(clip_id):
    conn = get_db()
    try:
        clip = row_to_dict(conn.execute("SELECT id, status FROM clips WHERE id = ?", (clip_id,)).fetchone())
    finally:
        conn.close()
    if not clip:
        return jsonify({"error": "Clip not found"}), 404
    thread = threading.Thread(target=retry_clip, args=(clip_id,), daemon=True)
    thread.start()
    return jsonify({"ok": True, "message": "Retry started"})

@app.route("/api/clips/<clip_id>/generate-copy", methods=["POST"])
def api_generate_copy(clip_id):
    result = generate_clip_copy(clip_id)
    if result is None:
        return jsonify({"error": "Generation failed — ensure clip has a transcript and API key is set"}), 400
    title, description = result
    return jsonify({"ok": True, "title": title, "description": description})

@app.route("/api/sessions/<session_id>/scan-status")
def api_scan_status(session_id):
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT gather_phase, video_title FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return jsonify({"error": "Session not found"}), 404
    return jsonify(dict(row))

@app.route("/api/sessions/<session_id>/suggestions")
def api_get_suggestions(session_id):
    conn = get_db()
    try:
        rows = rows_to_list(conn.execute(
            "SELECT * FROM suggestions WHERE session_id = ? ORDER BY sort_order",
            (session_id,)
        ).fetchall())
    finally:
        conn.close()
    return jsonify({"suggestions": rows})

@app.route("/api/suggestions/<suggestion_id>", methods=["PUT"])
def api_update_suggestion(suggestion_id):
    data = request.json or {}
    if "selected" not in data:
        return jsonify({"error": "selected field required"}), 400
    conn = get_db()
    try:
        conn.execute(
            "UPDATE suggestions SET selected = ? WHERE id = ?",
            (1 if data["selected"] else 0, suggestion_id)
        )
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True})

@app.route("/api/sessions/<session_id>/add-segment", methods=["POST"])
def api_add_segment(session_id):
    data = request.json or {}
    timestamp_raw = data.get("timestamp", "").strip()
    note = data.get("note", "").strip() or "clip"
    window = int(data.get("window_seconds", 300))

    try:
        ts = parse_timestamp(timestamp_raw)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    conn = get_db()
    try:
        max_order = conn.execute(
            "SELECT COALESCE(MAX(sort_order), -1) FROM suggestions WHERE session_id = ?",
            (session_id,)
        ).fetchone()[0]
        suggestion_id = uuid.uuid4().hex[:10]
        conn.execute(
            """INSERT INTO suggestions
               (id, session_id, sort_order, timestamp_seconds, suggested_title,
                source, note, window_seconds, selected)
               VALUES (?, ?, ?, ?, ?, 'manual', ?, ?, 1)""",
            (suggestion_id, session_id, max_order + 1, ts, note, note, window)
        )
        conn.commit()
        row = row_to_dict(conn.execute(
            "SELECT * FROM suggestions WHERE id = ?", (suggestion_id,)
        ).fetchone())
    finally:
        conn.close()
    return jsonify({"ok": True, "suggestion": row})

@app.route("/api/sessions/<session_id>/segments/<suggestion_id>", methods=["DELETE"])
def api_delete_segment(session_id, suggestion_id):
    conn = get_db()
    try:
        conn.execute(
            "DELETE FROM suggestions WHERE id = ? AND session_id = ?",
            (suggestion_id, session_id)
        )
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True})

@app.route("/api/sessions/<session_id>/gather", methods=["POST"])
def api_gather_session(session_id):
    conn = get_db()
    try:
        exists = conn.execute("SELECT id FROM sessions WHERE id = ?", (session_id,)).fetchone()
    finally:
        conn.close()
    if not exists:
        return jsonify({"error": "Session not found"}), 404
    threading.Thread(target=gather_session, args=(session_id,), daemon=True).start()
    return jsonify({"ok": True})

@app.route("/api/clips/<clip_id>/copy", methods=["PUT"])
def api_save_copy(clip_id):
    data = request.json or {}
    conn = get_db()
    try:
        conn.execute(
            "UPDATE clips SET generated_title = ?, generated_description = ? WHERE id = ?",
            (data.get("title", ""), data.get("description", ""), clip_id)
        )
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True})

@app.route("/api/clips/<clip_id>", methods=["DELETE"])
def api_delete_clip(clip_id):
    """Delete a single clip and its files. Only allowed for exported or ready clips."""
    conn = get_db()
    try:
        clip = row_to_dict(conn.execute(
            "SELECT id, session_id, status, raw_file, export_file FROM clips WHERE id = ?",
            (clip_id,)
        ).fetchone())
        if not clip:
            return jsonify({"error": "Clip not found"}), 404
        if clip["status"] not in ("exported", "ready"):
            return jsonify({"error": "Can only delete exported or ready clips"}), 400

        # Delete files
        for path_key in ("raw_file", "export_file"):
            p = clip.get(path_key)
            if p and Path(p).exists():
                Path(p).unlink(missing_ok=True)

        # Delete waveform cache
        waveform_cache = SESSIONS_DIR / clip["session_id"] / "waveforms" / f"{clip_id}.json"
        if waveform_cache.exists():
            waveform_cache.unlink(missing_ok=True)

        # Delete DB record
        conn.execute("DELETE FROM clips WHERE id = ?", (clip_id,))
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True})

@app.route("/api/sessions/<session_id>", methods=["DELETE"])
def api_delete_session(session_id):
    conn = get_db()
    try:
        conn.execute("DELETE FROM clips WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        conn.commit()
    finally:
        conn.close()
    session_dir = SESSIONS_DIR / session_id
    if session_dir.exists():
        shutil.rmtree(session_dir, ignore_errors=True)
    return jsonify({"ok": True})

@app.route("/api/parse", methods=["POST"])
def api_parse():
    data = request.json
    text = data.get("text", "")
    default_duration = int(data.get("default_duration", 5)) * 60
    clips = parse_clip_entries(text, default_duration)
    return jsonify({"clips": clips})

@app.route("/api/open-folder", methods=["POST"])
def api_open_folder():
    data = request.json
    # Support both direct path and session_id
    if data.get("session_id"):
        folder = str(SESSIONS_DIR / data["session_id"] / "raw")
    else:
        folder = data.get("path", str(OUTPUT_DIR))
    if sys.platform == "darwin":
        subprocess.Popen(["open", folder])
    elif sys.platform == "win32":
        subprocess.Popen(["explorer", folder])
    else:
        subprocess.Popen(["xdg-open", folder])
    return jsonify({"ok": True})


# ---------- SnipCut Routes ----------

@app.route("/api/snipcut/jobs", methods=["POST"])
def api_snipcut_create():
    data = request.json or {}
    input_path = (data.get("input_path") or "").strip().strip("'\"")
    if not input_path:
        return jsonify({"error": "input_path required"}), 400
    if not os.path.exists(input_path):
        return jsonify({"error": f"File not found: {input_path}"}), 400
    SUPPORTED_VIDEO_EXTS = (".mp4", ".mkv", ".mov", ".webm", ".avi", ".ts", ".mts", ".flv", ".m4v", ".wmv")
    if not input_path.lower().endswith(SUPPORTED_VIDEO_EXTS):
        return jsonify({"error": f"Unsupported format. Accepted: {', '.join(SUPPORTED_VIDEO_EXTS)}"}), 400

    mode = (data.get("mode") or "full").strip()
    if mode not in SNIPCUT_MODES:
        mode = "full"

    # Check if already processed
    cfr_output = str(Path(input_path).with_name(f"{Path(input_path).stem}_cfr{Path(input_path).suffix}"))
    if os.path.exists(cfr_output):
        return jsonify({"error": f"Output already exists: {Path(cfr_output).name}. Delete it first."}), 400

    job_id = uuid.uuid4().hex[:12]
    filename = Path(input_path).name

    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO snipcut_jobs (id, input_path, input_filename, status, mode) VALUES (?, ?, ?, 'queued', ?)",
            (job_id, input_path, filename, mode)
        )
        conn.commit()
    finally:
        conn.close()

    threading.Thread(target=snipcut_process, args=(job_id,), daemon=True).start()
    return jsonify({"ok": True, "job_id": job_id})


@app.route("/api/snipcut/jobs/<job_id>")
def api_snipcut_get(job_id):
    conn = get_db()
    try:
        job = row_to_dict(conn.execute(
            "SELECT * FROM snipcut_jobs WHERE id = ?", (job_id,)
        ).fetchone())
    finally:
        conn.close()
    if not job:
        return jsonify({"error": "Job not found"}), 404

    # Don't send large transcript in the main poll; client fetches separately if needed
    job.pop("transcript_json", None)
    return jsonify(job)


@app.route("/api/snipcut/jobs")
def api_snipcut_list():
    conn = get_db()
    try:
        rows = rows_to_list(conn.execute(
            "SELECT id, input_filename, status, error_text, duration_seconds, is_vfr, "
            "cfr_output_path, cfr_progress, transcribe_progress, cuts_json, "
            "analysis_reasoning, edl_path, resolve_status, transcript_path, output_fps, markers_path, "
            "mode, refined_edl_path, refined_markers_path, metadata_json, "
            "srt_path, resolve_script, created_at "
            "FROM snipcut_jobs ORDER BY created_at DESC LIMIT 20"
        ).fetchall())
    finally:
        conn.close()
    return jsonify({"jobs": rows})


@app.route("/api/snipcut/jobs/<job_id>/retry", methods=["POST"])
def api_snipcut_retry(job_id):
    """Resume a failed job from its last checkpoint."""
    conn = get_db()
    try:
        job = row_to_dict(conn.execute("SELECT id, status FROM snipcut_jobs WHERE id = ?", (job_id,)).fetchone())
    finally:
        conn.close()
    if not job:
        return jsonify({"error": "Job not found"}), 404
    if job["status"] not in ("error", "done"):
        return jsonify({"error": "Job is still running"}), 400
    # Clear error, keep all checkpointed data (cfr, transcript, silence, etc)
    _snipcut_update(job_id, status="queued", error_text="")
    threading.Thread(target=snipcut_process, args=(job_id,), daemon=True).start()
    return jsonify({"ok": True, "message": "Resuming from last checkpoint"})


@app.route("/api/snipcut/jobs/<job_id>/cleanup", methods=["POST"])
def api_snipcut_cleanup(job_id):
    """Clear large data (transcript, silence gaps) from DB but keep the job record + outputs."""
    conn = get_db()
    try:
        conn.execute(
            "UPDATE snipcut_jobs SET transcript_json = '', silence_gaps_json = '' WHERE id = ?",
            (job_id,)
        )
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True})


@app.route("/api/snipcut/jobs/<job_id>", methods=["DELETE"])
def api_snipcut_delete(job_id):
    conn = get_db()
    try:
        conn.execute("DELETE FROM snipcut_jobs WHERE id = ?", (job_id,))
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True})


@app.route("/api/snipcut/pick-file", methods=["POST"])
def api_snipcut_pick_file():
    """Open a native file dialog and return the selected path."""
    # pywebview windows list
    try:
        windows = webview.windows
        if windows:
            result = windows[0].create_file_dialog(
                webview.OPEN_DIALOG,
                file_types=("Video Files (*.mp4;*.mkv;*.mov;*.webm;*.avi;*.ts;*.mts;*.flv;*.m4v;*.wmv)",),
            )
            if result and len(result) > 0:
                return jsonify({"path": result[0]})
            return jsonify({"path": None, "cancelled": True})
        return jsonify({"error": "No window available"}), 500
    except Exception as e:
        return jsonify({"error": str(e)[:200]}), 500


@app.route("/api/snipcut/pending-file")
def api_snipcut_pending_file():
    """Return the file path passed via 'Open With ClipCutter' if not yet consumed."""
    if _pending_file["path"] and not _pending_file["consumed"]:
        path = _pending_file["path"]
        _pending_file["consumed"] = True
        return jsonify({"path": path})
    return jsonify({"path": None})


@app.route("/api/pending-session")
def api_pending_session():
    """Return a pending session from clipcutter:// URL scheme (StreamBuddy handoff)."""
    if _pending_session.get("youtube_url") and not _pending_session.get("consumed"):
        payload = {
            "youtube_url": _pending_session["youtube_url"],
            "timestamps": _pending_session.get("timestamps", ""),
            "title": _pending_session.get("title", ""),
        }
        _pending_session["consumed"] = True
        return jsonify(payload)
    return jsonify({"youtube_url": None})


@app.route("/api/fetch-from-streambuddy", methods=["POST"])
def api_fetch_from_streambuddy():
    """Fetch the most recent ended session from StreamBuddy."""
    data = request.json or {}
    base_url = (data.get("base_url") or "").strip().rstrip("/")
    token = (data.get("token") or "").strip()
    if not base_url:
        return jsonify({"error": "StreamBuddy URL not configured in Settings"}), 400
    if not base_url.startswith(("http://", "https://")):
        base_url = "https://" + base_url
    endpoint = f"{base_url}/api/sessions/recent"
    try:
        import requests as _rq
        headers = {"X-ClipCutter-Token": token} if token else {}
        r = _rq.get(endpoint, headers=headers, timeout=10)
        # Detect HTML responses (404 pages, error pages) instead of JSON
        content_type = r.headers.get("content-type", "")
        if "application/json" not in content_type:
            msg = f"{endpoint} returned {r.status_code} ({content_type or 'no content-type'})."
            if r.status_code == 404:
                msg += " Endpoint not deployed yet — wait for Railway to redeploy or verify the URL."
            return jsonify({"error": msg}), 502
        if r.status_code != 200:
            return jsonify({"error": f"StreamBuddy returned {r.status_code}: {r.text[:200]}"}), 502
        return jsonify(r.json())
    except Exception as e:
        return jsonify({"error": f"Fetch failed for {endpoint}: {e}"}), 502


@app.route("/api/snipcut/jobs/<job_id>/open-resolve", methods=["POST"])
def api_snipcut_open_resolve(job_id):
    """Copy Lua setup script to clipboard and launch DaVinci Resolve."""
    conn = get_db()
    try:
        job = row_to_dict(conn.execute(
            "SELECT resolve_script FROM snipcut_jobs WHERE id = ?",
            (job_id,)
        ).fetchone())
    finally:
        conn.close()
    if not job:
        return jsonify({"error": "Job not found"}), 404
    script = job.get("resolve_script") or ""
    if not script:
        return jsonify({"error": "No script generated for this job"}), 400

    # Copy script to clipboard via pbcopy
    try:
        proc = subprocess.run(["pbcopy"], input=script.encode("utf-8"), timeout=5)
    except Exception as e:
        return jsonify({"error": f"Clipboard copy failed: {e}"}), 500

    # Launch Resolve (non-blocking)
    try:
        subprocess.Popen(["open", "-a", "DaVinci Resolve"])
    except Exception:
        pass  # Not fatal — user can open Resolve manually

    return jsonify({"ok": True})


# ---------- SnipCut Interactive Review Routes ----------

@app.route("/api/snipcut/jobs/<job_id>/markers")
def api_snipcut_markers_list(job_id):
    """Return all markers for a job, ordered by sort_order."""
    conn = get_db()
    try:
        # Ensure job exists
        job = conn.execute("SELECT id FROM snipcut_jobs WHERE id = ?", (job_id,)).fetchone()
        if not job:
            return jsonify({"error": "Job not found"}), 404
        rows = rows_to_list(conn.execute(
            "SELECT id, sort_order, start_seconds, end_seconds, reason, content, decision "
            "FROM snipcut_markers WHERE job_id = ? ORDER BY sort_order ASC",
            (job_id,)
        ).fetchall())
    finally:
        conn.close()
    return jsonify({"markers": rows})


@app.route("/api/snipcut/markers/<marker_id>", methods=["PUT"])
def api_snipcut_marker_update(marker_id):
    """Update a single marker's decision."""
    data = request.json or {}
    decision = (data.get("decision") or "").strip()
    if decision not in SNIPCUT_DECISIONS:
        return jsonify({"error": f"decision must be one of {'|'.join(SNIPCUT_DECISIONS)}"}), 400
    conn = get_db()
    try:
        cur = conn.execute(
            "UPDATE snipcut_markers SET decision = ? WHERE id = ?",
            (decision, marker_id)
        )
        conn.commit()
        if cur.rowcount == 0:
            return jsonify({"error": "Marker not found"}), 404
    finally:
        conn.close()
    return jsonify({"ok": True})


@app.route("/api/snipcut/jobs/<job_id>/markers/bulk", methods=["PUT"])
def api_snipcut_markers_bulk(job_id):
    """Apply a list of decision updates in one request."""
    data = request.json or {}
    updates = data.get("updates") or []
    if not isinstance(updates, list):
        return jsonify({"error": "updates must be a list"}), 400
    conn = get_db()
    try:
        applied = 0
        for u in updates:
            mid = u.get("id")
            decision = u.get("decision")
            if not mid or decision not in SNIPCUT_DECISIONS:
                continue
            cur = conn.execute(
                "UPDATE snipcut_markers SET decision = ? WHERE id = ? AND job_id = ?",
                (decision, mid, job_id)
            )
            applied += cur.rowcount
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True, "applied": applied})


@app.route("/api/snipcut/jobs/<job_id>/export-refined", methods=["POST"])
def api_snipcut_export_refined(job_id):
    """Regenerate EDL + markers.txt using only markers where decision='cut'."""
    conn = get_db()
    try:
        job = row_to_dict(conn.execute(
            "SELECT * FROM snipcut_jobs WHERE id = ?", (job_id,)
        ).fetchone())
        if not job:
            return jsonify({"error": "Job not found"}), 404
        rows = rows_to_list(conn.execute(
            "SELECT start_seconds, end_seconds, reason, content "
            "FROM snipcut_markers WHERE job_id = ? AND decision = 'cut' ORDER BY sort_order ASC",
            (job_id,)
        ).fetchall())
    finally:
        conn.close()

    if not job.get("cfr_output_path") or not os.path.exists(job["cfr_output_path"]):
        return jsonify({"error": "CFR output not found"}), 400

    approved_cuts = [
        {"start": r["start_seconds"], "end": r["end_seconds"],
         "reason": r["reason"], "content": r["content"] or ""}
        for r in rows
    ]

    cfr_output = job["cfr_output_path"]
    input_path = job["input_path"]
    title = Path(input_path).stem
    duration = job.get("duration_seconds") or 0
    output_fps = job.get("output_fps") or 30

    keeps = snipcut_compute_keeps(approved_cuts, duration)
    refined_edl_path = str(Path(cfr_output).with_name(f"{title}_refined.edl"))
    snipcut_generate_edl(keeps, refined_edl_path, f"{title}_refined", fps=output_fps)

    refined_markers_path = str(Path(input_path).with_name(f"{title}_refined_markers.txt"))
    snipcut_generate_markers_txt(approved_cuts, refined_markers_path, f"{title} (refined)",
                                  duration, fps=output_fps)

    _snipcut_update(job_id, refined_edl_path=refined_edl_path,
                    refined_markers_path=refined_markers_path)

    return jsonify({
        "ok": True,
        "refined_edl_path": refined_edl_path,
        "refined_markers_path": refined_markers_path,
        "approved_count": len(approved_cuts),
        "kept_segments": len(keeps),
    })


@app.route("/api/snipcut/jobs/<job_id>/video")
def api_snipcut_video(job_id):
    """Stream the CFR video file for inline preview (supports byte-range)."""
    conn = get_db()
    try:
        job = row_to_dict(conn.execute(
            "SELECT cfr_output_path FROM snipcut_jobs WHERE id = ?", (job_id,)
        ).fetchone())
    finally:
        conn.close()
    if not job or not job.get("cfr_output_path"):
        return jsonify({"error": "Job not found"}), 404
    video_path = job["cfr_output_path"]
    if not os.path.exists(video_path):
        return jsonify({"error": "Video file missing"}), 404
    return send_file(video_path, mimetype="video/mp4", conditional=True)


# ---------- Entry Point ----------


# Pending file from "Open With → ClipCutter" (or drag-drop onto .app)
_pending_file = {"path": None, "consumed": False}

# Pending session from clipcutter:// URL scheme (StreamBuddy integration)
_pending_session = {"youtube_url": None, "timestamps": None, "title": None, "consumed": False}


def _parse_clipcutter_url(url: str) -> dict:
    """Parse a clipcutter:// URL into a session payload.
    Expected formats:
      clipcutter://session?url=<youtube>&timestamps=<base64_json>&title=<t>
      clipcutter://session?url=<youtube>&timestamps=<urlencoded_plain_text>
    """
    from urllib.parse import urlparse, parse_qs, unquote
    import base64

    try:
        parsed = urlparse(url)
        if parsed.scheme != "clipcutter":
            return {}
        qs = parse_qs(parsed.query)
        yt = (qs.get("url") or [""])[0]
        title = (qs.get("title") or [""])[0]
        ts_raw = (qs.get("timestamps") or [""])[0]
        timestamps = ""
        if ts_raw:
            # Try base64 JSON first, fall back to plain text
            try:
                decoded = base64.b64decode(ts_raw).decode("utf-8")
                # If it parses as JSON, format each entry as "H:MM:SS - note"
                try:
                    data = json.loads(decoded)
                    if isinstance(data, list):
                        lines = []
                        for item in data:
                            secs = int(item.get("elapsed_seconds", 0))
                            h = secs // 3600
                            m = (secs % 3600) // 60
                            s = secs % 60
                            tc = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
                            note = item.get("note", "")
                            lines.append(f"{tc} - {note}" if note else tc)
                        timestamps = "\n".join(lines)
                    else:
                        timestamps = decoded
                except json.JSONDecodeError:
                    timestamps = decoded
            except Exception:
                timestamps = unquote(ts_raw)
        return {"youtube_url": yt, "timestamps": timestamps, "title": title}
    except Exception as e:
        print(f"  URL parse error: {e}")
        return {}


def start_server():
    """Run Flask in a background thread."""
    app.run(host="127.0.0.1", port=5557, debug=False, use_reloader=False)


def main():
    # Initialize database
    init_db()

    # Initialize config
    if not CONFIG_PATH.exists():
        save_config(DEFAULT_CONFIG)

    # Check for file or URL scheme passed via sys.argv
    if len(sys.argv) > 1:
        candidate = sys.argv[1]
        if candidate.startswith("clipcutter://"):
            session = _parse_clipcutter_url(candidate)
            if session.get("youtube_url"):
                _pending_session.update(session)
                _pending_session["consumed"] = False
                print(f"  Queued StreamBuddy session: {session.get('title') or session['youtube_url']}")
        elif os.path.isfile(candidate) and any(candidate.lower().endswith(ext) for ext in (".mp4", ".mkv", ".mov", ".webm", ".avi", ".ts", ".mts", ".flv", ".m4v", ".wmv")):
            _pending_file["path"] = os.path.abspath(candidate)
            print(f"  Queued for SnipCut: {Path(candidate).name}")

    missing = check_system_deps()
    if missing:
        print(f"\n  Missing: {', '.join(missing)}")
        print("  The app will still launch but cutting won't work until you install them.\n")

    print(f"\n  ClipCutter v2")
    print(f"  Clips -> {OUTPUT_DIR}\n")

    # Start Flask in background
    server_thread = threading.Thread(target=start_server, daemon=True)
    server_thread.start()

    # Open native window
    window = webview.create_window(
        "ClipCutter",
        "http://127.0.0.1:5557",
        width=760,
        height=820,
        resizable=True,
        min_size=(500, 600),
    )

    webview.start()


if __name__ == "__main__":
    main()
