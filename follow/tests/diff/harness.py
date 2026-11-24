"""Differential test harness: the same random scenarios through upstream core.js and tracker.py.

`generate(seed, n)` builds JSON scenarios; `run_python(scenarios)` mirrors run_core.cjs;
`compare(js, py)` returns the first divergence (or None). Reasons are compared after mapping
upstream's Simplified Chinese strings to ours.
"""

from __future__ import annotations

import math
import random
from typing import Any, Dict, List, Optional

from follow.tracker import Decision, FollowCycle, Tracker, TrackerOptions

REASONS = {
    "原地注视": "原地注視", "目标丢失或等待连续两帧": "目標遺失或等待連續兩幀",
    "目标被裁切，停止平移": "目標被裁切，停止平移", "目标偏离机身正前方，仅转头": "目標偏離機身正前方，只轉頭",
    "没有有效标定距离，停止平移": "沒有有效的標定距離，停止平移",
    "目标较远，前进": "目標較遠，前進", "目标较小，前进": "目標較小，前進",
    "目标较近，后退": "目標較近，後退", "目标较大，后退": "目標較大，後退", "距离合适，停止": "距離合適，停止",
    "短程前进，到时停步": "短程前進，到時停步", "短程后退，到时停步": "短程後退，到時停步",
    "停步，等待零速度应答": "停步，等待零速度確認", "已停步，等待稳定及新画面": "已停步，等待穩定與新畫面",
    "移动段到时": "移動段到時", "目标确认过期或失效": "目標確認過期或失效", "外部停止": "外部停止",
}


def map_reason(text: Optional[str]) -> Optional[str]:
    if text is None:
        return None
    if text.startswith("停步识别："):
        return "停步辨識：" + map_reason(text[len("停步识别："):])
    return REASONS.get(text, text)


OPTION_NAMES = {"targetSize": "target_size", "sizeBand": "size_band", "headRate": "head_rate",
                "yawLimit": "yaw_limit", "pitchLimit": "pitch_limit", "invertYaw": "invert_yaw",
                "invertPitch": "invert_pitch", "frontGate": "front_gate", "targetId": "target_id",
                "distanceMode": "distance_mode", "targetDistance": "target_distance",
                "distanceBand": "distance_band"}


# ── generation ───────────────────────────────────────────────────────────────
def _options(rng: random.Random) -> Dict[str, Any]:
    o = {"targetSize": rng.uniform(.15, .7), "sizeBand": rng.uniform(.05, .35),
         "forward": rng.uniform(0, .25), "backward": rng.uniform(0, .25), "timeout": rng.uniform(.6, 15),
         "deadzone": rng.uniform(.02, .3), "gain": rng.uniform(.05, 2), "headRate": rng.uniform(.05, .6),
         "yawLimit": rng.uniform(.05, .7), "pitchLimit": rng.uniform(.05, .4), "vertical": rng.random() < .4,
         "invertYaw": rng.random() < .3, "invertPitch": rng.random() < .3, "frontGate": rng.random() < .5,
         "targetId": rng.choice([None, None, 0, 1, 2]), "distanceMode": rng.choice(["size", "size", "meters"]),
         "targetDistance": rng.uniform(.2, 3), "distanceBand": rng.uniform(.03, .3)}
    if rng.random() < .05:  # occasionally invalid: both sides must refuse
        o[rng.choice(["forward", "gain", "timeout"])] = rng.choice([1.0, -1.0, 100.0])
    return o


