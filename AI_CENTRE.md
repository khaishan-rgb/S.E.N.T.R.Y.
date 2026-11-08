# AI Operations Centre (HORIZON AI) - 08 Nov 2026

A new page, **/ai-centre** (menu: COMMAND > AI Operations Centre). It is a copy of the Forecast Centre with a
talking assistant, **HORIZON AI**, docked on the right. Controllers ask about any alert, service or incident and get
an answer in English, 中文 or Bahasa Melayu, on screen and read aloud.

The Forecast Centre page (/command, /forecast) is unchanged apart from one new top link, "AI OPERATIONS".

## What it does

- **Answers from the page.** Each question sends what the page already shows (top 30 alerts, 12 recommendations,
  40 services, KPIs, weather, filters) plus the checkpoint camera readings from the last 30 minutes. No routes or stops,
  and no new camera AI calls. Claude answers first, then gives "Why", "What to do" and "Expected recovery".
- **Advisory only.** It never changes anything. Anything that changes service is marked "Service Controller approval".
  In demo mode it says the data is simulated.
- **Three faces**, picked in the panel:
  - **Agent**: a drawn control-room agent with a headset. She blinks and her mouth moves with the voice.
  - **Orb**: an abstract AI core. The rings turn and the core pulses with the voice.
  - **Photo**: a picture chosen in the panel (Choose photo). Only use a picture you have permission to use.
    It is kept in that browser only. To give everyone the same photo, upload it as `brand/assistant-face.webp`
    and choose it once in each browser.
- **Three languages**: EN / 中文 / BM in the panel header. This changes the panel's words, the answer language, the voice
  and the microphone language.
- **Voice out** uses the browser's own voices (free). Chrome and Edge have English and Mandarin. Many PCs have no
  Malay voice: the panel then uses the Indonesian voice, which is close, and says so. Edge on Windows usually has a
  natural Malay voice.
- **Voice in** (microphone button) works in Chrome and Edge. Press it, speak, and the question is sent when you stop.
- Mute, replay, stop, bigger/smaller, minimise, and drag the panel by its header. The panel remembers these per browser.

## Setup

Nothing new if the camera AI already works: it uses the same `ANTHROPIC_API_KEY` in Render > Environment.
Never put the key in the code or on GitHub.

Optional settings (Render > Environment):

| Setting | Default | What it does |
|---|---|---|
| `ASSISTANT_AI_MODEL` | same model as the other AI features | Model for HORIZON AI answers |
| `ASSISTANT_DAILY_CAP` | 400 | Most questions a day for the whole site |

Each person can ask up to 8 questions a minute. Each question sends roughly 2,000 to 4,000 tokens and gets up to
700 back; the Anthropic Console's Usage page shows the real cost.

Settings > Pages can hide the AI Operations Centre like any other page (`aicentre`).

## Files

| File | New / changed | What |
|---|---|---|
| `ai_centre.html` | new | The page: a copy of `forecast.html` plus the assistant |
| `assistant.js` | new | The HORIZON AI panel: faces, languages, voice, chat |
| `assistant.css` | new | Panel styles (HORIZON navy and cyan) |
| `app.py` | changed | `/ai-centre`, `/assistant.js`, `/assistant.css`, `GET /api/assistant/status`, `POST /api/assistant/ask`; `aicentre` added to the pages Settings can hide |
| `app_shell.js` | changed | Menu entry "AI Operations Centre" with a new spark icon |
| `forecast.html` | changed | Top link "AI OPERATIONS" |
| `render.yaml` | changed | Lists `ANTHROPIC_API_KEY`, `ASSISTANT_AI_MODEL`, `ASSISTANT_DAILY_CAP` (values stay in Render) |
| `test_ai_assistant.py` | new | Offline tests: no key, bad input, languages, data trimming, checkpoint readings, limits, AI errors |

## Keep in step

`ai_centre.html` is a copy of `forecast.html`. When `forecast.html` changes, copy it again and redo the small edits
listed in the comment at the top of `ai_centre.html` (title, heading, top links, `data-module="aicentre"`, and the two
assistant lines).

## Later, if wanted

A photo-real talking head with lip sync (HeyGen or D-ID streaming avatars) can replace the face: it needs that
company's account and key, and costs per minute of video. The answers would still come from the same
`/api/assistant/ask`.
