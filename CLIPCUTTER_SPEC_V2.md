# ClipCutter — Product Spec v2

## Vision

ClipCutter turns livestream timestamps into ready-to-post YouTube Shorts. The entire post-production pipeline — from raw timestamp notes to titled, captioned vertical clips — lives in one desktop app. No DaVinci Resolve, no Opus Clip, no context-switching.

**User:** Finance content creator running 2–3 hour livestreams. Jots rough timestamps during streams for moments worth clipping into Shorts. Currently spends 30–60+ minutes per stream in post-production across three separate tools.

**Goal:** Reduce that to a 10-minute review session inside one app.

---

## Current Workflow (pain points)

1. **Livestream** → jots timestamps in Notes app (e.g. "1 hr 48 mins - sofi buybacks")
2. **Downloads entire 2.5hr stream** from YouTube (~5–10GB, 15+ min wait)
3. **Opens DaVinci Resolve** → scrubs to timestamp → trims 5 min segment → splices down to ~60 seconds for Shorts. This is the biggest time sink — pure editorial work, no graphics/b-roll, just finding the best 1 min.
4. **Exports from Resolve** → uploads to **Opus Clip** which ONLY adds captions and makes it vertical (9:16). Expensive for what it does.
5. **Posts** to YouTube Shorts and X.

## Target Workflow (with ClipCutter v2)

1. **Livestream** → no need to jot timestamps anymore
2. **Opens ClipCutter** → pastes YouTube URL → hits Start
3. **Step 1 Scan** → app pulls YouTube auto-captions, Claude identifies clippable moments → user reviews suggestions, adds any custom segments, copies timestamp list for video description
4. **Step 1 Collect** → downloads only selected segments, transcribes with Whisper, Claude finds ~60s sizzle reel in each
5. **Step 2 (Trim)** → user reviews AI suggestion per clip, adjusts start/end, saves → trimmed clip renders immediately
6. **Step 3 (Title & Description)** → Claude generates YT/X copy from trimmed transcript + channel profile → one-click copy
7. **Step 4 (Format, optional)** → vertical crop + captions burned in via ffmpeg → skip if using Opus Clip
8. **Save to History** → session with all clips archived for future reference

---

## App Structure

### Top-Level Navigation

Three tabs across the top of the app:

| Tab | Purpose |
|-----|---------|
| **New Session** | Paste a YouTube URL — that's it. ClipCutter handles the rest. |
| **Current Session** | The main workspace — shows the 4-step pipeline for the active session |
| **History** | List of past sessions — click to reopen into Current Session |

New Session is a single input: the YouTube URL. No timestamps needed — Claude finds them. Once the user hits Start, the app pulls the stream's auto-captions and moves to Current Session where Step 1 begins. History is a simple list view with session date, video title, and clip count. Clicking a history item loads it back into Current Session.

### Step Pipeline (within Current Session)

The session view shows four steps as a linear pipeline. Steps unlock sequentially but the user can navigate back to previous steps to make adjustments.

Each step has three states:
- **Locked** — previous step not complete (dimmed, not clickable)
- **Active** — currently working on this step
- **Done** — completed, shows summary, still clickable to go back

A step bar at the top of Current Session shows progress: `① Gather → ② Trim → ③ Title → ④ Format`

---

## Step 1: Gather

Step 1 has two phases: **Scan** (fast, ~30 seconds) and **Collect** (slower, depends on how many clips).

### Phase A: Scan

**Input:** YouTube URL only
**Processing:** Pull YouTube auto-captions → Claude analyzes full transcript → suggests clippable segments
**Output:** List of AI-suggested segments + ability to add custom segments

#### How it works

1. yt-dlp downloads the auto-generated captions file (seconds, no video download):
   `yt-dlp --write-auto-subs --sub-lang en --skip-download --sub-format json3 <url>`
2. The caption text is sent to Claude with a prompt asking it to identify the strongest clippable moments
3. Claude returns a list of suggested segments, each with: timestamp, suggested title, reasoning, and estimated clip quality

#### What the user sees

A "Scanning livestream..." progress state (very brief), then:

