"""Traffic-camera AI analytics: traffic level per camera and estimated crossing time to JB / SG.

How it works
- The camera still images (LTA Traffic Images) are sent to Claude (vision) in ONE batched request, which returns a
  traffic level per image: Smooth / Medium / Jam / Super Jam, and for the checkpoint cameras the level in each
  direction (towards Johor, towards Singapore). Only when ANTHROPIC_API_KEY is set.
- Without the key (or if the AI call fails) the level comes from LTA Traffic Speed Bands near the camera (app.py),
  never from TomTom (its daily cap is shared with the Forecast Centre traffic layer). Otherwise no level is shown (never guessed).
- Directions: the AI is told that all four checkpoint cameras stand on the Singapore side, that a camera's name says where it LOOKS
  (not where its traffic goes), and the confirmed view of camera 4703 (the lorry queue beside the sea is arriving INTO Singapore).
  It says which part of the photo it read for each direction (sg_where / jb_where). A controller can swap a camera's two
  directions for everyone (app.py stores it; CAMERA_AI_SWAP=2701,... sets defaults).
- Crossing time = typical time for that level (CROSSING_MIN). It is an estimate, labelled as such: the cameras show the
  road queue, not the immigration hall.
- Cost control: AI reads only the 4 checkpoint photos, shrunk (app.py), once per CAMERA_AI_EVERY_MIN (15) for everyone,
  05:00-24:00 SGT only, at most CAMERA_AI_DAILY_CAP (80) calls a day. Everything else uses free LTA speed bands.

Pure logic here; app.py passes in the network functions (fetch_image, ask_model, flow) so this is testable offline.
"""
import asyncio
import base64
import json
import os
import re
import time

LEVELS = ["Smooth", "Medium", "Jam", "Super Jam"]
MODEL = os.environ.get("CAMERA_AI_MODEL", "claude-haiku-5-5").strip()
CACHE_S = int(float(os.environ.get("CAMERA_AI_EVERY_MIN", "15")) * 60)   # one AI reading per checkpoint camera per 15 min, shared by every viewer
AI_HOURS = tuple(int(x) for x in os.environ.get("CAMERA_AI_HOURS", "5-24").split("-"))   # SGT hours when AI runs (outside: free LTA speed bands)
AI_DAILY_CAP = int(os.environ.get("CAMERA_AI_DAILY_CAP", "80"))                          # max AI calls a day (each call = the 4 checkpoint photos)
AI_ONLY_CHECKPOINTS = os.environ.get("CAMERA_AI_ALL_CAMERAS", "0") != "1"                # other cameras use LTA speed bands (free)
SGT_OFFSET = 8 * 3600
MAX_IMAGES = 10

# Woodlands Causeway and Tuas Second Link (LTA camera IDs). Both photos of a crossing are read for both directions.
CROSSINGS = [
    {"id": "woodlands", "name": "Woodlands", "cams": ["2701", "2702"]},
    {"id": "tuas", "name": "Tuas", "cams": ["4703", "4713"]},
]
CHECKPOINT_IDS = {c for x in CROSSINGS for c in x["cams"]}

# Which way the traffic in each checkpoint photo is going. All four cameras stand on the SINGAPORE side of the crossing. LTA's camera
# names ("Woodlands Causeway (Towards Johor)") say which way the camera LOOKS, not which way the traffic goes, and without this the AI
# guessed - camera 4703 had the lorry queue entering Singapore labelled "to Johor" (controller report, 30 Oct 2026).
VIEW_RULE = ("All four checkpoint cameras stand on the Singapore side of the crossing. A camera name such as \"(Towards Johor)\" says "
             "which way the camera LOOKS, not which way the traffic goes. to_sg = vehicles coming off the Causeway / Second Link "
             "towards the Singapore checkpoint (arriving in Singapore). to_jb = vehicles coming from Singapore's roads towards the "
             "checkpoint and the bridge (leaving for Johor). A queue belongs to the direction its vehicles travel in: judge it by "
             "which way the vehicles face. If you cannot tell which way a road's traffic is going, use null for that direction "
             "rather than guessing.")
