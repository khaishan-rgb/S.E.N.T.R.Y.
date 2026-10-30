# Checkpoint camera directions: fix (30 October 2026)

**Problem:** at Tuas (camera 4703), the AI read the lorry queue as traffic going **to Johor**. The queue was actually lorries **arriving into Singapore**, so the panel showed "→ Johor ≈ 60 min, jam" and "→ Singapore ≈ 15 min, smooth".

**Why:** the AI was asked for "to Johor" and "to Singapore" levels but was never told which way the traffic in each photo goes, so it guessed. LTA's camera names add to the confusion: "Woodlands Causeway (Towards Johor)" describes where the camera looks, not where its traffic goes.

**Fix** (`checkpoint_ai.py`):
* **Direction rule:** every AI reading now carries this rule. All four checkpoint cameras stand on the Singapore side. Traffic coming off the Causeway or the Second Link towards the checkpoint is arriving into Singapore. Traffic from Singapore's roads heading towards the checkpoint and the bridge is leaving for Johor. A queue belongs to the direction its vehicles face. If the AI cannot tell, it leaves that direction empty instead of guessing.
* **Camera 4703:** its confirmed view is written in. The long straight road beside the sea, where lorries queue, is traffic into Singapore.
* **Where the AI looked:** for each direction, the AI now says which part of the photo it read, e.g. "lorry queue along the sea". Controllers can check this at a glance.

**Camera dialog** (`forecast2026.js`, `forecast2026.css`):
* **This photo:** shows this camera's own reading for → Singapore and → Johor, with where it was read. "Not seen in this photo" appears when a direction isn't visible.
* **Crossing estimate:** shown separately and labelled "Tuas crossing · both Tuas cameras". Before, the crossing figures sat under the photo as if they came from that one photo.
* **Swap button:** "Directions wrong way round? Swap them" flips one checkpoint camera's two directions for every controller, straight away. The page records who swapped it and when, and the swap can be undone. Swaps are kept in the database. `CAMERA_AI_SWAP=2701,4713` (Render → Environment) can set swaps that survive a redeploy on the free plan.

**Server** (`app.py`):
* New `POST /api/forecast/centre/camera-ai/swap` with body `{"id":"4703","swapped":true}`. It accepts only the checkpoint cameras 2701, 2702, 4703 and 4713.
* `/api/forecast/centre/camera-ai` now also returns `swapped` and `swap_info`.
* `/api/camera-ai/test` also shows where each direction was read.

**Tests:** `test_checkpoint_ai.py` grows from 9 to 14 tests. The new ones cover the direction rule in the prompt, the 4703 view, where-read text, the exact 4703 case (the lorry queue counts as into Singapore), swap and undo taking effect immediately on a cached reading, and that a swap never touches speed-band readings. The swap endpoint was also checked against the real `app.py`: save, reload after restart, undo, a non-checkpoint camera refused, and a bad request refused. All Node UI suites pass. The new camera dialog was rendered at desktop and phone width.

**Upload / replace:** `checkpoint_ai.py`, `app.py`, `forecast2026.js`, `forecast2026.css`, `forecast.html` (asset version `20261030-cam1`, so browsers load the new script), `test_checkpoint_ai.py`, and this note.
