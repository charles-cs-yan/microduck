# CHANGELOG

## 2026-11-26|Boot standby, timed home turn

### Changed

- **Scripted home turn is timed** (`follow/turning.py` `TimedTurn`): `h` / "Let's go home" turns at 0.4 rad/s for a fixed time (default 8.7 s for 180°; `--turn-seconds`, or `STANDBY_TURN_S` for the boot standby), then walks straight back. On the duck robotd applied the full turn rate, but `odom.yaw` (IMU-integrated, half its reads stale) wobbled ±0.1 rad and drifted the wrong way, so the closed-loop turn gave up within 2 s every time. The odom change is now only logged. `--turn-seconds 0` keeps the closed loop.
- A rise counts as done only after 3 s of steady standing (robotd's limp-fall misfires right after a seated rise).
- Boot standby: PATH set for cron, battery read waits up to 3 min.

### Found on the duck

- Walking cut power to the whole board: a ribbon cable had come loose. Plugged back in and fixed in place.
- Seated rises can fall: robotd's `limp_fall` fires about 0.3 s after the rise, with the trunk still pitched about 45°. Turning it off in `/etc/robot/robotd.toml` is approved for the show but not applied yet.

### Added

- **Boot standby** (`follow/standby.sh`, `console.sh`, `standby_install.sh`): the scripted show starts at boot from one user-crontab `@reboot` line (no sudo) and waits. Nothing connects to robotd until a console sends `yes`; keys then arrive on `/tmp/microduck-keys`. If the console or the laptop voice link drops, the duck stops.
- `follow.app --standby --key-pipe FIFO`; `start_show.ps1` opens the console when standby is running.

## 2026-11-24|First steps on the real duck, the show routine, public repository

### Added

- **Dance mode** (`d`, "Let's party", "Shake it"): 4 s keyframed loop of body sway (±0.15 rad roll), nods, mouth chatter and sounds, up to 30 s, on the spot. Starts only when the duck is standing still; voice "dance" mid-pulse waits up to 3 s for the duck to stop.
- **Context-aware "Let's go"**: from standby it means follow; from a dance or gaze it means go home.
- `--home-turn DEG` (0 or ±30..±180; 0 skips the fixed turn and follows straight away) and `--kws-threshold` (0.1..0.6).
- Full-show rehearsal test (`test_show_rehearsal.py`) against a 1-D world model; measures an average follow speed of about 0.073 m/s.
- GitHub Actions workflow running the offline suite on every push.

### Fixed

- `--forward` only raised MotionGuard's cap; the tracker still asked for 0.12 m/s. It now reaches the tracker (capped at 0.25).
- A keystroke doubled by a terminal (`uu`) was rejected as unknown; a line of one repeated character now counts as one press. The unknown-command hint now lists `d`.

### On the duck (held by hand)

- [x] Stand up from "held": up in 1.1 s; fall protection then fired four times in 7 s before settling. Cause not yet known.
- [x] Forward 0.05 m/s: no motion. `robotd` treats ≤ 0.05 as standing.
- [x] Forward 0.08 m/s: steps in place.
- [x] Forward 0.12 m/s: walks about 22 cm in 3 s; odometry reports a 150° yaw change, not yet confirmed by eye.
- [x] Sit (`sit_toggle`): OK.
- [ ] Crouch: deferred, battery fell to 7.27 V.

### Known limits

- Voice has only been tested with synthetic speech. "Let's dance" is not recognised; a lone "Let's go" and "Stop" fail in loud noise.
- The charger has not exceeded 7.77 V (nominal full is 8.4 V).
- Standing with the media daemon running reads 84–85 °C against a 90 °C gate.

## 2026-11-23|Main app, voice, vision, three review rounds

### Added

- `app.py` mode machine with keyboard backup; tracker ported from upstream `vision_follow` (84,726 steps identical between the JS and Python versions).
- Odometry closed-loop turns, greeting routine, `stand.py`, status line, `deploy.sh`, `show.sh`.
- English keyword spotting on sherpa-onnx GigaSpeech 3.3M; ArUco worker reading the camera directly, with mediad's auto-exposure ported.
- Posture and skills: sit, stand, crouch, pick up, roll, kicks.

### Fixed (independent reviews, 29 findings in total)

- Race between `robot.stop` and `robot.move`.
- `robot.stop` does not cancel a skill: posture logic now follows the policy `robotd` reports.
- `robot.pose` has no deadman: poses are re-checked under the lock and released explicitly.

## 2026-11-22|Survey and core

### Added

- Read-only survey of the board, codebase map, stage requirements.
- `robotd_client.py`, `safety.py`, `motion.py` (MotionGuard), `probe.py`. 67 tests.