VIEWS = {   # confirmed views of individual cameras (from controllers who know the site)
    "4703": ("Tuas Second Link, Singapore side. The long straight multi-lane road beside the sea, where lorries queue nose to "
             "tail towards the checkpoint, is traffic ARRIVING INTO SINGAPORE from the Second Link bridge: read it as to_sg. "
             "Give to_jb only if you can clearly see traffic leaving Singapore towards the bridge; otherwise to_jb is null."),
}
# Cameras whose two directions a controller has swapped (app.py loads these from the database; CAMERA_AI_SWAP gives defaults).
SWAPPED = {x.strip() for x in os.environ.get("CAMERA_AI_SWAP", "").split(",") if x.strip()}
# Typical door-to-door crossing time (approach road + bridge + checkpoint) for each level, minutes.
CROSSING_MIN = {"Smooth": 15, "Medium": 30, "Jam": 60, "Super Jam": 90}

_CACHE = {}          # (camera id, photo time or link) -> (epoch, result dict)
STATUS = {"ok_at": None, "error": None, "error_at": None, "model": MODEL, "images": 0}   # last AI call, for /api/camera-ai/test
_LOCK = None


def level_from_ratio(ratio):
    """TomTom current / free-flow speed -> level."""
    if ratio is None:
        return None
    if ratio >= .8:
        return "Smooth"
    if ratio >= .5:
        return "Medium"
    if ratio >= .25:
        return "Jam"
    return "Super Jam"


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def level_from_band(band):
    """LTA Traffic Speed Band (1 = 0-9 km/h ... 8 = 70+ km/h) -> level."""
    try:
        band = int(band)
    except (TypeError, ValueError):
        return None
    if band >= 5:                    # same split as the Route Traffic speed layer (smooth >= 5, moderate 4)
        return "Smooth"
    if band == 4:
        return "Medium"
    if band >= 2:
        return "Jam"
    return "Super Jam" if band >= 1 else None