**AI Suggestions panel** — a list of suggested segments, each showing:
- Timestamp + suggested title (e.g. "1:48:00 — SoFi buyback thesis")
- Brief reasoning from Claude (e.g. "Clear thesis with specific $1.2B figure, works standalone")
- Checkbox to select/deselect (all selected by default)
- Confidence indicator (high/medium — Claude self-rates)

**Manual Add section** — below the suggestions:
- "Add custom segment" button
- Opens a simple row: timestamp input + note input + clip window override
- User can add as many custom segments as they want
- Supports the same flexible timestamp formats as v1 ("1 hr 48 mins", "2:15:00", etc.)

**YouTube Timestamps panel** — a formatted timestamp list for the video description:
- Auto-generated from all selected segments (AI + manual)
- One-click copy button
- Format: `1:48:00 - SoFi buyback thesis\n2:15:00 - Rate cut analysis\n...`
- User can paste this directly into their YouTube video description

**Default clip window** — configurable (default 5 mins), applies to all segments

#### Action

"Gather Selected Clips" button at the bottom → starts Phase B.

### Phase B: Collect

**Input:** Selected segments (AI-suggested + manual)
**Processing:** Download each segment → Transcribe with Whisper → AI finds sizzle reel within each clip
**Output:** 5-min clips with word-level transcripts and AI-suggested ~60s sizzle reels

This is the original Gather flow — downloads only the selected segments via yt-dlp `--download-sections`, runs Whisper for accurate word-level timestamps, then Claude analyzes each individual clip transcript to find the best ~60 second sizzle reel.

#### What the user sees

A list of clip cards, each showing:
- Clip name (from AI title or user note)
- Status: Queued → Downloading → Transcribing → Analyzing → Ready
- Per-clip progress bar
- When ready: green checkmark + "Ready for trim"

Processing happens in parallel where possible (download clip 2 while transcribing clip 1).

### When Step 1 is "done"

All selected clips have status "Ready." A prominent "Continue to Trim →" button appears.

The user can also move forward before all clips finish — clips still processing show as locked in Step 2.

---

## Step 2: Trim

**Input:** Raw 5-min clips with transcripts + AI suggestions
**Output:** Trimmed ~60s clips (rendered immediately on save)

### What the user sees

A list of clip cards (collapsed by default). Each card shows:
- Clip name
- AI-suggested duration (e.g. "66s suggested")
- Status: Needs Review / Trimmed

### Expanded clip view (click a card to open)

Two main panels:

**Left/Top: Transcript Panel**
- Full transcript with timestamps
- AI-suggested segment highlighted (e.g. yellow/amber background on those lines)
- User can click to select a different start/end point in the transcript
- Above the transcript: AI reasoning card explaining why this segment was chosen

**Right/Bottom: Preview Panel**
- Video player showing the raw 5-min clip
- Trim bar underneath: draggable start/end handles
- Time readout: start time, end time, duration
- The trim bar and transcript selection stay in sync — adjusting one updates the other

### Controls
- **Preview** button — plays just the selected segment
- **Save Trim** button — triggers ffmpeg to cut the clip (~10 seconds), saves the rendered file, marks clip as "Trimmed"
- **Reset** — returns to AI suggestion

### When Step 2 is "done"

All clips show "Trimmed" status. "Continue to Title & Description →" button appears.

---

## Step 3: Title & Description

**Input:** Trimmed clips with transcripts + channel profile (from Settings)
**Output:** YouTube/X-ready title and description per clip

### What the user sees

A list of clip cards, each showing:
- Clip name
- Generated title (editable inline)
- Generated description (editable inline)
- One-click copy buttons for each field (copies to clipboard)
- "Regenerate" button to get a new suggestion from Claude

### How generation works

For each trimmed clip, Claude receives:
1. The trimmed transcript (not the full 5-min version)
2. The channel profile template (from Settings — see below)
3. A system prompt guiding title/description style

Claude generates:
- **Title:** Punchy, specific, optimized for Shorts (under 100 chars)
- **Description:** 2-3 sentences with context, relevant tickers, and a CTA

The user can edit inline before copying. Edits are saved to the session.

### Channel Profile (in Settings)

A text area where the user describes their channel once. This is sent as context to Claude for all title/description generation. Example:

