#!/usr/bin/env python3
"""
ClipCutter v2 — AI-powered livestream clip editor.
Paste timestamps, get transcribed clips with AI sizzle reel suggestions.
"""

import os
import sys
import json
import shutil
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
    required = {"flask": "flask", "webview": "pywebview", "anthropic": "anthropic", "requests": "requests"}
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
import webview

# ClipCutter modules (split out to keep this file navigable)
from cc_config import (
    APP_DIR, DB_PATH, CONFIG_PATH, SESSIONS_DIR, OUTPUT_DIR,
    SUPPORTED_VIDEO_EXTS, DEFAULT_CONFIG,
    load_config, save_config,
)
from cc_db import (
    get_db, with_db, init_db, row_to_dict, rows_to_list,
    get_session_output_dir, snipcut_update as _snipcut_update,
)
from cc_helpers import (
    sanitize_note, resolve_user_path,
    parse_timestamp, seconds_to_hms, parse_clip_entries,
)
from cc_log import log

app = Flask(__name__)

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
            # Prefer single pre-muxed MP4 (no merge step = faster).
            # Falls back to separate streams + merge if pre-muxed unavailable.
            "-f", "best[height<=1080][ext=mp4]/bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best",
            "--merge-output-format", "mp4",
            "-o", str(output_path),
            "--no-playlist",
            url,
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        combined_output = (result.stdout or "") + (result.stderr or "")
        if result.returncode != 0:
            log.warning("Download failed (rc=%s). Output tail: %s", result.returncode, combined_output[-300:])
            if "could not open encoder" in combined_output.lower() or "aac" in combined_output.lower():
                log.info("Retrying download: single-format, no keyframe forcing (AAC encoder error)")
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
                    log.error("Retry also failed: %s%s", (result.stdout or '')[-200:], (result.stderr or '')[-200:])

        if result.returncode == 0:
            conn.execute(
                "UPDATE clips SET status = 'ready', raw_file = ? WHERE id = ?",
                (str(output_path), clip_id)
            )
            conn.commit()
            conn.close()
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


