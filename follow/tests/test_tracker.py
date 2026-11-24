"""Port of microduck/vision_follow/test_core.cjs and test_follow_cycle.cjs (tracker parts).

The state-gate tests of test_core.cjs live in test_safety.py (v18 fields).
"""

import copy
import math
import unittest

from follow.tracker import Decision, FollowCycle, Tracker, TrackerOptions, boxes_from


def picture(x=0.0, y=0.0, size=0.4, w=720, h=1280):
    return {"width": w, "height": h, "boxes": [{
        "x0": (x + 1) * w / 2 - w * .1, "x1": (x + 1) * w / 2 + w * .1,
        "y0": (y + 1) * h / 2 - h * size / 2, "y1": (y + 1) * h / 2 + h * size / 2, "score": 1.3}]}


def acquire(tracker, p):
    tracker.ingest(p, 0)
    tracker.ingest(p, .5)


def sign(v):
    return (v > 0) - (v < 0)


class TrackerTest(unittest.TestCase):
    def test_front_alignment_optional(self):
        for front_gate in (True, False):
            t = Tracker(front_gate=front_gate)
            t.reset([0, 0, .4, 0])
            acquire(t, picture(.5, 0, .2))
            out = t.output(.6, "follow", .1)
            self.assertEqual(out.desired_vx, 0 if front_gate else .12)
            self.assertLessEqual(abs(out.head[2]), .6)
        with self.assertRaises(ValueError):
            Tracker(front_gate="false")

    def test_no_alignment_still_rejects_clipped_and_missing_metric(self):
        t = Tracker(front_gate=False)
        acquire(t, picture(.85, 0, .2))
        self.assertEqual(t.output(.6, "follow", .1).desired_vx, 0)
        metric = Tracker(front_gate=False, distance_mode="meters")
        acquire(metric, picture(.5, 0, .2))
        self.assertEqual(metric.output(.6, "follow", .1).desired_vx, 0)

    def test_resolution_invariant(self):
        a = boxes_from(picture(.3, -.2, .3))[0]
        b = boxes_from(picture(.3, -.2, .3, 1280, 720))[0]
        for k in ("x", "y", "size"):
            self.assertLess(abs(getattr(a, k) - getattr(b, k)), 1e-12)

    def test_two_frames_and_gaze_never_translates(self):
        t = Tracker()
        t.ingest(picture(.4), 0)
        self.assertFalse(t.output(0, "follow", .1).valid)
        t.ingest(picture(.4), .5)
        out = t.output(.5, "gaze", .1)
        self.assertEqual(out.vx, 0)
        self.assertLess(out.head[2], 0)
        self.assertEqual(out.head[1], 0)
        self.assertLessEqual(abs(out.head[2]), .035 + 1e-12)

    def test_head_integrates_once_per_detection(self):
        t = Tracker()
        acquire(t, picture(.5))
        aim = t.aim[2]
        for i in range(6):
            t.output(.5 + i * .1, "gaze", .1)
        self.assertEqual(t.aim[2], aim)
        self.assertLessEqual(abs(t.head[2]), abs(aim) + 1e-12)

    def test_vertical_opt_in_and_inversion(self):
        a = Tracker(vertical=True)
        b = Tracker(vertical=True, invert_pitch=True, invert_yaw=True)
        acquire(a, picture(.3, .3, .3))
        acquire(b, picture(.3, .3, .3))
        x, y = a.output(.5, "gaze", .1), b.output(.5, "gaze", .1)
        self.assertTrue(x.head[1] > 0 and x.head[2] < 0)
        self.assertEqual(x.head[1], -y.head[1])
        self.assertEqual(x.head[2], -y.head[2])

    def test_size_direction(self):
        for size, expected in ((.2, 1), (.6, -1), (.4, 0)):
            t = Tracker()
            acquire(t, picture(0, 0, size))
            o = t.output(.5, "follow", .1)
            self.assertEqual(sign(o.vx), expected, size)
            self.assertEqual((o.vy, o.vyaw), (0, 0))

    def test_stale_empty_malformed_stop(self):
        for kind in ("stale", "empty", "malformed"):
            t = Tracker()
            acquire(t, picture(0, 0, .2))
            self.assertGreater(t.output(.5, "follow", .1).vx, 0)
            if kind == "empty":
                t.ingest({"width": 720, "height": 1280, "boxes": []}, .6)
            if kind == "malformed":
                t.ingest({"width": 0, "height": 0, "boxes": []}, .6)
            out = t.output(1.8 if kind == "stale" else .6, "follow", .1)
            self.assertEqual(out.vx, 0, kind)
            self.assertFalse(out.valid, kind)

    def test_edge_or_turned_head_never_translates(self):
        a = Tracker()
        acquire(a, picture(.5, 0, .2))
        self.assertEqual(a.output(.5, "follow", .1).vx, 0)
        b = Tracker()
        b.reset([0, 0, .3, 0])
        acquire(b, picture(0, 0, .2))
        self.assertEqual(b.output(.5, "follow", .1).vx, 0)
        c = Tracker()
        acquire(c, picture(0, .6, .8))
        self.assertEqual(c.output(.5, "follow", .1).vx, 0)

    def test_hysteresis(self):
        t = Tracker()
        acquire(t, picture(0, 0, .3))
        self.assertGreater(t.output(.5, "follow", .1).vx, 0)
        for i in range(1, 9):
            t.ingest(picture(0, 0, .4), .5 + .1 * i)
            t.output(.5 + .1 * i, "follow", .1)
        self.assertEqual(t.velocity, 0)
        for i in range(1, 7):
            t.ingest(picture(0, 0, .38 if i % 2 else .42), 1.3 + .1 * i)
            self.assertEqual(t.output(1.3 + .1 * i, "follow", .1).vx, 0)

    def test_reverse_never_continues_forward(self):
        t = Tracker()
        acquire(t, picture(0, 0, .3))
        for i in range(6):
            t.output(.5 + i * .05, "follow", .1)
        self.assertGreater(t.velocity, 0)
        t.ingest(picture(0, 0, .6), .9)
        self.assertLess(t.output(.9, "follow", .1).vx, 0)

    def test_reacquisition_needs_confirmation(self):
        t = Tracker()
        acquire(t, picture(-.45, 0, .3))
        self.assertIsNone(t.ingest(picture(.45, 0, .3), .6))
        t.ingest(picture(.45, 0, .3), .7)
        self.assertFalse(t.output(.7, "follow", .1).valid)
        t.ingest(picture(.45, 0, .3), .8)
        self.assertTrue(t.output(.8, "follow", .1).valid)

    def test_calibration(self):
        t = Tracker()
        with self.assertRaises(ValueError):
            t.calibrate(0)
        acquire(t, picture(0, 0, .3))
        self.assertLess(abs(t.calibrate(.5) - .3), 1e-10)
        with self.assertRaises(ValueError):
            t.calibrate(2)
        with self.assertRaises(ValueError):
            Tracker(forward=1)
        with self.assertRaises(ValueError):
            Tracker(gain=float("nan"))

    def test_invalid_numbers_never_enter(self):
        self.assertEqual(len(boxes_from(picture())), 1)
        p = picture()
        p["boxes"][0]["score"] = float("nan")
        self.assertEqual(boxes_from(p), [])
        q = picture()
        q["boxes"][0]["x1"] = float("inf")
        self.assertEqual(boxes_from(q), [])
        for bad in (None, [], {"width": "720", "height": 1280, "boxes": []}, {"width": 720, "height": 1280, "boxes": "x"}):
            self.assertEqual(boxes_from(bad), [])

    def test_marker_id_lock_and_metric_direction(self):
        for distance, expected in ((1, 1), (.3, -1), (.6, 0)):
            t = Tracker(target_id=0, distance_mode="meters")
            p = picture(0, 0, .2)
            p["boxes"][0].update(id=1, distance_status="calibrated", distance_m=distance)
            t.ingest(p, 0)
            self.assertIsNone(t.target)
            p["boxes"][0]["id"] = 0
            acquire(t, p)
            self.assertEqual(sign(t.output(.5, "follow", .1).vx), expected, distance)

    def test_metric_failure_does_not_fall_back(self):
        t = Tracker(target_id=0, distance_mode="meters")
        p = picture(0, 0, .2)
        p["boxes"][0].update(id=0, distance_status="calibrated", distance_m=1)
        acquire(t, p)
        self.assertGreater(t.output(.5, "follow", .1).vx, 0)
        p["boxes"][0]["distance_status"] = "pose_rejected"
        t.ingest(p, .7)
        self.assertEqual(t.output(.7, "follow", .1).vx, 0)
        self.assertIsNone(t.filtered_distance)
        with self.assertRaises(ValueError):
            t.calibrate(.7)

    def test_duplicate_id_is_ambiguous(self):
        t = Tracker(target_id=0)
        p = picture()
        p["boxes"][0]["id"] = 0
        twin = copy.deepcopy(p["boxes"][0])
        twin["x0"] += 30
        twin["x1"] += 30
        p["boxes"].append(twin)
        acquire(t, p)
        self.assertIsNone(t.target)
        self.assertEqual(t.output(.5, "follow", .1).vx, 0)

    def test_never_vy_or_vyaw(self):
        t = Tracker(front_gate=False)
        acquire(t, picture(.3, 0, .2))
        for i in range(10):
            out = t.output(.5 + i * .05, "follow", .1)
            self.assertEqual((out.vy, out.vyaw), (0.0, 0.0))


