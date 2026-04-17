"""Pure utility functions — no Flask, no DB, no global state.
Safe to import from anywhere.
"""

import re
from pathlib import Path

# Regex patterns compiled once at module load — these run in tight loops when
# parsing pasted timestamp blocks, so avoiding per-call recompilation matters.
_FS_UNSAFE = re.compile(r'[^\w\s-]')
_FS_WHITESPACE = re.compile(r'\s+')
_TC_HMS = re.compile(r'^(\d+):(\d{1,2}):(\d{2})$')
_TC_MS = re.compile(r'^(\d+):(\d{2})$')
_TC_HOURS = re.compile(r'(\d+)\s*(?:hrs?|hours?|h)\b')
_TC_MINS = re.compile(r'(\d+)\s*(?:mins?|minutes?|m)\b')
_TC_SECS = re.compile(r'(\d+)\s*(?:secs?|seconds?|s)\b')
_CLIP_DURATION_MIN = re.compile(r'\|\s*(\d+)\s*(?:mins?|minutes?|m)\s*$')
_CLIP_DURATION_SEC = re.compile(r'\|\s*(\d+)\s*(?:secs?|seconds?|s)\s*$')
_CLIP_TS_NOTE_SPLIT = re.compile(r'\s*[-–]\s*')


def sanitize_note(note: str) -> str:
    """Convert a clip note to a filesystem-safe string."""
    safe = _FS_UNSAFE.sub('', note).strip()
    safe = _FS_WHITESPACE.sub('_', safe)
    return safe or "clip"


def resolve_user_path(raw: str) -> Path:
    """Resolve a user-supplied path (strip wrapping quotes, expand ~, follow
    symlinks) and confirm it points to an existing file.
    Raises FileNotFoundError if the path doesn't resolve or isn't a regular file.
    Hardens subprocess/ffmpeg args against traversal even though we use shell=False."""
    cleaned = (raw or "").strip().strip("'\"")
    if not cleaned:
        raise FileNotFoundError("empty path")
    try:
        resolved = Path(cleaned).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as e:
        raise FileNotFoundError(str(e))
    if not resolved.is_file():
        raise FileNotFoundError(f"not a file: {cleaned}")
    return resolved


def parse_timestamp(raw: str) -> int:
    """Parse '1:23', '1:23:45', '5m30s', '90s' style timestamps -> seconds."""
    raw = raw.strip().lower()

    m = _TC_HMS.match(raw)
    if m:
        h, mm, s = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return h * 3600 + mm * 60 + s

    m = _TC_MS.match(raw)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        # Heuristic: '1:30' reads as 1h30m but '60:00' reads as 60m (a > 9 => minutes)
        return a * 60 + b if a > 9 else a * 3600 + b * 60

    total = 0
    hr_match = _TC_HOURS.search(raw)
    min_match = _TC_MINS.search(raw)
    sec_match = _TC_SECS.search(raw)

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
    """Parse pasted timestamp lines into clip dicts with start/end/duration.
    Format per line: 'timestamp - note [| Nm|Ns]'"""
    clips = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue

        duration = default_duration
        duration_match = _CLIP_DURATION_MIN.search(line)
        if duration_match:
            duration = int(duration_match.group(1)) * 60
            line = line[:duration_match.start()].strip()
        else:
            duration_match_s = _CLIP_DURATION_SEC.search(line)
            if duration_match_s:
                duration = int(duration_match_s.group(1))
                line = line[:duration_match_s.start()].strip()

        split_match = _CLIP_TS_NOTE_SPLIT.split(line, maxsplit=1)
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