```
Finance YouTube channel (32K subscribers) covering $SOFI, $BMNR, and $NBIS. 
Titles should be punchy, use specific numbers/data points when possible.
Tone is conversational and analytical — like explaining to a smart friend.
Usually posts Shorts as highlights from livestream analysis sessions.
Include relevant ticker symbols in titles when applicable.
```

This is editable anytime in Settings. Changes apply to future generations (not retroactively).

### When Step 3 is "done"

All clips have titles and descriptions (generated or manually entered). "Continue to Format →" button appears, with a secondary "Skip to Save" option (since Step 4 is optional).

---

## Step 4: Format (Optional)

**Input:** Trimmed clips
**Output:** Vertical (9:16) clips with burned-in captions

### What the user sees

A list of clip cards with toggles:
- **Vertical crop** toggle (on/off) — converts to 9:16
- **Captions** toggle (on/off) — burns subtitles from Whisper transcript
- **Caption style** selector: Bold white w/ black outline (default), Yellow highlight, Karaoke word-by-word
- **Export** button per clip — renders the final version
- **Export All** button — batch renders everything

### How it works

Uses ffmpeg to:
1. Crop to 9:16 (center crop by default)
2. Generate ASS subtitle file from Whisper word-level timestamps
3. Burn subtitles onto the video

### Skip behavior

If the user is sending clips to Opus Clip for formatting, they skip this step entirely. The "Skip to Save" option from Step 3 jumps straight to the Save action.

### When Step 4 is "done"

All clips with formatting enabled show "Exported" status. "Save Session" button appears.

---

## Save to History

A button at the bottom of the step pipeline (visible from Step 3 onward). Saves the entire session:
- All clips (raw + trimmed + formatted versions)
- All transcripts
- All titles and descriptions
- AI suggestions and reasoning
- Session metadata (date, URL, video title)

The session appears in the History tab. Reopening a session loads it back into Current Session at whatever step was last active.

---

## Technical Architecture

### Stack

- **Python + Flask** — backend server
- **pywebview** — native desktop window
- **yt-dlp** — segment download (--download-sections for partial downloads)
- **ffmpeg** — cutting, vertical crop, caption burn-in
- **Whisper** (local, `openai-whisper` package) — transcription with word-level timestamps
- **Claude API** (Haiku) — sizzle reel identification + title/description generation
- **SQLite** — session/clip persistence

### Directory Structure

```
~/.clipcutter/
├── venv/                    # Python virtual environment
├── clipcutter.py            # Main application
├── clipcutter.db            # SQLite database
├── whisper_models/          # Cached Whisper model files
└── sessions/
    └── <session_id>/
        ├── raw/             # Downloaded 5-min segments
        ├── trimmed/         # Rendered trimmed clips
        ├── formatted/       # Final vertical+captioned clips
        └── transcripts/     # Whisper JSON output
```

### Data Model (SQLite)