def _picture(rng: random.Random, target: Dict[str, float], ids: List[int]) -> Any:
    if rng.random() < .04:
        return rng.choice([None, {"width": 0, "height": 10, "boxes": []}, {"width": 720, "height": 1280, "boxes": "x"},
                           {"width": "720", "height": 1280, "boxes": []}])
    w, h = rng.choice([(720, 1280), (1280, 720), (640, 360)])
    boxes = []
    for i in range(rng.choice([0, 1, 1, 1, 2, 3])):
        if i == 0 and rng.random() < .85:
            cx, cy, size = target["x"], target["y"], target["size"]
        else:
            cx, cy, size = rng.uniform(-1, 1), rng.uniform(-1, 1), rng.uniform(.02, .9)
        half_w, half_h = rng.uniform(.01, .2), size / 2
        box = {"x0": (cx + 1) * w / 2 - w * half_w, "x1": (cx + 1) * w / 2 + w * half_w,
               "y0": (cy + 1) * h / 2 - h * half_h, "y1": (cy + 1) * h / 2 + h * half_h,
               "score": rng.choice([1.0, 0.7, 1.3])}
        box = {k: (round(v, 3) if isinstance(v, float) else v) for k, v in box.items()}
        if rng.random() < .7:
            box["id"] = rng.choice(ids)
        if rng.random() < .5:
            box["size_norm"] = rng.choice([round(size, 4), 0, 1.5])
        if rng.random() < .4:
            box["distance_status"] = rng.choice(["calibrated", "calibrated", "pose_rejected", "uncalibrated"])
            box["distance_m"] = round(rng.uniform(.02, 12), 3)
        if rng.random() < .03:
            box.pop(rng.choice(["x0", "score"]))
        boxes.append(box)
    return {"width": w, "height": h, "boxes": boxes}


def _coherent_picture(rng: random.Random, target: Dict[str, float], ident) -> Dict[str, Any]:
    """A clean frame: the host's marker where the target is, sometimes one distractor."""
    w, h = 720, 1280
    boxes = []
    for cx, cy, size, i in [(target["x"], target["y"], target["size"], ident)] + (
            [(rng.uniform(-.9, .9), rng.uniform(-.9, .9), rng.uniform(.05, .5), 7)] if rng.random() < .2 else []):
        box = {"x0": (cx + 1) * w / 2 - w * .08, "x1": (cx + 1) * w / 2 + w * .08,
               "y0": (cy + 1) * h / 2 - h * size / 2, "y1": (cy + 1) * h / 2 + h * size / 2, "score": 1.0}
        box = {k: round(v, 3) for k, v in box.items()}
        if i is not None:
            box["id"] = i
        if rng.random() < .5:
            box["distance_status"], box["distance_m"] = "calibrated", round(.3 / max(size, .05), 3)
        boxes.append(box)
    return {"width": w, "height": h, "boxes": boxes}


def _tracker_scenario(rng: random.Random) -> Dict[str, Any]:
    options = _options(rng)
    coherent = rng.random() < .5
    if coherent:  # the realistic case: one host, mostly visible, driving forward/back and turning the head
        options.update(timeout=rng.uniform(1, 12), forward=rng.uniform(.05, .25), backward=rng.uniform(.05, .25))
    target = {"x": rng.uniform(-.6, .6), "y": rng.uniform(-.4, .4), "size": rng.uniform(.08, .75)}
    ident = options["targetId"] if options["targetId"] is not None else rng.choice([None, 0])
    t, steps = 0.0, []
    for _ in range(rng.randint(5, 60) if not coherent else rng.randint(30, 120)):
        if coherent:
            t = round(t + rng.choice([.1, .1, .1, .2, .5]), 4)
        else:
            t = round(max(0.0, t + rng.choice([rng.uniform(0, .7), rng.uniform(0, .2), 0.0, -.05, 3.0])), 4)
        target = {k: min(.9, max(-.9, v + rng.uniform(-.06, .06))) for k, v in target.items()}
        target["size"] = min(.85, max(.05, target["size"]))
        r = rng.random()
        if coherent:
            if r < .5:
                params = _coherent_picture(rng, target, ident) if rng.random() < .9 else {"width": 720, "height": 1280, "boxes": []}
                steps.append({"op": "ingest", "t": t, "params": params})
            elif r < .97:
                steps.append({"op": "output", "t": t, "mode": "follow" if rng.random() < .85 else "gaze", "dt": .1})
            else:
                steps.append({"op": "calibrate", "t": t})
            continue
        if r < .45:
            steps.append({"op": "ingest", "t": t, "params": _picture(rng, target, [0, 1, 2])})
        elif r < .9:
            steps.append({"op": "output", "t": t, "mode": rng.choice(["follow", "follow", "gaze"]),
                          "dt": round(rng.uniform(0, .3), 4)})
        elif r < .96:
            steps.append({"op": "calibrate", "t": t})
        else:
            steps.append({"op": "reset", "head": [round(rng.uniform(-.3, .3), 3) for _ in range(4)]})
    return {"kind": "tracker", "options": options, "steps": steps}


