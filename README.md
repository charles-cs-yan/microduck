# Microduck Follow

A Python client module that lets a [Microduck](https://github.com/MHDuck/microduck) biped robot duck
follow a person and run a short stage routine by voice:

1. **"Let's go, follow me!"** — the duck follows the host on stage (ArUco marker tracking).
2. **"Let's party!"** — it dances on the spot.
3. **"Let's go home"** — it turns around and follows the host off stage.

讓 Microduck 雙足機器鴨聽英文指令跟著人走、跳舞、回家的 Python 模組。狀態：開發中，還在實機嘗試階段。

## What is here

| Path | Contents |
|---|---|
| `follow/` | The module: robotd client, safety gate (MotionGuard), tracker, voice keywords, ArUco vision worker, show script. See [`follow/README.md`](follow/README.md). |
| `follow/tests/` | 278 offline tests against a fake robotd; no hardware needed. |
| `docs/show-runbook.md` | One-page operator runbook for show day. |
| `docs/stage-demo.md` | Stage requirements and risks. |
| `docs/codebase-map.md` | Notes on the upstream code base and its IPC API. |
| `docs/devlog/` | Development and field-test log (HTML page). |
| `CLAUDE.md` | Working rules for the AI assistant used on this project, including the safety rules. |

## Design rules

- `follow/` is only ever a **client** of `robotd` (JSON-RPC over `/run/robotd.sock`). It does not modify motor, fall or policy code.
- Forward speed starts at 0.12 m/s, no sideways motion, and `robot.stop` is always sent on target loss or exit.
- Walking stops at 7.6 V; the duck is not asked to stand up at or below 7.5 V.
- Anything that moves the motors needs a person next to the duck.

## Running the tests

```sh
python3 -m unittest discover -s follow/tests -t .
```

## License

Apache-2.0, see [LICENSE](LICENSE) and [NOTICE](NOTICE). Parts are ports of code from the upstream
Microduck project, which is not included here.