```sql
CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
-- Keys: "claude_api_key", "channel_profile", "default_clip_window", 
--        "target_short_duration", "whisper_model", "output_directory"

CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    youtube_url TEXT NOT NULL,
    video_title TEXT,
    video_duration INTEGER,          -- total stream duration in seconds
    stream_captions TEXT,            -- full YouTube auto-captions (JSON)
    youtube_timestamps TEXT,         -- formatted timestamp list for video description
    current_step INTEGER DEFAULT 1,  -- 1=gather, 2=trim, 3=title, 4=format
    gather_phase TEXT DEFAULT 'scanning', -- scanning/selecting/collecting/done
    status TEXT DEFAULT 'active',    -- active, saved
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- AI-suggested segments from the full-stream scan (Phase A)
CREATE TABLE suggestions (
    id TEXT PRIMARY KEY,
    session_id TEXT REFERENCES sessions(id),
    sort_order INTEGER,
    timestamp_seconds INTEGER,       -- center point in the stream
    suggested_title TEXT,            -- Claude's suggested clip title
    reasoning TEXT,                  -- why this moment is clippable
    confidence TEXT DEFAULT 'high',  -- high/medium
    selected INTEGER DEFAULT 1,      -- user selected this for clipping (boolean)
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE clips (
    id TEXT PRIMARY KEY,
    session_id TEXT REFERENCES sessions(id),
    suggestion_id TEXT REFERENCES suggestions(id),  -- NULL if manually added
    sort_order INTEGER,
    source TEXT DEFAULT 'ai',        -- 'ai' (from suggestion) or 'manual' (user-added)
    note TEXT,                       -- clip label ("sofi buybacks")
    center_seconds INTEGER,          -- timestamp in seconds
    window_seconds INTEGER,          -- clip window (default 300)
    
    -- Step 1 Phase B: Collect
    gather_status TEXT DEFAULT 'queued',  -- queued/downloading/transcribing/analyzing/ready/error
    raw_file TEXT,                        -- path to downloaded 5-min segment
    transcript_json TEXT,                 -- Whisper output (JSON with word timestamps)
    ai_suggestion_start REAL,            -- Claude's suggested sizzle reel start (seconds within clip)
    ai_suggestion_end REAL,
    ai_reasoning TEXT,
    
    -- Step 2: Trim
    trim_status TEXT DEFAULT 'pending',   -- pending/trimmed
    trim_start REAL,                      -- user-confirmed start (defaults to AI suggestion)
    trim_end REAL,
    trimmed_file TEXT,                    -- path to rendered trimmed clip
    trimmed_transcript TEXT,             -- subset of transcript for the trimmed range
    
    -- Step 3: Title & Description
    generated_title TEXT,
    generated_description TEXT,
    final_title TEXT,                     -- user-edited version (or same as generated)
    final_description TEXT,
    
    -- Step 4: Format
    format_vertical INTEGER DEFAULT 0,    -- boolean
    format_captions INTEGER DEFAULT 0,    -- boolean
    caption_style TEXT DEFAULT 'bold_white',
    formatted_file TEXT,                  -- path to final exported clip
    
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### API Endpoints

```
-- Session management
POST   /api/sessions              -- create session (URL only), triggers caption scan
GET    /api/sessions              -- list all sessions (for History tab)
GET    /api/sessions/<id>         -- get session with all clips + suggestions
PUT    /api/sessions/<id>         -- update session (current_step, status)
DELETE /api/sessions/<id>         -- delete session and files

-- Step 1 Phase A: Scan
GET    /api/sessions/<id>/scan-status    -- poll scan progress (downloading captions → analyzing)
GET    /api/sessions/<id>/suggestions    -- get AI-suggested segments
PUT    /api/suggestions/<id>             -- toggle selected/deselected on a suggestion
POST   /api/sessions/<id>/add-segment    -- add a manual segment (timestamp + note)
DELETE /api/sessions/<id>/segments/<id>  -- remove a manual segment
GET    /api/sessions/<id>/yt-timestamps  -- get formatted YouTube description timestamps

-- Step 1 Phase B: Collect
POST   /api/sessions/<id>/gather         -- start collecting selected segments (download + transcribe + analyze)
GET    /api/sessions/<id>/gather-status  -- poll gather progress for all clips

-- Step 2: Trim
GET    /api/clips/<id>                   -- get clip detail (transcript, AI suggestion, video path)
PUT    /api/clips/<id>/trim              -- set trim start/end, trigger render
GET    /api/clips/<id>/preview           -- get preview of trimmed segment (lower quality, fast)

-- Step 3: Title & Description
POST   /api/clips/<id>/generate-copy     -- trigger Claude to generate title + description
PUT    /api/clips/<id>/copy              -- save edited title + description

-- Step 4: Format
PUT    /api/clips/<id>/format-settings   -- set vertical/captions/style toggles
POST   /api/clips/<id>/export            -- render final formatted clip
POST   /api/sessions/<id>/export-all     -- batch export all clips with format settings

-- Utility
POST   /api/open-folder           -- open Finder/Explorer to a path
GET    /api/settings               -- get all settings
PUT    /api/settings               -- update settings
```

### Whisper Integration

```python
import whisper

# Load model once on startup (medium = best accuracy/speed for English)
# ~1.5GB download on first run, cached in ~/.clipcutter/whisper_models/
model = whisper.load_model("medium", download_root="~/.clipcutter/whisper_models")

# Transcribe with word-level timestamps
result = model.transcribe(
    audio_path,
    word_timestamps=True,
    language="en",
)
# result["segments"] → list of segments with word-level timing
# Each segment has: start, end, text, words[]
# Each word has: word, start, end, probability
```

### YouTube Auto-Captions (Step 1 Phase A)

```bash
# Pull auto-generated captions without downloading video (~5 seconds)
yt-dlp --write-auto-subs --sub-lang en --skip-download \
       --sub-format json3 \
       -o "%(id)s" <url>

