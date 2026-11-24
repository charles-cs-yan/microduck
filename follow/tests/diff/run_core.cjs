// Run differential scenarios against upstream vision_follow/core.js.
//   node run_core.cjs <path/to/core.js> <scenarios.json> <results.json>
"use strict";
const fs = require("fs");
const [coreJs, inPath, outPath] = process.argv.slice(2);
const { Tracker, FollowCycle } = require(require("path").resolve(coreJs));

const num = v => (typeof v === "number" && !Number.isFinite(v) ? null : v);
const nums = a => (Array.isArray(a) ? a.map(num) : a);

function trackerSnap(t) {
  return { aim: nums(t.aim), head: nums(t.head), velocity: num(t.velocity), direction: t.direction,
    confirmed: t.confirmed, filteredSize: num(t.filteredSize), filteredDistance: num(t.filteredDistance),
    lastDetection: num(t.lastDetection), targetId: t.target ? t.target.id : null };
}
function boxSnap(b) {
  return b ? { x: b.x, y: b.y, size: b.size, id: b.id, distance: b.distance, clipped: b.clipped } : null;
}
function cycleSnap(c) {
  return { phase: c.phase, speed: num(c.speed), deadline: num(c.deadline), captureAfter: num(c.captureAfter),
    used: num(c.used), peakSpeed: num(c.peakSpeed), stopReason: c.stopReason, plannedSpeed: num(c.plannedSpeed) };
}
function runTracker(s) {
  let t;
  try { t = new Tracker(s.options); } catch (e) { return { constructError: true }; }
  const out = [];
  for (const step of s.steps) {
    let r;
    try {
      if (step.op === "ingest") r = { selected: boxSnap(t.ingest(step.params, step.t)) };
      else if (step.op === "output") {
        const o = t.output(step.t, step.mode, step.dt);
        r = { head: nums(o.head), vx: o.vx, desiredVx: o.desiredVx, vy: o.vy, vyaw: o.vyaw, reason: o.reason, valid: o.valid };
      } else if (step.op === "calibrate") r = { value: t.calibrate(step.t) };
      else if (step.op === "reset") { t.reset(step.head); r = {}; }
    } catch (e) { r = { error: true }; }
    out.push({ r, snap: trackerSnap(t) });
  }
  return { steps: out };
}
function runCycle(s) {
  let c;
  try { c = new FollowCycle(s.options); } catch (e) { return { constructError: true }; }
  const out = [];
  for (const step of s.steps) {
    let r;
    if (step.op === "start") { c.start(); r = {}; }
    else if (step.op === "reset") { c.reset(); r = {}; }
    else if (step.op === "brake") { c.brake(step.reason); r = {}; }
    else if (step.op === "ack") r = { value: c.acknowledgeStop(step.t) };
    else if (step.op === "capture") r = { value: c.canCapture(step.t, step.video) };
    else if (step.op === "accepts") r = { value: c.accepts(step.captured, step.t) };
    else if (step.op === "update") {
      const o = c.update(step.out, step.captured, step.t);
      r = { vx: o.vx, reason: o.reason, phase: o.phase };
    }
    out.push({ r, snap: cycleSnap(c) });
  }
  return { steps: out };
}
const scenarios = JSON.parse(fs.readFileSync(inPath, "utf8"));
const results = scenarios.map(s => (s.kind === "tracker" ? runTracker(s) : runCycle(s)));
fs.writeFileSync(outPath, JSON.stringify(results));