def decision(desired_vx=.12, valid=True):
    return Decision(head=[0, 0, 0, 0], vx=.12, desired_vx=desired_vx, reason="target", valid=valid)


def ready(test, cycle, now=0):
    cycle.start()
    cycle.acknowledge_stop(now)
    test.assertTrue(cycle.can_capture(now + 1, now + 1))


class FollowCycleTest(unittest.TestCase):
    def test_capture_waits_for_ack_settle_and_new_frame(self):
        c = FollowCycle()
        c.start()
        self.assertFalse(c.can_capture(10, 10))
        self.assertEqual(c.update(decision(), 10, 10).vx, 0)
        c.acknowledge_stop(10)
        self.assertFalse(c.can_capture(10.9, 10.9))
        self.assertFalse(c.can_capture(12, 10.9), "a pre-settle frame cannot be reused")
        self.assertTrue(c.can_capture(12, 12))
        self.assertFalse(c.accepts(10.5, 12))

    def test_one_result_one_pulse(self):
        c = FollowCycle(move_seconds=.8)
        ready(self, c)
        self.assertEqual(c.update(decision(), 1, 3).phase, "moving")
        self.assertGreater(c.update(decision(), 1, 3.4).vx, 0)
        self.assertFalse(c.can_capture(3.4, 3.4))
        self.assertEqual(c.update(decision(), 1, 3.81).vx, 0)
        self.assertEqual(c.phase, "stopping")
        self.assertEqual(c.update(decision(), 1, 4).vx, 0)
        c.acknowledge_stop(4)
        self.assertTrue(c.can_capture(5, 5))
        self.assertEqual(c.update(decision(), 1, 5).vx, 0, "used result cannot start another pulse")
        self.assertEqual(c.update(decision(-.1), 5, 5.2).phase, "moving")
        self.assertLess(c.update(decision(-.1), 5, 5.5).vx, 0)

    def test_bad_decisions_never_start(self):
        for out, captured, now in ((decision(.12, False), 1, 2), (decision(0), 1, 2),
                                   (decision(), 1, 7), (decision(), .5, 2), (decision(), 3, 2)):
            c = FollowCycle()
            ready(self, c)
            self.assertEqual(c.update(out, captured, now).vx, 0)
            self.assertEqual(c.phase, "detecting")

    def test_loss_or_reversal_brakes(self):
        for out in (decision(.12, False), decision(-.1), decision(0)):
            c = FollowCycle()
            ready(self, c)
            c.update(decision(), 1, 2)
            self.assertEqual(c.update(out, 1, 2.1).vx, 0)
            self.assertEqual(c.phase, "stopping")

    def test_default_segment(self):
        c = FollowCycle()
        ready(self, c)
        c.update(decision(), 1, 2)
        self.assertEqual(c.move_seconds, 2.5)
        self.assertLess(abs(c.update(decision(), 1, 2.4).vx - .12), 1e-8)
        for t in (2.8, 3.2, 3.8, 4.4):
            self.assertEqual(c.update(decision(), 1, t).vx, .12)
        self.assertEqual(c.deadline, 4.5, "polling an old decision must not extend the segment")
        self.assertEqual(c.update(decision(), 1, 4.5).vx, 0)
        self.assertEqual(c.stop_reason, "移動段到時")
        self.assertEqual(c.peak_speed, .12)

    def test_reset_and_limits(self):
        c = FollowCycle()
        ready(self, c)
        c.update(decision(), 1, 2)
        c.reset()
        self.assertEqual(c.update(decision(), 1, 2.2).vx, 0)
        self.assertEqual(c.phase, "idle")
        with self.assertRaises(ValueError):
            FollowCycle(move_seconds=10)
        with self.assertRaises(ValueError):
            FollowCycle(settle_seconds=0)

    def test_tracker_and_cycle_together(self):
        """End to end: a far marker produces one bounded forward pulse, then a stop."""
        t, c = Tracker(), FollowCycle(move_seconds=1.0)
        c.start()
        c.acknowledge_stop(0)
        self.assertTrue(c.can_capture(1, 1))
        t.ingest(picture(0, 0, .2), 1.0)
        t.ingest(picture(0, 0, .2), 1.1)
        out = c.update(t.output(1.1, "follow", .1), 1.1, 1.1)
        self.assertEqual(out.phase, "moving")
        speeds = []
        for i in range(1, 15):
            now = 1.1 + i * .1
            t.ingest(picture(0, 0, .2), now)
            speeds.append(c.update(t.output(now, "follow", .1), 1.1, now).vx)
        self.assertTrue(all(0 <= v <= .12 for v in speeds))
        self.assertEqual(speeds[-1], 0)
        self.assertEqual(c.phase, "stopping")
        self.assertTrue(math.isclose(max(speeds), .12) or max(speeds) < .12)


if __name__ == "__main__":
    unittest.main()