# Output: <video_id>.en.json3 — contains timed caption segments
# Parse this JSON for the full transcript with timestamps
```

### Claude API — Stream Scanner (Step 1 Phase A)

```python
import anthropic

client = anthropic.Anthropic(api_key=settings["claude_api_key"])

prompt = f"""You are a YouTube Shorts editor for a finance content creator's livestream.

Analyze this full livestream transcript and identify the 5-10 strongest moments 
that would work as standalone YouTube Shorts (~45-75 seconds each).

Look for moments that have:
- A clear, complete thesis or insight (not mid-thought)
- Specific data points, numbers, or analysis
- Natural energy or conviction in the delivery
- A hook in the first few seconds that grabs attention
- Standalone clarity — a viewer with no context should still follow

FULL LIVESTREAM TRANSCRIPT (with timestamps):
{full_caption_text}

For each suggested segment, respond in JSON only, no other text:
{{
  "suggestions": [
    {{
      "timestamp_seconds": <int — center point in the stream>,
      "title": "<short punchy title for this clip>",
      "reasoning": "<1-2 sentences on why this moment is clippable>",
      "confidence": "high" | "medium",
      "hook": "<the opening line that grabs attention>"
    }}
  ]
}}

Order by quality (strongest first)."""

response = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=2000,
    messages=[{"role": "user", "content": prompt}],
)
```

### Claude API — Sizzle Reel Finder (Step 1 Phase B)

```python
import anthropic

client = anthropic.Anthropic(api_key=settings["claude_api_key"])

prompt = f"""You are a YouTube Shorts editor for a finance content creator.

Given this transcript from a livestream segment, identify the single best
continuous segment (~45-75 seconds) that would work as a standalone YouTube Short.

The ideal segment:
- Has a clear, complete thought or thesis
- Includes specific data points, numbers, or analysis
- Starts and ends at natural sentence boundaries
- Would make a viewer want to watch more
- Works without needing additional context

TRANSCRIPT (with timestamps in seconds):
{formatted_transcript}

Respond in JSON only, no other text:
{{
  "start_seconds": <float>,
  "end_seconds": <float>,
  "duration_seconds": <float>,
  "reasoning": "<2-3 sentences explaining why this is the strongest segment>",
  "hook": "<the opening line that grabs attention>"
}}"""

response = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=300,
    messages=[{"role": "user", "content": prompt}],
)
```

### Claude API — Title & Description (Step 3)

```python
channel_profile = settings["channel_profile"]

prompt = f"""You are a YouTube Shorts copywriter for this channel:

{channel_profile}

Given this transcript from a trimmed clip, generate a title and description 
optimized for YouTube Shorts and X (Twitter).

TRANSCRIPT:
{trimmed_transcript}

Requirements:
- Title: Under 100 characters, punchy, includes specific numbers/data if present
- Description: 2-3 sentences, provides context, includes relevant $TICKER symbols
- Both should match the conversational, analytical tone of the channel

Respond in JSON only, no other text:
{{
  "title": "<title>",
  "description": "<description>"
}}"""

response = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=300,
    messages=[{"role": "user", "content": prompt}],
)
```

### Export Pipeline (ffmpeg)

```bash
# Step 2 — Trim (immediate render on save):
ffmpeg -i raw_clip.mp4 -ss {start} -to {end} -c copy trimmed.mp4

# Step 4 — Format (vertical + captions):

# Generate ASS subtitle file from Whisper word timestamps (Python writes this)
# Then:
ffmpeg -i trimmed.mp4 \
  -vf "crop=ih*9/16:ih,scale=1080:1920,ass=captions.ass" \
  -c:v libx264 -preset fast -crf 20 \
  -c:a aac -b:a 192k \
  final_short.mp4

# Without captions, just vertical:
ffmpeg -i trimmed.mp4 \
  -vf "crop=ih*9/16:ih,scale=1080:1920" \
  -c:v libx264 -preset fast -crf 20 \
  -c:a aac -b:a 192k \
  final_short.mp4
