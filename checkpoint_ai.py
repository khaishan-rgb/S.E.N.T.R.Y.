"""Traffic-camera AI analytics: traffic level per camera and estimated crossing time to JB / SG.

How it works
- The camera still images (LTA Traffic Images) are sent to Claude (vision) in ONE batched request, which returns a
  traffic level per image: Smooth / Medium / Jam / Super Jam, and for the checkpoint cameras the level in each
  direction (towards Johor, towards Singapore). Only when ANTHROPIC_API_KEY is set.
- Without the key (or if the AI call fails) the level comes from TomTom speed now vs free-flow speed at the camera
  point, when a TomTom key is set. Otherwise no level is shown (never guessed).
- Crossing time = typical time for that level (CROSSING_MIN). It is an estimate, labelled as such: the cameras show the
  road queue, not the immigration hall.
- Results are cached per camera photo for CACHE_S seconds, so a page refresh does not call the AI again.

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
CACHE_S = int(os.environ.get("CAMERA_AI_CACHE_S", "300"))
MAX_IMAGES = 10

# Woodlands Causeway and Tuas Second Link (LTA camera IDs). Both photos of a crossing are read for both directions.
CROSSINGS = [
    {"id": "woodlands", "name": "Woodlands", "cams": ["2701", "2702"]},
    {"id": "tuas", "name": "Tuas", "cams": ["4703", "4713"]},
]
CHECKPOINT_IDS = {c for x in CROSSINGS for c in x["cams"]}
# Typical door-to-door crossing time (approach road + bridge + checkpoint) for each level, minutes.
CROSSING_MIN = {"Smooth": 15, "Medium": 30, "Jam": 60, "Super Jam": 90}

_CACHE = {}          # (camera id, photo time or link) -> (epoch, result dict)
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
        where = c.get("desc") or c.get("label") or ("Camera " + str(c["id"]))
        lines.append(f"Image {i + 1}: camera {c['id']} - {where}" + (" (Singapore-Johor border crossing)" if c["id"] in CHECKPOINT_IDS else ""))
    return ("You are a traffic analyst for a Singapore bus operations centre. Below are " + str(len(cams)) +
            " still images from LTA expressway traffic cameras, in order.\n" + "\n".join(lines) +
            "\n\nFor each image judge how congested the road is from vehicle density, queue length and spacing. "
            "Use exactly one of: Smooth, Medium, Jam, Super Jam (Super Jam = stationary queue filling the carriageway). "
            "For border-crossing images also judge each direction separately where visible: to_jb (traffic heading to Johor) and "
            "to_sg (traffic heading into Singapore); use null if that direction is not visible. If an image is unreadable "
            "(dark, blurred, frozen) use null for level.\n"
            'Reply with JSON only: [{"image":1,"level":"Medium","to_jb":null,"to_sg":null,"confidence":0.7}, ...]')


def parse_reply(text, n):
    """Model text -> {index: {level, to_jb, to_sg, confidence}} (index 0-based). Tolerates prose around the JSON."""
    if not text:
        return {}
    m = re.search(r"\[.*\]", text, re.S)
    try:
        rows = json.loads(m.group(0) if m else text)
    except (ValueError, TypeError):
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
    return out


def _key(c):
    return (str(c["id"]), str(c.get("taken") or c.get("link") or ""))


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


async def analyse(cams, fetch_image=None, ask_model=None, flow=None, now=None):
    """cams: camera dicts (id, link, lat, lon, taken, desc). Returns {"cameras": {id: result}, "crossings": [...], "ai": bool, "note": str}."""
    global _LOCK
    now = now or time.time()
    _LOCK = _LOCK or asyncio.Lock()
    cams = [c for c in cams if c and c.get("id") and c.get("link")][:MAX_IMAGES]
    results, todo = {}, []
    for c in cams:
        hit = _CACHE.get(_key(c))
        if hit and now - hit[0] < CACHE_S:
            results[str(c["id"])] = hit[1]
        else:
            todo.append(c)
    error = None
    if todo and ask_model and fetch_image:
        async with _LOCK:
            try:
                images = await asyncio.gather(*(fetch_image(c["link"]) for c in todo), return_exceptions=True)
                ok = [(c, img) for c, img in zip(todo, images) if not isinstance(img, BaseException) and img and img[0]]
                if ok:
                    content = []
                    for c, (data, mime) in ok:
                        content.append({"type": "image", "source": {"type": "base64", "media_type": (mime or "image/jpeg").split(";")[0],
                                                                    "data": base64.b64encode(data).decode()}})
                    content.append({"type": "text", "text": prompt([c for c, _ in ok])})
                    parsed = parse_reply(await ask_model(content), len(ok))
                    for i, (c, _) in enumerate(ok):
                        r = parsed.get(i)
                        if r and (r["level"] or r["to_jb"] or r["to_sg"]):
                            res = dict(r, source="AI camera analytics", model=MODEL, at=now)
                            _CACHE[_key(c)] = (now, res)
                            results[str(c["id"])] = res
            except Exception as e:      # network / quota / parse problems fall back to speed data
                error = "AI camera analytics unavailable: " + type(e).__name__
    if flow:
        for c in cams:
            if str(c["id"]) in results:
                continue
            try:
                f = await flow(c["lat"], c["lon"])
            except Exception:
                f = None
            lv = level_from_ratio((f or {}).get("ratio"))
            if lv:
                results[str(c["id"])] = {"level": lv, "to_jb": None, "to_sg": None, "confidence": (f or {}).get("conf"),
                                         "source": "TomTom speed", "speed": round(f["cur"]), "free_speed": round(f["free"]), "at": now}
    if len(_CACHE) > 500:
        for k in sorted(_CACHE, key=lambda k: _CACHE[k][0])[:200]:
            _CACHE.pop(k, None)
    return {"cameras": results, "crossings": crossings(results), "ai": bool(ask_model),
            "note": "Estimate from camera stills (road queue only, not the immigration hall). Level: Smooth / Medium / Jam / Super Jam.",
            "error": error, "updated": now}