def _cycle_scenario(rng: random.Random) -> Dict[str, Any]:
    options = {"moveSeconds": rng.uniform(.2, 4), "settleSeconds": rng.uniform(.3, 5), "maxAge": rng.uniform(.8, 15)}
    if rng.random() < .05:
        options["moveSeconds"] = 10
    reasons = list(REASONS)[:10]
    t, steps = 0.0, [{"op": "start"}]
    if rng.random() < .5:  # the realistic loop: stop acked, settle, fresh capture, pulse, repeat
        for _ in range(rng.randint(3, 8)):
            t = round(t + .1, 4)
            steps.append({"op": "ack", "t": t})
            t = round(t + options["settleSeconds"] + rng.uniform(0, .5), 4)
            steps.append({"op": "capture", "t": t, "video": t})
            for _ in range(rng.randint(5, 45)):
                out = {"desiredVx": rng.choice([.12, .12, -.1, 0]), "vx": .1, "valid": rng.random() < .95,
                       "reason": rng.choice(reasons), "head": [0, 0, 0, 0]}
                steps.append({"op": "update", "out": out, "captured": round(t - .05, 4), "t": t})
                t = round(t + .1, 4)
        return {"kind": "cycle", "options": options, "steps": steps}
    for _ in range(rng.randint(5, 50)):
        t = round(t + rng.uniform(0, .8), 4)
        r = rng.random()
        if r < .1:
            steps.append({"op": "ack", "t": t})
        elif r < .3:
            steps.append({"op": "capture", "t": t, "video": round(t - rng.uniform(0, 2), 4)})
        elif r < .35:
            steps.append({"op": "accepts", "captured": round(t - rng.uniform(-1, 6), 4), "t": t})
        elif r < .9:
            out = {"desiredVx": rng.choice([0, .12, -.1, .05, .3]), "vx": .1, "valid": rng.random() < .85,
                   "reason": rng.choice(reasons), "head": [0, 0, 0, 0]}
            steps.append({"op": "update", "out": out, "captured": round(t - rng.uniform(-.5, 6), 4), "t": t})
        elif r < .95:
            steps.append({"op": "brake", "reason": rng.choice(reasons)})
        elif r < .98:
            steps.append({"op": "start"})
        else:
            steps.append({"op": "reset"})
    return {"kind": "cycle", "options": options, "steps": steps}


def generate(seed: int, n: int) -> List[Dict[str, Any]]:
    rng = random.Random(seed)
    return [(_tracker_scenario if i % 2 == 0 else _cycle_scenario)(rng) for i in range(n)]


# ── python runner (mirror of run_core.cjs) ───────────────────────────────────
def _num(v):
    return None if isinstance(v, float) and not math.isfinite(v) else v


def _nums(a):
    return [_num(v) for v in a] if isinstance(a, list) else a


def _tracker_snap(t: Tracker):
    return {"aim": _nums(t.aim), "head": _nums(t.head), "velocity": _num(t.velocity), "direction": t.direction,
            "confirmed": t.confirmed, "filteredSize": _num(t.filtered_size),
            "filteredDistance": _num(t.filtered_distance), "lastDetection": _num(t.last_detection),
            "targetId": t.target.id if t.target else None}


def _cycle_snap(c: FollowCycle):
    return {"phase": c.phase, "speed": _num(c.speed), "deadline": _num(c.deadline),
            "captureAfter": _num(c.capture_after), "used": _num(c.used), "peakSpeed": _num(c.peak_speed),
            "stopReason": c.stop_reason, "plannedSpeed": _num(c.planned_speed)}