```

---

## Design Direction

### Visual Style
- **Dark theme** — dark surfaces, content takes center stage
- **Monospace + sans-serif pairing** — JetBrains Mono for data/timestamps/labels, Outfit for UI text
- **Accent: rose/red (#f43f5e)** — primary actions and active states
- **Step colors** — each step has a subtle color accent for its header/progress:
  - Gather: teal
  - Trim: purple  
  - Title: coral
  - Format: amber
- **Surfaces** — use elevation (slightly lighter backgrounds) not borders to create hierarchy
- **Film grain / noise overlay** — subtle texture on the background

### Typography Scale
- **App title:** 20px, mono, bold
- **Step headers:** 14px, mono, bold, with step color
- **Section labels:** 10px, mono, uppercase, letterspaced, dimmed
- **Body/transcript:** 13-14px, sans, regular
- **Data/timestamps:** 11-12px, mono, dimmed
- **Buttons:** 13px, sans, semibold

### Component Patterns

**Step bar:** Horizontal progress indicator at the top of Current Session. Steps shown as labeled dots connected by lines. Active step is filled with its color, completed steps have checkmarks, locked steps are dimmed.

**Clip cards (collapsed):** Compact row showing: status dot (color-coded) + clip name + key metadata + status badge (Ready/Trimmed/etc). Click to expand.

**Clip cards (expanded):** Full-width panel with transcript on left, video preview on right (or stacked on narrow screens). Trim controls below the video. Action buttons at bottom.

**Transcript viewer:** Scrollable, line-by-line with timestamps in the left gutter. AI-suggested region has a highlighted background. Clicking a line sets the trim start/end point.

**Copy buttons:** Small clipboard icon next to title/description fields. Shows brief "Copied!" toast on click.

**Toast notifications:** Bottom-right, auto-dismiss after 3 seconds. For background completions ("Clip trimmed", "Title generated").

---

## Settings

Accessible via gear icon in the top-right corner. Fields:

| Setting | Default | Notes |
|---------|---------|-------|
| Claude API Key | (empty) | Required for AI features. Stored locally. App works without it but AI steps are disabled. |
| Channel Profile | (empty) | Free-text describing channel voice/style. Used as context for title generation. |
| Default Clip Window | 5 mins | How much video to grab around each timestamp |
| Target Short Duration | 60 seconds | Guide for AI when suggesting sizzle reel length |
| Whisper Model | medium | Options: small (faster), medium (default), large (most accurate) |
| Caption Style | Bold white | Default for Step 4. Options: Bold white w/ outline, Yellow highlight, Karaoke |
| Output Directory | ~/ClipCutter_Clips/ | Where final exports are saved |

---

## Key UX Details

### First Run
1. App opens to New Session tab with clean empty state
2. If no API key → banner: "Add your Claude API key in Settings to enable AI features"
3. App works WITHOUT API key — downloads segments (Step 1 partial), user trims manually (Step 2), no AI titles (Step 3 becomes manual-only), format still works (Step 4)

### Error Handling
- **yt-dlp fails on a clip** → that clip shows error state with retry button, other clips continue
- **Whisper fails** → clip skips AI analysis, user can still manually trim
- **Claude API fails** → show transcript without AI suggestion (Step 1) or empty title fields (Step 3), user fills in manually
- **ffmpeg fails** → show error with stderr output, allow retry

### Keyboard Shortcuts
- `Cmd+N` — new session
- `Space` — play/pause video preview
- `←/→` — nudge trim by 1 second
- `Shift+←/→` — nudge trim by 5 seconds
- `I` — set trim in-point at current playback position
- `O` — set trim out-point at current playback position
- `Cmd+S` — save current trim
- `Cmd+C` — copy title (when title field is focused)
- `Cmd+E` — export current clip
- `Cmd+Shift+E` — export all clips

### Step Navigation
- Steps unlock linearly: must complete (or have completable clips in) Step N before Step N+1 activates
- User can always click back to a previous step to make changes
- Going back and re-trimming a clip resets its title/description (since the content changed)
- Step 4 is skippable — user can go from Step 3 directly to Save

---

## Existing Codebase

The v1 of ClipCutter already exists and handles:
- Flask backend + pywebview desktop window
- yt-dlp segment download with --download-sections
- Timestamp parsing (natural language: "1 hr 48 mins", "2:15:00", "45m", etc.)
- Basic progress tracking and job management
- Dark themed UI with JetBrains Mono + Outfit fonts

The v2 build should evolve the existing codebase, not start from scratch. The core download infrastructure and timestamp parser are solid. The main additions are:
1. SQLite persistence layer (sessions + clips + settings + suggestions)
2. YouTube auto-caption download and full-stream AI scan
3. Whisper integration for word-level transcription
4. Claude API integration (stream scan + sizzle reel + titles)
5. Restructured UI with the 4-step pipeline
6. In-app video preview and trim controls
7. ffmpeg-based format/caption export

### GitHub + Auto-Update Architecture

The codebase lives in a private GitHub repo. The .app launcher auto-pulls on every launch:

```
~/.clipcutter/               ← this IS the git repo
├── .git/
├── clipcutter.py            # main application
├── requirements.txt         # pip dependencies (launcher watches for changes)
├── clipcutter.db            # SQLite (gitignored)
├── whisper_models/          # cached models (gitignored)
├── venv/                    # virtual environment (gitignored)
└── sessions/                # clip data (gitignored)
```

**.gitignore should include:** `venv/`, `sessions/`, `whisper_models/`, `clipcutter.db`, `__pycache__/`, `.req_hash`

**Developer workflow (Claude Code):**
1. Claude Code works directly in `~/.clipcutter/`
2. Makes changes to `clipcutter.py` (and any new files)
3. If new pip dependencies are needed, adds them to `requirements.txt`
4. Commits and pushes to `origin main`
5. Next time user clicks the app → launcher does `git pull` → new code runs

**Launcher behavior on every click:**
1. `cd ~/.clipcutter`
2. `git pull --ff-only origin main` (timeout 5s — works offline too)
3. Check if `requirements.txt` hash changed → `pip install -r requirements.txt` if so
4. `venv/bin/python clipcutter.py`

Total overhead: 1-2 seconds when nothing changed, 3-5 seconds when there's an update.

### Installation

The app uses a virtual environment at `~/.clipcutter/venv/` to avoid system Python conflicts. An installer script (`install.sh`) handles all dependencies (Homebrew, Python, ffmpeg, git, pip packages) and creates a macOS .app bundle on the Desktop.

The installer only needs to run once. After that, all updates flow through GitHub automatically.

### Environment Note

The user's Mac has Python 3.12 via Homebrew, managed by `uv`. System pip is locked down — all packages must go into the venv at `~/.clipcutter/venv/`. The installer handles this correctly.

---

## Channel Profile (Default)

This text is stored in Settings and sent as context to Claude for all title/description generation in Step 3. The user can edit it anytime.

```
Finance YouTube channel (32K+ subscribers) hosted by Tevis. Covers three core 
tickers: $SOFI, $BMNR, and $NBIS, with occasional coverage of broader market themes.

