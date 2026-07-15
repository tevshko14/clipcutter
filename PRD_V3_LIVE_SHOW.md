# ClipCutter v3 — Live Show Tab

**Status:** Approved for build
**Date:** 2026-04
**Replaces:** StreamBuddy (Railway deployment already off) + manual note-taking

---

## 1. Problem

The livestream shorts workflow currently spans two tools and a manual gap:

1. **During the show** — StreamBuddy (now retired) or manual notes capture "that was a good moment" timestamps.
2. **After the show** — timestamps get copied/pasted into ClipCutter, the recording is dropped in, clips are extracted, then edited in DaVinci → OpusClip.

The handoff between "notes taken during the show" and "clips extracted after the show" is friction: copy/paste, format mismatches, a second app to keep open, a URL to keep track of. StreamBuddy's collaborative features went unused — in practice this is a solo activity.

## 2. Goal

One app, one flow, zero handoffs:

> Open ClipCutter before the show → enter title + YouTube URL → Go Live → tap timestamps and potential clips as they happen → End Show → **one click** → clip files on disk, ready for DaVinci.

## 3. Non-Goals

- **No collaboration.** Solo capture only. No Supabase, no realtime, no share links. (The data model keeps a clean `shows`/`show_entries` split so sync could be added later, but nothing is built for it.)
- **No AI.** No transcription, no suggestions, no auto-detection of moments.
- **No YouTube auto-detect.** The URL is pasted manually (it can be added or corrected at any point, including after the show ends).
- **No editing features.** Extraction only — editing stays in DaVinci/OpusClip.

## 4. User Flow

### 4.1 Before the show
1. Open ClipCutter → **Live** tab (new, first tab).
2. Click **New Show** → enter show title (required) and YouTube URL (optional — can be added later).
3. The show sits in "pre-live" state. A **Go Live** button is displayed prominently.

### 4.2 Going live
4. When the stream actually starts, click **Go Live**. A local timer starts (persisted — derived from `started_at` in the DB, so restarting the app never loses the clock).

### 4.3 During the show
5. Two capture sections, always visible while live:
   - **Timestamps** — general moments for chapters/reference.
   - **Potential Clips** — moments that should become shorts.
6. To capture: click into the input and type a note, press **Enter**. The recorded time is the moment typing *started*, not the moment Enter was pressed (typing a note takes 5–15 seconds; capturing at first keystroke keeps timestamps accurate to the moment).
7. A small live chip next to the input shows the pending capture time ("@ 1:23:45") once typing begins. Clearing the input resets the pending capture.
8. Every entry appears in its section list immediately, newest visible, showing `h:mm:ss — note`.
9. Entries are editable in place at any time (fix a typo, adjust the time, delete, or flip an entry between Timestamp ↔ Clip).

### 4.4 Ending the show
10. Click **End Show** (prominent red button, confirmation dialog). Timer stops; `ended_at` is recorded.
11. Entries remain editable after ending (cleanup pass before extraction is normal).