def _run_tracker(s):
    try:
        t = Tracker(TrackerOptions(**{OPTION_NAMES.get(k, k): v for k, v in s["options"].items()}))
    except (ValueError, TypeError):
        return {"constructError": True}
    out = []
    for step in s["steps"]:
        try:
            if step["op"] == "ingest":
                b = t.ingest(step["params"], step["t"])
                r = {"selected": None if b is None else {"x": b.x, "y": b.y, "size": b.size, "id": b.id,
                                                         "distance": b.distance, "clipped": b.clipped}}
            elif step["op"] == "output":
                o = t.output(step["t"], step["mode"], step["dt"])
                r = {"head": _nums(o.head), "vx": o.vx, "desiredVx": o.desired_vx, "vy": o.vy, "vyaw": o.vyaw,
                     "reason": o.reason, "valid": o.valid}
            elif step["op"] == "calibrate":
                r = {"value": t.calibrate(step["t"])}
            else:
                t.reset(step["head"])
                r = {}
        except ValueError:
            r = {"error": True}
        out.append({"r": r, "snap": _tracker_snap(t)})
    return {"steps": out}


def _run_cycle(s):
    o = s["options"]
    try:
        c = FollowCycle(move_seconds=o["moveSeconds"], settle_seconds=o["settleSeconds"], max_age=o["maxAge"])
    except ValueError:
        return {"constructError": True}
    out = []
    for step in s["steps"]:
        op = step["op"]
        if op == "start":
            c.start(); r = {}
        elif op == "reset":
            c.reset(); r = {}
        elif op == "brake":
            c.brake(map_reason(step["reason"])); r = {}
        elif op == "ack":
            r = {"value": c.acknowledge_stop(step["t"])}
        elif op == "capture":
            r = {"value": c.can_capture(step["t"], step["video"])}
        elif op == "accepts":
            r = {"value": c.accepts(step["captured"], step["t"])}
        else:
            d = step["out"]
            decision = Decision(head=d["head"], vx=d["vx"], desired_vx=d["desiredVx"],
                                reason=map_reason(d["reason"]), valid=d["valid"])
            res = c.update(decision, step["captured"], step["t"])
            r = {"vx": res.vx, "reason": res.reason, "phase": res.phase}
        out.append({"r": r, "snap": _cycle_snap(c)})
    return {"steps": out}


def run_python(scenarios):
    return [_run_tracker(s) if s["kind"] == "tracker" else _run_cycle(s) for s in scenarios]


# ── comparison ───────────────────────────────────────────────────────────────
def _same(js, py, path, tol=1e-9):
    if isinstance(js, bool) or isinstance(py, bool):
        return None if js == py else f"{path}: js={js!r} py={py!r}"
    if isinstance(js, (int, float)) and isinstance(py, (int, float)):
        return None if abs(js - py) <= tol * (1 + abs(js)) else f"{path}: js={js!r} py={py!r}"
    if isinstance(js, str) or isinstance(py, str):
        mapped = map_reason(js) if isinstance(js, str) else js
        return None if mapped == py else f"{path}: js={js!r} (→{mapped!r}) py={py!r}"
    if isinstance(js, dict) and isinstance(py, dict):
        for key in sorted(set(js) | set(py)):
            if key not in js or key not in py:
                return f"{path}.{key}: only in {'js' if key in js else 'py'}"
            problem = _same(js[key], py[key], f"{path}.{key}", tol)
            if problem:
                return problem
        return None
    if isinstance(js, list) and isinstance(py, list):
        if len(js) != len(py):
            return f"{path}: length js={len(js)} py={len(py)}"
        for i, (a, b) in enumerate(zip(js, py)):
            problem = _same(a, b, f"{path}[{i}]", tol)
            if problem:
                return problem
        return None
    return None if js == py else f"{path}: js={js!r} py={py!r}"


def compare(js_results, py_results) -> Optional[str]:
    return _same(js_results, py_results, "")