def extract_clip_local(clip_id: str, source_path: str):
    """Extract a clip segment from a local video file using ffmpeg -c copy.
    Instant (no re-encoding). Chains to transcription on success."""
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

        cmd = [
            "ffmpeg", "-y",
            "-ss", str(clip["start_seconds"]),
            "-to", str(clip["end_seconds"]),
            "-i", source_path,
            "-c", "copy",
            "-movflags", "+faststart",
            str(output_path),
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode == 0 and output_path.exists():
            conn.execute(
                "UPDATE clips SET status = 'ready', raw_file = ? WHERE id = ?",
                (str(output_path), clip_id)
            )
            conn.commit()
            conn.close()
        else:
            error_msg = (result.stderr or "ffmpeg extraction failed")[-300:]
            conn.execute(
                "UPDATE clips SET status = 'error', error_text = ? WHERE id = ?",
                (error_msg, clip_id)
            )
            conn.commit()
            conn.close()
    except Exception as exc:
        try:
            conn.execute(
                "UPDATE clips SET status = 'error', error_text = ? WHERE id = ?",
                (f"Extraction failed: {str(exc)[:300]}", clip_id)
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


# ---------- Export ----------

def export_clip(clip_id: str):
    """Re-encode a clip's raw download to CFR MP4 in the output folder.
    No trim (full raw range), no captions, no vertical crop — just DaVinci-ready CFR."""
    conn = get_db()
    try:
        clip = row_to_dict(conn.execute(
            "SELECT id, session_id, note, raw_file, window_seconds "
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

        out_dir = get_session_output_dir(clip["session_id"])
        safe_note = sanitize_note(clip["note"] or "clip")
        output_path = out_dir / f"{safe_note}_{clip_id[:6]}.mp4"

        cmd = [
            "ffmpeg", "-y",
            "-i", clip["raw_file"],
            "-c:v", "libx264", "-preset", "fast", "-crf", "20",
            "-r", "30",
            "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart",
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
            log.error("Export failed: %s", error_msg)
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


def export_all_clips(session_id: str):
    """Export all ready clips in a session sequentially."""
    with with_db() as conn:
        clips = rows_to_list(conn.execute(
            "SELECT id FROM clips WHERE session_id = ? AND status = 'ready'",
            (session_id,)
        ).fetchall())
    for clip in clips:
        export_clip(clip["id"])


# ---------- Retry ----------

def retry_clip(clip_id: str):
    """Re-download a failed clip (skips if raw_file still exists)."""
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
            conn.execute(
                "UPDATE clips SET status = 'ready', error_text = '' WHERE id = ?",
                (clip_id,)
            )
            conn.commit()
            return

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
            return

        session_dir = SESSIONS_DIR / clip["session_id"] / "raw"
        session_dir.mkdir(parents=True, exist_ok=True)

        start_hms = seconds_to_hms(clip["start_seconds"])
        end_hms = seconds_to_hms(clip["end_seconds"])
        safe_note = sanitize_note(clip["note"] or "clip")
        output_path = session_dir / f"{safe_note}_{clip_id[:6]}.mp4"
        section_arg = f"*{start_hms}-{end_hms}"

        ytdlp_cmd = get_ytdlp_cmd()
        cmd = [
            *ytdlp_cmd,
            "--download-sections", section_arg,
            "-f", "best[height<=1080][ext=mp4]/bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best",
            "--merge-output-format", "mp4",
            "-o", str(output_path),
            "--no-playlist",
            session["youtube_url"],
        ]

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            if result.returncode == 0:
                conn.execute(
                    "UPDATE clips SET status = 'ready', raw_file = ? WHERE id = ?",
                    (str(output_path), clip_id)
                )
            else:
                error_msg = result.stderr[-500:] if result.stderr else "Download failed"
                conn.execute(
                    "UPDATE clips SET status = 'error', error_text = ? WHERE id = ?",
                    (error_msg, clip_id)
                )
            conn.commit()
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            conn.execute(
                "UPDATE clips SET status = 'error', error_text = ? WHERE id = ?",
                (str(e)[:400], clip_id)
            )
            conn.commit()
    except Exception as e:
        try:
            conn.execute(
                "UPDATE clips SET status = 'error', error_text = ? WHERE id = ?",
                (f"Retry failed: {str(e)[:400]}", clip_id)
            )
            conn.commit()
        except Exception:
            pass
    finally:
        try:
            conn.close()
        except Exception:
            pass

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
                    "UPDATE clips SET status = 'ready', raw_file = ? WHERE id = ?",
                    (str(output_path), clip_id)
                )
                conn.commit()
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


def snipcut_process(job_id: str):
    """Probe and CFR-convert a video. Checkpoint-safe: if the CFR file
    already exists we skip re-conversion, useful for retries."""
    with with_db() as conn:
        job = row_to_dict(conn.execute(
            "SELECT * FROM snipcut_jobs WHERE id = ?", (job_id,)
        ).fetchone())

    if not job:
        return

    input_path = job["input_path"]
    cfr_output = str(Path(input_path).with_name(f"{Path(input_path).stem}_cfr{Path(input_path).suffix}"))

    try:
        # ── Probe (skip if we already have duration on file) ──
        if not (job["duration_seconds"] and job["duration_seconds"] > 0):
            _snipcut_update(job_id, status="probing")
            info = snipcut_probe(input_path)
            _snipcut_update(job_id, duration_seconds=info["duration"],
                            is_vfr=1 if info["is_vfr"] else 0,
                            width=info["width"], height=info["height"])
        else:
            log.info("SnipCut: resuming — probe data exists (%.0fs)", job["duration_seconds"])

        # ── CFR convert (skip if output file already exists) ──
        if job["cfr_output_path"] and os.path.exists(job["cfr_output_path"]):
            log.info("SnipCut: CFR file already exists, skipping conversion")
            output_fps = job.get("output_fps") or 30
        elif os.path.exists(cfr_output):
            log.info("SnipCut: found existing CFR file on disk, recording in DB")
            output_fps = job.get("output_fps") or 30
            _snipcut_update(job_id, cfr_output_path=cfr_output, cfr_progress=100.0)
        else:
            _snipcut_update(job_id, status="processing")
            cfr_output, output_fps = snipcut_convert_cfr(input_path, cfr_output, job_id)
            _snipcut_update(job_id, output_fps=output_fps)

        _snipcut_update(job_id, status="done")

    except Exception as e:
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
    for key in ["api_key", "default_clip_window", "target_duration", "output_dir", "channel_profile", "streambuddy_url", "streambuddy_token"]:
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
    raw_local = data.get("local_file", "")

    if not url and not raw_local:
        return jsonify({"error": "YouTube URL or local file required"}), 400

    local_file = ""
    if raw_local:
        try:
            local_file = str(resolve_user_path(raw_local))
        except FileNotFoundError as e:
            return jsonify({"error": f"File not found: {e}"}), 400

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
                (session_id, url or "")
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

        if local_file:
            # Local file mode: extract clips with ffmpeg (~1s each, no download)
            for clip_id in clip_ids:
                threading.Thread(target=extract_clip_local, args=(clip_id, local_file), daemon=True).start()
        else:
            # URL mode: download clips from YouTube
            for clip_id in clip_ids:
                threading.Thread(target=download_clip, args=(clip_id, url), daemon=True).start()

        mode = "local" if local_file else "direct"
        return jsonify({"session_id": session_id, "clip_count": len(parsed), "mode": mode})

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
                   SUM(CASE WHEN c.status IN ('downloading','exporting') THEN 1 ELSE 0 END) AS active_count,
                   SUM(CASE WHEN c.status IN ('ready','exported') THEN 1 ELSE 0 END) AS done_count
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

@app.route("/api/clips/<clip_id>/export", methods=["POST"])
def api_export_clip(clip_id):
    with with_db() as conn:
        clip = row_to_dict(conn.execute("SELECT id FROM clips WHERE id = ?", (clip_id,)).fetchone())
    if not clip:
        return jsonify({"error": "Clip not found"}), 404
    threading.Thread(target=export_clip, args=(clip_id,), daemon=True).start()
    return jsonify({"ok": True, "message": "Export started"})

@app.route("/api/sessions/<session_id>/export-all", methods=["POST"])
def api_export_all(session_id):
    threading.Thread(target=export_all_clips, args=(session_id,), daemon=True).start()
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


@app.route("/api/open-url", methods=["POST"])
def api_open_url():
    """Open an external URL in the user's default browser."""
    data = request.json or {}
    url = (data.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return jsonify({"error": "http(s) URL required"}), 400
    if sys.platform == "darwin":
        subprocess.Popen(["open", url])
    elif sys.platform == "win32":
        subprocess.Popen(["cmd", "/c", "start", url], shell=False)
    else:
        subprocess.Popen(["xdg-open", url])
    return jsonify({"ok": True})


# ---------- SnipCut Routes ----------

@app.route("/api/snipcut/jobs", methods=["POST"])
def api_snipcut_create():
    data = request.json or {}
    raw_input = data.get("input_path") or ""
    if not raw_input.strip():
        return jsonify({"error": "input_path required"}), 400

    try:
        resolved = resolve_user_path(raw_input)
    except FileNotFoundError as e:
        return jsonify({"error": f"File not found: {e}"}), 400

    input_path = str(resolved)
    if not input_path.lower().endswith(SUPPORTED_VIDEO_EXTS):
        return jsonify({"error": f"Unsupported format. Accepted: {', '.join(SUPPORTED_VIDEO_EXTS)}"}), 400

    cfr_output = str(resolved.with_name(f"{resolved.stem}_cfr{resolved.suffix}"))
    if os.path.exists(cfr_output):
        return jsonify({"error": f"Output already exists: {Path(cfr_output).name}. Delete it first."}), 400

    job_id = uuid.uuid4().hex[:12]
    filename = resolved.name

    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO snipcut_jobs (id, input_path, input_filename, status) VALUES (?, ?, ?, 'queued')",
            (job_id, input_path, filename)
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
            "cfr_output_path, cfr_progress, output_fps, created_at "
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
                file_types=(f"Video Files ({';'.join(f'*{ext}' for ext in SUPPORTED_VIDEO_EXTS)})",),
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



# ---------- Entry Point ----------


# Pending file from "Open With → ClipCutter" (or drag-drop onto .app)
_pending_file = {"path": None, "consumed": False}

# Pending session from clipcutter:// URL scheme (StreamBuddy integration)
_pending_session = {"youtube_url": None, "timestamps": None, "title": None, "consumed": False}


# Realistic clipcutter:// URLs (url + base64-encoded timestamps + title)
# run ~2-5KB for dozens of shorts. 32KB leaves comfortable headroom while
# still cutting off garbage/hostile payloads before we decode them.
_CLIPCUTTER_URL_MAX_LEN = 32 * 1024
_CLIPCUTTER_TS_MAX_LEN = 24 * 1024
_CLIPCUTTER_TITLE_MAX_LEN = 500
_CLIPCUTTER_YT_MAX_LEN = 2048


def _parse_clipcutter_url(url: str) -> dict:
    """Parse a clipcutter:// URL into a session payload.
    Expected formats:
      clipcutter://session?url=<youtube>&timestamps=<base64_json>&title=<t>
      clipcutter://session?url=<youtube>&timestamps=<urlencoded_plain_text>
    """
    from urllib.parse import urlparse, parse_qs, unquote
    import base64

    # Size cap guards against accidental or hostile giant payloads that
    # would hang base64 decoding or json parsing before validation.
    if len(url) > _CLIPCUTTER_URL_MAX_LEN:
        log.warning("clipcutter:// URL exceeds %d bytes, ignoring", _CLIPCUTTER_URL_MAX_LEN)
        return {}

    try:
        parsed = urlparse(url)
        if parsed.scheme != "clipcutter":
            return {}
        qs = parse_qs(parsed.query)
        yt = (qs.get("url") or [""])[0][:_CLIPCUTTER_YT_MAX_LEN]
        title = (qs.get("title") or [""])[0][:_CLIPCUTTER_TITLE_MAX_LEN]
        ts_raw = (qs.get("timestamps") or [""])[0][:_CLIPCUTTER_TS_MAX_LEN]
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
        log.error("URL parse error: %s", e)
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
                log.info("Queued StreamBuddy session: %s", session.get('title') or session['youtube_url'])
        elif os.path.isfile(candidate) and candidate.lower().endswith(SUPPORTED_VIDEO_EXTS):
            _pending_file["path"] = os.path.abspath(candidate)
            log.info("Queued for SnipCut: %s", Path(candidate).name)

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