Content format: Primarily livestream analysis sessions (2-3 hours) clipped into 
YouTube Shorts. Shorts are highlight moments — a single clear insight, thesis, 
or data breakdown pulled from the full stream.

Tone: Educational and analytical. Breaks down complex financial concepts 
(securitization, buybacks, SBC dilution, options flow) in a way that feels like 
explaining to a smart friend — not dumbing it down, but making it accessible. 
Data-driven — always anchored in specific numbers, filings, or earnings call 
details rather than vague sentiment.

Title style: Varies by clip. Use whichever fits best:
- Punchy statement: "SoFi's $1.2B Buyback Changes Everything"
- Question hook: "Is $BMNR's H2 Recovery Actually Happening?"  
- Thesis-driven: "Why $NBIS at $40 Is Still Undervalued"
Always include the relevant ticker symbol ($SOFI, $BMNR, $NBIS). 
No clickbait — the title should accurately reflect the clip's content.

Description style: 2-3 sentences. First sentence hooks the topic. Include relevant 
ticker symbols. End with context about the source (e.g. "From our weekly $SOFI 
deep dive livestream"). Keep it natural, not keyword-stuffed.

Avoid: Generic market commentary, hype language ("to the moon", "massive gains"), 
misleading framing. The audience trusts this channel for honest, research-backed analysis.
```

