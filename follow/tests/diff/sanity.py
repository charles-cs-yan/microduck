"""Check the differential harness itself: coverage of the scenarios, and that it catches mutations.

    PATH=~/mdev/node/bin:$PATH python3 -m follow.tests.diff.sanity
"""

import collections

import follow.tracker as tr
from follow.tests.diff.harness import compare, generate, run_python
from follow.tests.test_differential import run_js


def first_divergence(js, scenarios):
    return next((p for a, b in zip(js, run_python(scenarios)) if (p := compare(a, b))), None)


def main():
    scenarios = [s for seed in (1, 2, 3) for s in generate(seed, 400)]
    js = run_js(scenarios)
    stats = collections.Counter()
    for s, r in zip(scenarios, js):
        if "constructError" in r:
            stats[s["kind"] + "_construct_error"] += 1
            continue
        for step, x in zip(s["steps"], r["steps"]):
            stats["steps"] += 1
            stats["ingest_selected"] += step["op"] == "ingest" and bool(x["r"].get("selected"))
            stats["output_valid"] += step["op"] == "output" and bool(x["r"].get("valid"))
            stats["output_nonzero_vx"] += step["op"] == "output" and bool(x["r"].get("vx"))
            stats["calibrate_ok"] += step["op"] == "calibrate" and "value" in x["r"]
            stats["cycle_moving"] += s["kind"] == "cycle" and x["snap"]["phase"] == "moving"
    print(dict(stats))

    mutations = {
        "clamp ×(1+1e-7)": ("_clamp", lambda orig: lambda v, a, b: orig(v * (1 + 1e-7), a, b)),
        "never clipped": ("boxes_from", lambda orig: lambda p: [tr.Box(**{**b.__dict__, "clipped": False}) for b in orig(p)]),
        "sign(0)=1": ("_sign", lambda orig: lambda v: 1 if v >= 0 else -1),
    }
    for name, (attr, make) in mutations.items():
        original = getattr(tr, attr)
        setattr(tr, attr, make(original))
        try:
            caught = first_divergence(js, scenarios)
        finally:
            setattr(tr, attr, original)
        print(f"mutation {name}: {'CAUGHT ' + caught[:80] if caught else 'MISSED'}")
    print("unmutated:", first_divergence(js, scenarios) or "IDENTICAL")


if __name__ == "__main__":
    main()