### 4.5 One-click clips
12. After ending, a **Get Clips** panel appears:
    - Source choice (asked every time):
      - **From YouTube URL** — downloads each clip segment via yt-dlp (uses the show's URL; editable here).
      - **From local recording** — browse/drop the StreamYard file; extracts each segment via ffmpeg stream copy (seconds, not minutes).
    - Optional **offset** field (± seconds, default 0) applied to all clip times — compensates if Go Live was clicked before/after the actual stream start.
13. Click **Get Clips** → a clip session is created using the existing Clips pipeline (one clip per *Potential Clips* entry, centered on its timestamp, default window from Settings). The app switches to the Clips tab showing progress, exactly like today.
14. The show records the generated session id. Re-running Get Clips is allowed (creates a new session) with a "already generated once" note.

### 4.6 After
15. Past shows are listed on the Live tab (most recent first) with title, date, duration, entry counts, and a **Get Clips** shortcut for ended shows.
16. **Copy Timestamps** button on any show copies all timestamp-type entries as `h:mm:ss - note` lines (paste into YouTube description for chapters).

## 5. Functional Requirements

### FR-1 Show lifecycle
- FR-1.1 Create show with `title` (required, ≤200 chars) and `youtube_url` (optional).
- FR-1.2 States: `pre` (created, not started) → `live` (started_at set) → `ended` (ended_at set). State is derived from the two timestamps, not stored separately.
- FR-1.3 Only one show may be `live` at a time. Going live while another show is live is blocked with a clear message.
- FR-1.4 Timer = `now − started_at`, computed client-side every second; survives app restart.
- FR-1.5 `youtube_url` and `title` editable in any state.
- FR-1.6 Shows deletable in any state (confirmation; cascades entries).

### FR-2 Entry capture
- FR-2.1 Entry = `{type: timestamp|clip, note, elapsed_seconds}`. Note may be empty (a bare mark is valid).
- FR-2.2 Capture inputs enabled only while `live`.
- FR-2.3 Capture time = elapsed seconds at first keystroke of the current input value (pending-capture model, per §4.3).
- FR-2.4 Entries editable (note, time, type) and deletable in `live` and `ended` states.
- FR-2.5 Time edit accepts `mm:ss` and `h:mm:ss` (reuses the existing `parse_timestamp` rules).

### FR-3 Get Clips
- FR-3.1 Enabled only when the show is `ended` and has ≥1 clip-type entry.
- FR-3.2 Source = YouTube URL (requires non-empty URL) or local file (validated through `resolve_user_path`, same formats as SnipCut).
- FR-3.3 Creates a session + clips directly (no text round-trip): one clip per clip-entry, `center = elapsed + offset`, `window = default_clip_window` setting. Clamped ≥ 0.
- FR-3.4 Reuses the existing download (`download_clip`) / extraction (`extract_clip_local`) workers unchanged.
- FR-3.5 Stores `generated_session_id` on the show; UI links to it and warns on regeneration.

### FR-4 Persistence
- FR-4.1 New SQLite tables (see §6); created idempotently in `init_db`.
- FR-4.2 No changes to existing tables.

## 6. Data Model

```sql
CREATE TABLE IF NOT EXISTS shows (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    youtube_url TEXT DEFAULT '',
    started_at TEXT,              -- ISO8601 UTC; NULL until Go Live
    ended_at TEXT,                -- ISO8601 UTC; NULL until End Show
    generated_session_id TEXT DEFAULT '',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS show_entries (
    id TEXT PRIMARY KEY,
    show_id TEXT NOT NULL REFERENCES shows(id) ON DELETE CASCADE,
    type TEXT NOT NULL CHECK (type IN ('timestamp','clip')),
    note TEXT DEFAULT '',
    elapsed_seconds INTEGER NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_show_entries_show ON show_entries(show_id, elapsed_seconds);
```

## 7. API

| Method | Route | Body | Notes |
|---|---|---|---|
| POST | `/api/shows` | `{title, youtube_url?}` | create |
| GET | `/api/shows` | — | recent 30, with entry counts + state |
| GET | `/api/shows/<id>` | — | show + all entries |
| PUT | `/api/shows/<id>` | `{title?, youtube_url?}` | edit metadata |
| POST | `/api/shows/<id>/go-live` | — | 409 if another show is live |
| POST | `/api/shows/<id>/end` | — | idempotent |
| DELETE | `/api/shows/<id>` | — | cascades entries |
| POST | `/api/shows/<id>/entries` | `{type, note, elapsed_seconds}` | add entry |
| PUT | `/api/show-entries/<id>` | `{note?, elapsed_seconds?, type?}` | edit entry |
| DELETE | `/api/show-entries/<id>` | — | delete entry |
| POST | `/api/shows/<id>/get-clips` | `{source: 'url'\|'local', local_file?, youtube_url?, offset_seconds?}` | creates session; returns `{session_id, clip_count}` |

## 8. UI Spec

- **Tab bar:** `LIVE · NEW SESSION · CLIPS · SNIPCUT · HISTORY` — Live is first and is the default view on launch.
- **Live tab, no show active:** New Show form (title, URL) + past shows list.
- **Pre-live card:** title, URL field, large **● Go Live** primary button, Delete.
- **Live view:**
  - Header: pulsing LIVE badge + `h:mm:ss` timer (large, monospace) + red **■ End Show** button (confirmation).
  - Two side-by-side (stacked on narrow) capture sections: *Timestamps* and *Potential Clips*, each with input + pending-time chip + entry list (newest at bottom, auto-scroll).
  - Entry rows: mono timestamp, note, hover reveals edit/switch-type/delete.
- **Ended view:** summary line (duration, counts), entry lists (still editable), **Get Clips** panel (source toggle, file browse, offset input, primary button), **Copy Timestamps**, link to generated session if present.
- **Past shows list:** title, date, duration, `N ts · M clips`, state badge, Get Clips shortcut (ended, ≥1 clip), delete on hover.
- Keyboard: **Enter** submits capture input. `Cmd+1..5` switch tabs (existing pattern untouched).

## 9. Edge Cases

| Case | Behavior |
|---|---|
| App restarted mid-show | Timer resumes from `started_at`; state intact |
| Entry submitted with empty note | Allowed — bare mark, note editable later |
| Go Live clicked twice | Second click no-ops (already live) |
| Two shows live | Blocked with message naming the live show |
| Get Clips with 0 clip entries | Button disabled + hint ("mark Potential Clips during the show") |
| Get Clips, URL source, empty URL | Inline validation error |
| Get Clips twice | Allowed; warning shows previous session link |
| Offset makes a clip start < 0 | Clamped to 0 (existing pipeline behavior) |
| Note contains `-` or `\|` | Safe — clips are created directly, no text parsing |

## 10. Rollout

1. Ship behind nothing — it's additive (new tab, new tables). Existing tabs untouched.
2. Old StreamBuddy fetch/picker code stays for now (dead but harmless); removed in a later cleanup once the Live tab is proven over 2–3 real shows.
3. `Install_ClipCutter.command` already glob-copies `*.py`; no installer change needed.

## 11. Success Criteria

- A full show (2h, ~15 entries) captured without touching any app other than ClipCutter.
- End Show → clips on disk in under 60 seconds (local file source).
- Zero copy/paste of timestamps anywhere in the flow.

## 12. Future (explicitly out of scope now)

- Global hotkey capture while ClipCutter is in background (menu-bar mark button)
- Re-sync/collab layer
- Auto-pairing a dropped recording with the most recent ended show
- Chapters export formatted specifically for YouTube (`00:00` first-line requirement)
