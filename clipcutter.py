#!/usr/bin/env python3
"""
ClipCutter v3 — livestream clip prep.
Capture timestamps during your show (Live tab), then one click turns the
Potential Clips into DaVinci-ready CFR files. Convert tab CFR-converts any
recording. No AI, no editing — extraction and conversion only.
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
    required = {"flask": "flask", "webview": "pywebview"}
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

# HD-only format chain for every YouTube download. YouTube's pre-muxed
# progressive MP4s cap at ~360p, so they are deliberately excluded — if no
# adaptive HD stream is available the download fails loudly instead of
# silently producing an unusable clip. h264+aac first (merge is a pure
# remux); any-codec merge as fallback (export re-encodes to h264 anyway).
YTDLP_HD_FORMAT = ("bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]"
                   "/bestvideo[height<=1080]+bestaudio")

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
    seconds_to_hms,
)
from cc_log import log

app = Flask(__name__)

# ---------- Clip Workers ----------

def _classify_download_error(output: str) -> str:
    """Turn a yt-dlp/ffmpeg failure tail into a short, actionable message.
    The raw output is often a giant URL fragment that means nothing to a user."""
    low = (output or "").lower()
    # "Invalid data / exit 183" on a live/DVR URL is almost always transient:
    # right after a stream ends, YouTube is still finalizing the recording and
    # its fragments can't be cut yet. It clears on its own within minutes.
    # (Not a livestream limitation — live clipping works fine once finalized.)
    if ("invalid data found" in low or "exited with code 183" in low
            or "playlist_type/dvr" in low or "force_finished" in low):
        return ("Couldn't cut this section yet. If the stream just ended, YouTube "
                "is still finalizing it — wait a few minutes and Retry. Or use the "
                "local recording (Get Clips → Local recording).")
    if "http error 429" in low or "too many requests" in low:
        return "YouTube rate-limited the download. Wait a few minutes and Retry, or use the local recording."
    if "video unavailable" in low or "private video" in low or "members-only" in low:
        return "This video isn't downloadable (private, members-only, or removed). Use the local recording."
    if "requested format is not available" in low:
        return "No HD format available for this video. Use the local recording."
    # Fall back to the raw tail, but keep it short.
    tail = (output or "Download failed").strip()
    return tail[-300:]


def download_clip(clip_id: str, url: str):
    """Download one clip segment from YouTube, then auto-export. Runs in a thread."""
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
            # HD adaptive streams only. YouTube's pre-muxed progressive MP4s
            # cap at ~360p — never acceptable — so they are deliberately NOT
            # in the fallback chain. h264+aac preferred (remux only); any
            # codec merge second; fail loudly rather than degrade.
            "-f", YTDLP_HD_FORMAT,
            "--merge-output-format", "mp4",
            "-o", str(output_path),
            "--no-playlist",
            url,
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        combined_output = (result.stdout or "") + (result.stderr or "")
        if result.returncode != 0:
            log.error("Download failed (rc=%s). Output tail: %s", result.returncode, combined_output[-300:])

        if result.returncode == 0:
            conn.execute(
                "UPDATE clips SET status = 'ready', raw_file = ? WHERE id = ?",
                (str(output_path), clip_id)
            )
            conn.commit()
            conn.close()
            _finish_clip(clip_id, clip["session_id"])
        else:
            conn.execute(
                "UPDATE clips SET status = 'error', error_text = ? WHERE id = ?",
                (_classify_download_error(combined_output), clip_id)
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
    """Extract a clip segment from a local video file using ffmpeg -c copy
    (instant, lossless), then auto-export. Runs in a thread."""
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
            _finish_clip(clip_id, clip["session_id"])
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


# ---------- Export ----------

# One x264 encode at a time. Every clip worker chains into export the moment
# its extraction lands, so a 5-clip session would otherwise run 5 concurrent
# encodes — each starved to a fraction of the CPU until all of them blow the
# timeout. x264 already uses all cores; serializing is strictly faster.
_export_lock = threading.Lock()

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

        # Ordinal = this clip's position in the session's timeline, so files
        # sort in Finder the way they happened during the show.
        ordered = rows_to_list(conn.execute(
            "SELECT id FROM clips WHERE session_id = ? ORDER BY center_seconds, id",
            (clip["session_id"],)
        ).fetchall())
        ordinal = next((i + 1 for i, r in enumerate(ordered) if r["id"] == clip_id), 1)

        out_dir = get_session_output_dir(clip["session_id"])
        safe_note = sanitize_note(clip["note"] or "clip")[:60]
        # NN_note_raw.mp4 — numbered, clean note, "raw" marks the unedited cut.
        output_path = out_dir / f"{ordinal:02d}_{safe_note}_raw.mp4"

        cmd = [
            "ffmpeg", "-y",
            "-i", clip["raw_file"],
            # CRF 17: this export is a second encode generation (raw clip is
            # already compressed) and feeds DaVinci -> OpusClip -> YouTube,
            # each adding its own generation. Keep this one near-transparent.
            "-c:v", "libx264", "-preset", "fast", "-crf", "17",
            "-r", "30",
            "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart",
            str(output_path),
        ]

        with _export_lock:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)

        if result.returncode == 0:
            conn.execute(
                "UPDATE clips SET status = 'exported', export_file = ? WHERE id = ?",
                (str(output_path), clip_id)
            )
        else:
            error_msg = result.stderr[-500:] if result.stderr else "ffmpeg export failed"
            log.error("Export failed: %s", error_msg)
            conn.execute(
                "UPDATE clips SET status = 'error', error_text = ? WHERE id = ?",
                (f"Export failed: {error_msg[:400]}", clip_id)
            )
        conn.commit()
    except subprocess.TimeoutExpired:
        log.error("Export timed out for clip %s", clip_id)
        conn.execute(
            "UPDATE clips SET status = 'error', error_text = 'Export timed out (30 min) — Retry to try again' WHERE id = ?",
            (clip_id,)
        )
        conn.commit()
    except Exception as e:
        conn.execute(
            "UPDATE clips SET status = 'error', error_text = ? WHERE id = ?",
            (f"Export failed: {str(e)[:400]}", clip_id)
        )
        conn.commit()
    finally:
        conn.close()


def _finish_clip(clip_id: str, session_id: str):
    """Auto-export a downloaded/extracted clip to CFR MP4, then — if it was
    the last one in the session — open the output folder in Finder.
    One click on Get Clips ends with files on disk, no further steps."""
    export_clip(clip_id)

    with with_db() as conn:
        remaining = conn.execute(
            "SELECT COUNT(*) FROM clips WHERE session_id = ? "
            "AND status NOT IN ('exported', 'error')",
            (session_id,)
        ).fetchone()[0]
        if remaining > 0:
            return
        # Atomically claim the 'session finished' event so simultaneous
        # last-clip finishers don't open the folder twice.
        cur = conn.execute(
            "UPDATE sessions SET gather_phase = 'done' WHERE id = ? AND gather_phase != 'done'",
            (session_id,)
        )
        conn.commit()
        claimed = cur.rowcount > 0

    if claimed:
        out_dir = get_session_output_dir(session_id)
        log.info("Session %s complete — opening %s", session_id, out_dir)
        if sys.platform == "darwin":
            subprocess.Popen(["open", str(out_dir)])


# ---------- Retry ----------

def retry_clip(clip_id: str):
    """Re-attempt a failed clip: re-download if the raw file is gone, then
    auto-export. Re-arms the session-finished event so the folder opens
    again when the retry completes the set."""
    downloaded = False
    conn = get_db()
    try:
        clip = row_to_dict(conn.execute(
            "SELECT id, session_id, raw_file, start_seconds, end_seconds, note "
            "FROM clips WHERE id = ?", (clip_id,)
        ).fetchone())
        if not clip:
            return

        conn.execute(
            "UPDATE sessions SET gather_phase = 'collecting' WHERE id = ?",
            (clip["session_id"],)
        )
        conn.commit()

        if clip["raw_file"] and Path(clip["raw_file"]).exists():
            conn.execute(
                "UPDATE clips SET status = 'ready', error_text = '' WHERE id = ?",
                (clip_id,)
            )
            conn.commit()
            downloaded = True
        else:
            conn.execute(
                "UPDATE clips SET status = 'downloading', error_text = '' WHERE id = ?",
                (clip_id,)
            )
            conn.commit()

            session = row_to_dict(conn.execute(
                "SELECT youtube_url FROM sessions WHERE id = ?",
                (clip["session_id"],)
            ).fetchone())
            if not session or not session["youtube_url"]:
                conn.execute(
                    "UPDATE clips SET status = 'error', error_text = 'No YouTube URL to re-download from' WHERE id = ?",
                    (clip_id,)
                )
                conn.commit()
                return

            session_dir = SESSIONS_DIR / clip["session_id"] / "raw"
            session_dir.mkdir(parents=True, exist_ok=True)

            safe_note = sanitize_note(clip["note"] or "clip")
            output_path = session_dir / f"{safe_note}_{clip_id[:6]}.mp4"
            section_arg = f"*{seconds_to_hms(clip['start_seconds'])}-{seconds_to_hms(clip['end_seconds'])}"

            cmd = [
                *get_ytdlp_cmd(),
                "--download-sections", section_arg,
                "-f", YTDLP_HD_FORMAT,
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
                    conn.commit()
                    downloaded = True
                else:
                    combined = (result.stdout or "") + (result.stderr or "")
                    conn.execute(
                        "UPDATE clips SET status = 'error', error_text = ? WHERE id = ?",
                        (_classify_download_error(combined), clip_id)
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

    if downloaded and clip:
        _finish_clip(clip_id, clip["session_id"])

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
    config.pop("api_key", None)  # legacy key may linger in old config files
    return jsonify(config)

@app.route("/api/config", methods=["PUT"])
def api_update_config():
    config = load_config()
    data = request.json
    for key in ["default_clip_window", "output_dir"]:
        if key in data:
            config[key] = data[key]
    if "auto_trash_on_post" in data:
        config["auto_trash_on_post"] = bool(data["auto_trash_on_post"])
    save_config(config)
    return jsonify({"ok": True})

# -- Sessions --

def _create_clip_session(url: str, local_file: str, clip_specs: list, title: str = "") -> str:
    """Create a session + clips and kick off workers. Returns session_id.
    clip_specs: [{note, center, duration}] — start/end derived from center±half.
    Source: local ffmpeg extraction when local_file is set, else yt-dlp from url.
    title names the output folder (YYYY-MM-DD_Title)."""
    session_id = uuid.uuid4().hex[:12]
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO sessions (id, youtube_url, video_title, gather_phase) VALUES (?, ?, ?, 'collecting')",
            (session_id, url or "", title or "")
        )
        clip_ids = []
        for spec in clip_specs:
            center = max(0, int(spec["center"]))
            duration = int(spec["duration"])
            half = duration // 2
            start = max(0, center - half)
            end = center + half
            clip_id = uuid.uuid4().hex[:10]
            conn.execute(
                """INSERT INTO clips (id, session_id, note, center_seconds, window_seconds,
                   start_seconds, end_seconds, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'queued')""",
                (clip_id, session_id, spec.get("note", ""), center, duration, start, end)
            )
            clip_ids.append(clip_id)
        conn.commit()
    finally:
        conn.close()

    for clip_id in clip_ids:
        if local_file:
            threading.Thread(target=extract_clip_local, args=(clip_id, local_file), daemon=True).start()
        else:
            threading.Thread(target=download_clip, args=(clip_id, url), daemon=True).start()
    return session_id


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
                      window_seconds, export_file, generated_title, posted
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
               generated_title, generated_description, posted
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

@app.route("/api/clips/<clip_id>/posted", methods=["POST"])
def api_set_clip_posted(clip_id):
    """User-set 'posted to social' label. Pure bookkeeping — nothing in the
    clip pipeline reads it; it only feeds the raw/posted counts in the UI.

    Optionally (auto_trash_on_post, default off) moves this clip's own video
    files to the OS trash on a genuine not-posted -> posted flip. Un-posting
    never touches files. The DB paths are intentionally left untouched so a
    restore puts the files back exactly where they were."""
    posted = 1 if (request.json or {}).get("posted") else 0
    conn = get_db()
    try:
        clip = row_to_dict(conn.execute(
            "SELECT id, posted, raw_file, export_file FROM clips WHERE id = ?", (clip_id,)
        ).fetchone())
        if not clip:
            return jsonify({"error": "Clip not found"}), 404
        conn.execute("UPDATE clips SET posted = ? WHERE id = ?", (posted, clip_id))
        conn.commit()
    finally:
        conn.close()

    # Trash only on a real false -> true transition, and only for this clip.
    # Failures here must never fail the request: the label is already saved.
    trashed = []
    became_posted = posted == 1 and not clip["posted"]
    if became_posted and load_config().get("auto_trash_on_post"):
        for original in (clip["export_file"], clip["raw_file"]):
            in_trash = _trash_file(original)
            if in_trash:
                trashed.append({"original": original, "trashed": in_trash,
                                "name": Path(original).name})

    return jsonify({"ok": True, "posted": bool(posted), "trashed": trashed})


@app.route("/api/clips/<clip_id>/untrash", methods=["POST"])
def api_untrash_clip(clip_id):
    """Undo the auto-trash for one clip — restores each file from the trash."""
    items = (request.json or {}).get("trashed") or []
    restored = sum(1 for it in items
                   if _restore_from_trash(it.get("trashed", ""), it.get("original", "")))
    return jsonify({"ok": True, "restored": restored})

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

@app.route("/api/open-folder", methods=["POST"])
def api_open_folder():
    data = request.json
    # export_session_id -> the session's named export folder;
    # session_id -> its raw downloads; path -> explicit; default OUTPUT_DIR
    if data.get("export_session_id"):
        folder = str(get_session_output_dir(data["export_session_id"]))
    elif data.get("session_id"):
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


def _trash_file(path: str):
    """Move one file to the OS trash — recoverable, never a permanent delete.

    Returns its new path inside the trash (so it can be restored), or None if
    nothing was moved. Never raises: a missing path, a already-deleted file, an
    unsupported platform or an API failure are all logged and skipped so the
    caller (the Posted toggle) is never blocked.

    macOS only. pywebview already requires pyobjc-framework-Cocoa, so this uses
    the same NSFileManager API Finder does — no extra dependency. On other
    platforms it deliberately does nothing rather than falling back to any
    form of permanent deletion.
    """
    if not path:
        return None
    try:
        if not Path(path).exists():
            log.info("Auto-trash: file already gone, skipping: %s", path)
            return None
    except OSError as e:
        log.warning("Auto-trash: cannot stat %s: %s", path, e)
        return None

    if sys.platform != "darwin":
        log.info("Auto-trash: no trash API on %s, leaving file in place: %s",
                 sys.platform, path)
        return None

    try:
        from Foundation import NSFileManager, NSURL
        ok, resulting, err = NSFileManager.defaultManager() \
            .trashItemAtURL_resultingItemURL_error_(NSURL.fileURLWithPath_(path), None, None)
        if ok and resulting is not None:
            log.info("Auto-trash: moved to trash: %s", path)
            return resulting.path()
        log.warning("Auto-trash: failed for %s: %s", path, err)
    except Exception as e:                              # noqa: BLE001 - never block the toggle
        log.warning("Auto-trash: error for %s: %s", path, e)
    return None


def _restore_from_trash(trash_path: str, original_path: str) -> bool:
    """Undo one _trash_file: move it back to where it came from."""
    try:
        src, dst = Path(trash_path), Path(original_path)
        if not src.exists() or dst.exists():
            return False
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        log.info("Auto-trash: restored %s", original_path)
        return True
    except (OSError, shutil.Error) as e:
        log.warning("Auto-trash: restore failed for %s: %s", original_path, e)
        return False


@app.route("/api/open-url", methods=["POST"])
def api_open_url():
    """Open a URL in the user's default browser (not the pywebview window).
    Restricted to http(s) so the OS 'open' handler can't be pointed at a
    local file or app bundle."""
    url = ((request.json or {}).get("url") or "").strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        return jsonify({"error": "Only http(s) URLs can be opened"}), 400
    if sys.platform == "darwin":
        subprocess.Popen(["open", url])
    elif sys.platform == "win32":
        subprocess.Popen(["explorer", url])
    else:
        subprocess.Popen(["xdg-open", url])
    return jsonify({"ok": True})


# ---------- Live Show Routes (v3) ----------

def _show_state(show: dict) -> str:
    if show.get("ended_at"):
        return "ended"
    if show.get("started_at"):
        return "live"
    return "pre"


@app.route("/api/shows", methods=["POST"])
def api_create_show():
    data = request.json or {}
    title = (data.get("title") or "").strip()[:200]
    youtube_url = (data.get("youtube_url") or "").strip()
    if not title:
        return jsonify({"error": "Title required"}), 400
    show_id = uuid.uuid4().hex[:12]
    with with_db() as conn:
        conn.execute(
            "INSERT INTO shows (id, title, youtube_url) VALUES (?, ?, ?)",
            (show_id, title, youtube_url)
        )
        conn.commit()
    return jsonify({"ok": True, "show_id": show_id})


@app.route("/api/shows", methods=["GET"])
def api_list_shows():
    with with_db() as conn:
        # raw_clip_count / posted_count come from the generated session's
        # extracted clips (scalar subqueries so they don't fan out the
        # show_entries join). clip_count (potential-clip markers) is kept
        # unchanged for anything else that reads it.
        rows = rows_to_list(conn.execute("""
            SELECT s.*,
                   SUM(CASE WHEN e.type = 'timestamp' THEN 1 ELSE 0 END) AS timestamp_count,
                   SUM(CASE WHEN e.type = 'clip' THEN 1 ELSE 0 END) AS clip_count,
                   (SELECT COUNT(*) FROM clips c
                      WHERE c.session_id = s.generated_session_id) AS raw_clip_count,
                   (SELECT COUNT(*) FROM clips c
                      WHERE c.session_id = s.generated_session_id AND c.posted = 1) AS posted_count
            FROM shows s
            LEFT JOIN show_entries e ON e.show_id = s.id
            GROUP BY s.id
            ORDER BY s.created_at DESC
            LIMIT 30
        """).fetchall())
    for r in rows:
        r["state"] = _show_state(r)
        r["timestamp_count"] = r["timestamp_count"] or 0
        r["clip_count"] = r["clip_count"] or 0
        r["raw_clip_count"] = r["raw_clip_count"] or 0
        r["posted_count"] = r["posted_count"] or 0
    return jsonify({"shows": rows})


@app.route("/api/shows/<show_id>", methods=["GET"])
def api_get_show(show_id):
    with with_db() as conn:
        show = row_to_dict(conn.execute(
            "SELECT * FROM shows WHERE id = ?", (show_id,)
        ).fetchone())
        if not show:
            return jsonify({"error": "Show not found"}), 404
        entries = rows_to_list(conn.execute(
            "SELECT * FROM show_entries WHERE show_id = ? ORDER BY elapsed_seconds ASC",
            (show_id,)
        ).fetchall())
    show["state"] = _show_state(show)
    show["entries"] = entries
    return jsonify(show)


@app.route("/api/shows/<show_id>", methods=["PUT"])
def api_update_show(show_id):
    data = request.json or {}
    fields = {}
    if "title" in data and (data["title"] or "").strip():
        fields["title"] = data["title"].strip()[:200]
    if "youtube_url" in data:
        fields["youtube_url"] = (data["youtube_url"] or "").strip()
    if not fields:
        return jsonify({"error": "Nothing to update"}), 400
    with with_db() as conn:
        cols = ", ".join(f"{k} = ?" for k in fields)
        cur = conn.execute(f"UPDATE shows SET {cols} WHERE id = ?",
                           list(fields.values()) + [show_id])
        conn.commit()
        if cur.rowcount == 0:
            return jsonify({"error": "Show not found"}), 404
    return jsonify({"ok": True})


@app.route("/api/shows/<show_id>/go-live", methods=["POST"])
def api_show_go_live(show_id):
    from datetime import datetime, timezone
    with with_db() as conn:
        show = row_to_dict(conn.execute(
            "SELECT id, started_at, ended_at FROM shows WHERE id = ?", (show_id,)
        ).fetchone())
        if not show:
            return jsonify({"error": "Show not found"}), 404
        if show["started_at"]:
            return jsonify({"ok": True, "already_live": True})
        other = conn.execute(
            "SELECT id, title FROM shows WHERE started_at IS NOT NULL AND ended_at IS NULL AND id != ?",
            (show_id,)
        ).fetchone()
        if other:
            return jsonify({"error": f'"{other["title"]}" is already live. End it first.'}), 409
        now = datetime.now(timezone.utc).isoformat()
        conn.execute("UPDATE shows SET started_at = ? WHERE id = ?", (now, show_id))
        conn.commit()
    return jsonify({"ok": True, "started_at": now})


@app.route("/api/shows/<show_id>/end", methods=["POST"])
def api_show_end(show_id):
    from datetime import datetime, timezone
    with with_db() as conn:
        show = row_to_dict(conn.execute(
            "SELECT id, started_at, ended_at FROM shows WHERE id = ?", (show_id,)
        ).fetchone())
        if not show:
            return jsonify({"error": "Show not found"}), 404
        if not show["started_at"]:
            return jsonify({"error": "Show hasn't started"}), 400
        if show["ended_at"]:
            return jsonify({"ok": True, "already_ended": True})
        now = datetime.now(timezone.utc).isoformat()
        conn.execute("UPDATE shows SET ended_at = ? WHERE id = ?", (now, show_id))
        conn.commit()
    return jsonify({"ok": True, "ended_at": now})


@app.route("/api/shows/<show_id>", methods=["DELETE"])
def api_delete_show(show_id):
    with with_db() as conn:
        conn.execute("DELETE FROM shows WHERE id = ?", (show_id,))
        conn.commit()
    return jsonify({"ok": True})


@app.route("/api/shows/<show_id>/entries", methods=["POST"])
def api_add_show_entry(show_id):
    data = request.json or {}
    etype = data.get("type")
    if etype not in ("timestamp", "clip"):
        return jsonify({"error": "type must be timestamp|clip"}), 400
    try:
        elapsed = max(0, int(data.get("elapsed_seconds", 0)))
    except (TypeError, ValueError):
        return jsonify({"error": "elapsed_seconds must be an integer"}), 400
    note = (data.get("note") or "").strip()[:500]

    with with_db() as conn:
        show = conn.execute("SELECT id FROM shows WHERE id = ?", (show_id,)).fetchone()
        if not show:
            return jsonify({"error": "Show not found"}), 404
        entry_id = uuid.uuid4().hex[:12]
        conn.execute(
            "INSERT INTO show_entries (id, show_id, type, note, elapsed_seconds) VALUES (?, ?, ?, ?, ?)",
            (entry_id, show_id, etype, note, elapsed)
        )
        conn.commit()
    return jsonify({"ok": True, "entry_id": entry_id})


@app.route("/api/show-entries/<entry_id>", methods=["PUT"])
def api_update_show_entry(entry_id):
    data = request.json or {}
    fields = {}
    if "note" in data:
        fields["note"] = (data["note"] or "").strip()[:500]
    if "type" in data:
        if data["type"] not in ("timestamp", "clip"):
            return jsonify({"error": "type must be timestamp|clip"}), 400
        fields["type"] = data["type"]
    if "elapsed_seconds" in data:
        try:
            fields["elapsed_seconds"] = max(0, int(data["elapsed_seconds"]))
        except (TypeError, ValueError):
            return jsonify({"error": "elapsed_seconds must be an integer"}), 400
    if not fields:
        return jsonify({"error": "Nothing to update"}), 400
    with with_db() as conn:
        cols = ", ".join(f"{k} = ?" for k in fields)
        cur = conn.execute(f"UPDATE show_entries SET {cols} WHERE id = ?",
                           list(fields.values()) + [entry_id])
        conn.commit()
        if cur.rowcount == 0:
            return jsonify({"error": "Entry not found"}), 404
    return jsonify({"ok": True})


@app.route("/api/show-entries/<entry_id>", methods=["DELETE"])
def api_delete_show_entry(entry_id):
    with with_db() as conn:
        conn.execute("DELETE FROM show_entries WHERE id = ?", (entry_id,))
        conn.commit()
    return jsonify({"ok": True})


@app.route("/api/shows/<show_id>/get-clips", methods=["POST"])
def api_show_get_clips(show_id):
    """One-click: turn this show's clip-type entries into a clip session."""
    data = request.json or {}
    source = data.get("source")
    if source not in ("url", "local"):
        return jsonify({"error": "source must be url|local"}), 400
    try:
        offset = int(data.get("offset_seconds") or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "offset_seconds must be an integer"}), 400

    with with_db() as conn:
        show = row_to_dict(conn.execute(
            "SELECT * FROM shows WHERE id = ?", (show_id,)
        ).fetchone())
        if not show:
            return jsonify({"error": "Show not found"}), 404
        clip_entries = rows_to_list(conn.execute(
            "SELECT note, elapsed_seconds FROM show_entries "
            "WHERE show_id = ? AND type = 'clip' ORDER BY elapsed_seconds ASC",
            (show_id,)
        ).fetchall())

    if not show["ended_at"]:
        return jsonify({"error": "End the show first"}), 400
    if not clip_entries:
        return jsonify({"error": "No Potential Clips entries on this show"}), 400

    # Allow overriding the stored URL at get-clips time
    url = (data.get("youtube_url") or show.get("youtube_url") or "").strip()
    local_file = ""
    if source == "local":
        try:
            local_file = str(resolve_user_path(data.get("local_file") or ""))
        except FileNotFoundError as e:
            return jsonify({"error": f"File not found: {e}"}), 400
    elif not url:
        return jsonify({"error": "No YouTube URL on this show — add one or use a local file"}), 400

    default_duration = load_config().get("default_clip_window", 5) * 60
    session_id = _create_clip_session(url, local_file, [
        {"note": e["note"] or "clip", "center": e["elapsed_seconds"] + offset,
         "duration": default_duration}
        for e in clip_entries
    ], title=show["title"])

    with with_db() as conn:
        conn.execute("UPDATE shows SET generated_session_id = ?, youtube_url = ? WHERE id = ?",
                     (session_id, url, show_id))
        conn.commit()

    log.info("Show %s -> session %s (%d clips, offset %+ds, source=%s)",
             show_id, session_id, len(clip_entries), offset, source)
    return jsonify({"ok": True, "session_id": session_id, "clip_count": len(clip_entries)})


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


# ---------- Entry Point ----------


# Pending file from "Open With → ClipCutter" (or drag-drop onto .app)
_pending_file = {"path": None, "consumed": False}


def start_server():
    """Run Flask in a background thread."""
    app.run(host="127.0.0.1", port=5557, debug=False, use_reloader=False)


def main():
    # Initialize database
    init_db()

    # Initialize config
    if not CONFIG_PATH.exists():
        save_config(DEFAULT_CONFIG)

    # Check for file passed via sys.argv (right-click "Open With")
    if len(sys.argv) > 1:
        candidate = sys.argv[1]
        if os.path.isfile(candidate) and candidate.lower().endswith(SUPPORTED_VIDEO_EXTS):
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
