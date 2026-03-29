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
    required = {"flask": "flask", "webview": "pywebview", "whisper": "openai-whisper", "anthropic": "anthropic"}
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
    ]:
        try:
            conn.execute(migration)
        except sqlite3.OperationalError as e:
            if "duplicate column name" not in str(e):
                raise
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

def call_claude(api_key: str, prompt: str, max_tokens: int = 300) -> dict:
    """Call Claude Haiku, strip optional code fences, and return parsed JSON."""
    import anthropic
    text = anthropic.Anthropic(api_key=api_key).messages.create(
        model="claude-haiku-4-5-20251001",
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
        cmd = [
            *ytdlp_cmd,
            "--write-auto-subs",
            "--sub-lang", "en",
            "--skip-download",
            "--sub-format", "json3",
            "--print", "title",
            "-o", str(captions_base),
            "--no-playlist",
            url,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)

        # First stdout line is the video title (from --print title)
        video_title = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""

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

        suggestions_data = call_claude(api_key, prompt, max_tokens=2000)

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

def _run_whisper(audio_path: str, model_name: str = None) -> dict:
    """Run Whisper transcription, with fallback to 'base' model on tensor errors."""
    import whisper
    models_dir = str(APP_DIR / "whisper_models")

    if model_name is None:
        model_name = load_config().get("whisper_model", "base")

    model = get_whisper_model()
    try:
        return model.transcribe(audio_path, word_timestamps=True, language="en")
    except RuntimeError as e:
        if "size of tensor" in str(e) and model_name != "base":
            # Known Whisper tensor mismatch — retry with base model
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

        result = call_claude(api_key, prompt, max_tokens=400)
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
    for key in ["api_key", "default_clip_window", "target_duration", "whisper_model", "output_dir", "channel_profile"]:
        if key in data:
            config[key] = data[key]
    save_config(config)
    return jsonify({"ok": True})

# -- Sessions --

@app.route("/api/sessions", methods=["POST"])
def api_create_session():
    data = request.json
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "YouTube URL is required"}), 400

    session_id = uuid.uuid4().hex[:12]
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
    return jsonify({"session_id": session_id})

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
        sessions = []
        for r in rows:
            s = dict(r)
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


# ---------- Entry Point ----------


def start_server():
    """Run Flask in a background thread."""
    app.run(host="127.0.0.1", port=5557, debug=False, use_reloader=False)


def main():
    # Initialize database
    init_db()

    # Initialize config
    if not CONFIG_PATH.exists():
        save_config(DEFAULT_CONFIG)

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