def sniff_mime(data, header=None):
    """Claude accepts image/jpeg, png, gif, webp only. Camera hosts (S3) often send binary/octet-stream: read the bytes."""
    b = bytes(data[:12]) if data else b""
    if b[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if b[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "image/webp"
    h = (header or "").split(";")[0].strip().lower()
    return h if h in ("image/jpeg", "image/png", "image/gif", "image/webp") else None


def norm_level(value):
    if not value:
        return None
    v = str(value).strip().lower().replace("_", " ").replace("-", " ")
    for lv in LEVELS:
        if v == lv.lower():
            return lv
    if "super" in v or "standstill" in v or "severe" in v:
        return "Super Jam"
    if "jam" in v or "heavy" in v or "congest" in v:
        return "Jam"
    if "medium" in v or "moderate" in v or "slow" in v:
        return "Medium"
    if "smooth" in v or "light" in v or "free" in v or "clear" in v:
        return "Smooth"
    return None


def worst(levels):
    found = [lv for lv in levels if lv in LEVELS]
    return max(found, key=LEVELS.index) if found else None


def prompt(cams):
    lines = []
    for i, c in enumerate(cams):
        cid = str(c["id"])
        where = c.get("desc") or c.get("label") or ("Camera " + cid)
        lines.append(f"Image {i + 1}: camera {cid} - {where}" + (" (Singapore-Johor border crossing)" if cid in CHECKPOINT_IDS else "")
                     + (" - VIEW: " + VIEWS[cid] if cid in VIEWS else ""))
    has_cp = any(str(c["id"]) in CHECKPOINT_IDS for c in cams)
    return ("You are a traffic analyst for a Singapore bus operations centre. Below are " + str(len(cams)) +
            " still images from LTA expressway traffic cameras, in order.\n" + "\n".join(lines) +
            "\n\nFor each image judge how congested the road is from vehicle density, queue length and spacing. "
            "Use exactly one of: Smooth, Medium, Jam, Super Jam (Super Jam = stationary queue filling the carriageway). "
            "For border-crossing images also judge each direction separately where visible: to_jb (traffic heading to Johor) and "
            "to_sg (traffic heading into Singapore); use null if that direction is not visible. For each direction you give, say in "
            "a few words which part of the photo you read (sg_where, jb_where), e.g. \"lorry queue along the sea, top\". "
            + ("\nDirections at the checkpoints: " + VIEW_RULE + "\n" if has_cp else "") +
            "If an image is unreadable (dark, blurred, frozen) use null for level.\n"
            'Reply with JSON only: [{"image":1,"level":"Medium","to_jb":null,"to_sg":"Jam","sg_where":"lorry queue, top","jb_where":null,'
            '"confidence":0.7}, ...]')


def _where(v):
    v = " ".join(str(v).split())[:80] if v not in (None, "", "null") else ""
    return v or None


def swap_dirs(cid, res):
    """A camera whose directions a controller swapped: exchange to_jb / to_sg (and where each was read). Cached readings stay raw,
    so a swap (or undoing it) takes effect at once."""
    if str(cid) not in SWAPPED or not res or not (res.get("to_jb") or res.get("to_sg")):
        return res
    out = dict(res, to_jb=res.get("to_sg"), to_sg=res.get("to_jb"), swapped=True)
    for a, b in (("jb_where", "sg_where"), ("sg_where", "jb_where")):
        if b in res:
            out[a] = res[b]
        else:
            out.pop(a, None)
    return out


def parse_reply(text, n):
    """Model text -> {index: {level, to_jb, to_sg, confidence}} (index 0-based). Tolerates prose around the JSON."""
    if not text:
        return {}
    rows = None
    for cand in (text, *(x.group(0) for x in [re.search(r"\[.*\]", text, re.S), re.search(r"\{.*\}", text, re.S)] if x)):
        try:
            rows = json.loads(cand)
            break
        except (ValueError, TypeError):
            continue
    if isinstance(rows, dict):                       # {"images": [...]} or {"1": {...}, "2": {...}}
        lists = [v for v in rows.values() if isinstance(v, list)]
        rows = lists[0] if lists else [dict(v, image=v.get("image", k)) for k, v in rows.items() if isinstance(v, dict)]
    if not isinstance(rows, list):
        return {}
    out = {}
    for pos, r in enumerate(rows if isinstance(rows, list) else []):
        if not isinstance(r, dict):
            continue
        try:
            i = int(r.get("image", pos + 1)) - 1
        except (TypeError, ValueError):
            continue
        if not 0 <= i < n:
            continue
        try:
            conf = max(0.0, min(1.0, float(r.get("confidence"))))
        except (TypeError, ValueError):
            conf = None
        out[i] = {"level": norm_level(r.get("level")), "to_jb": norm_level(r.get("to_jb")), "to_sg": norm_level(r.get("to_sg")), "confidence": conf}
        for k, d in (("sg_where", "to_sg"), ("jb_where", "to_jb")):
            w = _where(r.get(k))
            if w and out[i][d]:
                out[i][k] = w
    return out


def _key(c):
    return str(c["id"])          # per camera, not per photo: LTA posts a new photo every minute or two


def ai_allowed(now):
    """(allowed, reason): inside the SGT time window and under today's call cap."""
    sgt = time.gmtime(now + SGT_OFFSET)
    day = time.strftime("%Y-%m-%d", sgt)
    if STATUS.get("day") != day:
        STATUS.update(day=day, calls_today=0)
    lo, hi = (AI_HOURS + (24,))[:2] if len(AI_HOURS) == 1 else AI_HOURS[:2]
    if not lo <= sgt.tm_hour < hi:
        return False, f"AI runs {lo:02d}:00-{hi:02d}:00 SGT only; LTA speed bands now"
    if STATUS["calls_today"] >= AI_DAILY_CAP:
        return False, f"today's AI limit ({AI_DAILY_CAP} readings) reached; LTA speed bands until midnight"
    return True, None


def crossings(results):
    """Per crossing and direction: worst level reported by its cameras -> estimated minutes."""
    out = []
    for x in CROSSINGS:
        rows = [results.get(cid) for cid in x["cams"] if results.get(cid)]
        if not rows:
            continue
        item = {"id": x["id"], "name": x["name"], "source": rows[0].get("source")}
        for d in ("to_jb", "to_sg"):
            lv = worst([r.get(d) for r in rows])
            item[d] = {"level": lv, "minutes": CROSSING_MIN.get(lv)} if lv else None
        if not (item["to_jb"] or item["to_sg"]):
            # Speed data (or an image without a readable direction) gives one level for the crossing, not per direction.
            lv = worst([r.get("level") for r in rows])
            item["any"] = {"level": lv, "minutes": CROSSING_MIN.get(lv)} if lv else None
        if item["to_jb"] or item["to_sg"] or item.get("any"):
            out.append(item)
    return out


async def analyse(cams, fetch_image=None, ask_model=None, flow=None, now=None, force=False):
    """cams: camera dicts (id, link, lat, lon, taken, desc). Returns {"cameras": {id: result}, "crossings": [...], "ai": bool, "note": str}."""
    global _LOCK
    now = now or time.time()
    _LOCK = _LOCK or asyncio.Lock()
    cams = [c for c in cams if c and c.get("id") and c.get("link")][:MAX_IMAGES]
    results, todo = {}, []
    for c in cams:
        hit = _CACHE.get(_key(c))
        if hit and now - hit[0] < CACHE_S and not force:
            results[str(c["id"])] = hit[1]
        else:
            todo.append(c)
    error = None
    todo_ai = [c for c in todo if str(c["id"]) in CHECKPOINT_IDS or not AI_ONLY_CHECKPOINTS]
    allowed, why = ai_allowed(now)
    if todo_ai and ask_model and fetch_image and not (allowed or force):
        STATUS["paused"] = why
    if todo_ai and ask_model and fetch_image and (allowed or force):
        async with _LOCK:
            # another viewer's request may have refreshed these while this one waited for the lock
            for c in list(todo_ai):
                hit = _CACHE.get(_key(c))
                if hit and now - hit[0] < CACHE_S and not force:
                    results[str(c["id"])] = hit[1]
                    todo_ai.remove(c)
            todo = todo_ai
            try:
                if not todo:
                    raise StopAsyncIteration
                STATUS["calls_today"] = STATUS.get("calls_today", 0) + 1
                STATUS["paused"] = None
                images = await asyncio.gather(*(fetch_image(c["link"]) for c in todo), return_exceptions=True)
                ok = [(c, img[0], sniff_mime(img[0], img[1])) for c, img in zip(todo, images) if not isinstance(img, BaseException) and img and img[0]]
                ok = [(c, (data, mime)) for c, data, mime in ok if mime]
                STATUS["images"] = len(ok)
                if not ok:
                    bad = [img for img in images if isinstance(img, BaseException)]
                    raise RuntimeError("no camera photo could be downloaded" + (f" ({type(bad[0]).__name__}: {str(bad[0])[:80]})" if bad else " (not an image)"))
                if ok:
                    content = []
                    for c, (data, mime) in ok:
                        content.append({"type": "image", "source": {"type": "base64", "media_type": mime,
                                                                    "data": base64.b64encode(data).decode()}})
                    content.append({"type": "text", "text": prompt([c for c, _ in ok])})
                    reply = await ask_model(content)
                    STATUS["reply"] = (reply or "")[:400]
                    parsed = parse_reply(reply, len(ok))
                    if not parsed:
                        raise RuntimeError("the AI reply had no readable levels: " + (reply or "")[:120])
                    STATUS.update(ok_at=now, error=None)
                    for i, (c, _) in enumerate(ok):
                        r = parsed.get(i)
                        if r and (r["level"] or r["to_jb"] or r["to_sg"]):
                            res = dict(r, source="AI camera analytics", model=MODEL, at=now)
                            _CACHE[_key(c)] = (now, res)
                            results[str(c["id"])] = res
            except StopAsyncIteration:
                pass
            except Exception as e:      # network / quota / parse problems fall back to speed data
                error = "AI camera reading failed: " + (str(e)[:220] or type(e).__name__)
                STATUS.update(error=error, error_at=now)
    if flow:
        for c in cams:
            if str(c["id"]) in results:
                continue
            try:
                f = await flow(c["lat"], c["lon"])
            except Exception:
                f = None
            f = f or {}
            lv = f.get("level") or level_from_ratio(f.get("ratio"))
            if lv:
                sp, fr = _num(f.get("speed", f.get("cur"))), _num(f.get("free_speed", f.get("free")))   # LTA sends speeds as text
                results[str(c["id"])] = {"level": lv, "to_jb": None, "to_sg": None, "confidence": f.get("conf"),
                                         "source": f.get("source", "TomTom speed"), "speed": round(sp) if sp is not None else None,
                                         "free_speed": round(fr) if fr is not None else None, "at": now}
    if len(_CACHE) > 500:
        for k in sorted(_CACHE, key=lambda k: _CACHE[k][0])[:200]:
            _CACHE.pop(k, None)
    results = {cid: swap_dirs(cid, r) for cid, r in results.items()}
    return {"cameras": results, "crossings": crossings(results), "ai": bool(ask_model), "ai_paused": STATUS.get("paused"),
            "swapped": sorted(SWAPPED & CHECKPOINT_IDS),
            "ai_usage": {"calls_today": STATUS.get("calls_today", 0), "daily_cap": AI_DAILY_CAP, "every_min": CACHE_S // 60, "hours": list(AI_HOURS)},
            "note": "Estimate from camera stills (road queue only, not the immigration hall). Level: Smooth / Medium / Jam / Super Jam.",
            "error": error, "updated": now}
