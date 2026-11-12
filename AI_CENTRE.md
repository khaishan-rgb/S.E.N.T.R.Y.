# AI Operations Centre (HORIZON AI) - 08 Nov 2026 (Officer portrait and AI hologram faces added 12 Nov)

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
- **Four faces**, picked in the panel:
  - **Officer** (default, 3D since 12 Nov): a 3D head-and-shoulders model of a young Chinese man in the SBS Transit
    uniform, wearing an operator headset. File `assistant-officer.glb` (5.4 MB, served as `/brand/assistant-officer.glb`
    and cached by the browser), made from the 80 MB model supplied on 12 Nov (216106.glb): scaled so his face matches the
    player's face points, the hidden inside of the shirt shell and parts never seen from the front removed, reduced to
    about 160,000 triangles (the eyes and mouth keep every point), with the full 4096 px texture. It is drawn in the
    browser (WebGL, no library, no outside service, no cost). The model has no skeleton or face rig, so the movement is
    added here: he breathes, sways, turns and nods his head, blinks, and when he talks his jaw drops and his lips part,
    widen or round with the letters. The headset microphone is marked in the file so it stays still when the jaw moves.
    His arms stay still.
    While the model loads, and on a computer that cannot draw it, `assistant-officer.webp` shows instead: a still picture of
    the same model from the same camera, which also blinks and talks. He uses a male voice where the browser has one: Edge
    on Windows has Wayne (English, Singapore), Yunxi (Mandarin) and Osman (Malay); Chrome has a male English voice only.
    To use a different 3D model, it must be one mesh with Y up and the face towards +Z; then update the eye, mouth,
    chin, brow and neck positions in `O3` at the top of the "Officer in 3D" code in `assistant.js`, and remake the still
    picture and its points (`OFF_PTS`). A rigged model (skeleton and face shapes) would need a different player.
    Browsers keep `/brand/` files for a day, so after replacing `assistant-officer.glb` or `.webp`, change `OFFICER_V`
    at the top of the Officer code in `assistant.js`; every browser then loads the new files straight away.
    Every browser switches to the Officer once; anyone can switch to another face.
  - **AI**: a holographic head made of slices of light, like the AI in films, drawn live in the browser (WebGL). It turns
    its head, blinks, and its jaw and lips follow the words. It glows with the voice, turns violet while it checks the data
    and green while it listens. A computer without WebGL shows the Orb.
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

## How HORIZON AI thinks (updated 08 Nov, 15:20)

- **Your rules:** `horizon_ai_prompt.md` holds the OCC's HORIZON AI rules (role, never treat a missing alert or a missing service
  as "normal" or "not running", the answer format, data confidence). Edit that file and upload it to change how it answers.
  The app adds a short data guide after it that says what each piece of data means and what is not connected (CFMS, duty records,
  OTA, official EWT, departure logs).
- **Services you name are checked, not guessed.** For "How was 147?", "147A bunching?", "147号怎么样", "bas 147 hari ini":
  - live: buses on the route per direction, estimated spacing between buses, scheduled headway and its source, bunching and gaps
    (measured on demand by the bunching collector from LTA arrival data, then kept on watch for 45 minutes);
  - history: bunching and long-headway events logged in the window, and how many minutes that direction was actually monitored,
    so "no events" is never mistaken for "normal";
  - running time: measured trips against the scheduled and usual running time;
  - every Forecast Centre alert, service row and recommendation for that service (not only the top ones).
- **Time window:** "now" = live plus the last hour; "today", "last 3 hours", "yesterday" (history only, no live check).
- **Thresholds** are the ones in Bunching settings (bunching below 3 min, long headway at scheduled + 10 min, 15 stops to confirm,
  unless you changed them). HORIZON AI does not invent others.
- **Answer format:** "Service 147 - status", Assessment, Key findings, Action (only when needed), Data confidence
  (Verified / Partial / Insufficient / Stale). Under 120 words.
- On a DEMO page it does not take live measurements (no LTA calls); the rules prompt is cached, so the long rules cost little after
  the first question.

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
| `assistant.js` | new, changed 12 Nov | The HORIZON AI panel: faces (Officer portrait, AI hologram, Orb, Photo), languages, voice, chat |
| `assistant-officer.glb` | new 12 Nov | The Officer 3D model (head and shoulders, with headset; reduced from the 80 MB original) |
| `assistant-officer.webp` | new 12 Nov | A still of the 3D Officer: shown while the model loads, and if it cannot be drawn |
| `assistant.css` | new, changed 12 Nov | Panel styles (HORIZON navy and cyan) |
| `app.py` | changed (12 Nov: `/brand/` also serves `.glb`) | `/ai-centre`, `/assistant.js`, `/assistant.css`, `GET /api/assistant/status`, `POST /api/assistant/ask`; `aicentre` added to the pages Settings can hide |
| `app_shell.js` | changed | Menu entry "AI Operations Centre" with a new spark icon |
| `forecast.html` | changed | Top link "AI OPERATIONS" |
| `render.yaml` | changed | Lists `ANTHROPIC_API_KEY`, `ASSISTANT_AI_MODEL`, `ASSISTANT_DAILY_CAP` (values stay in Render) |
| `test_ai_assistant.py` | new | Offline tests: no key, bad input, languages, data trimming, checkpoint readings, limits, AI errors, service numbers, time windows, live / history checks |
| `horizon_ai_prompt.md` | new | The OCC's HORIZON AI rules (edit to change how it answers) |

## Keep in step

`ai_centre.html` is a copy of `forecast.html`. When `forecast.html` changes, copy it again and redo the small edits
listed in the comment at the top of `ai_centre.html` (title, heading, top links, `data-module="aicentre"`, and the two
assistant lines).

## Later, if wanted

A photo-real talking head with lip sync (HeyGen or D-ID streaming avatars) can replace the face: it needs that
company's account and key, and costs per minute of video. The answers would still come from the same
`/api/assistant/ask`.
