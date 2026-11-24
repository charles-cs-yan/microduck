"""Upstream core.js vs follow/tracker.py on thousands of random steps. Skipped without Node.

    PATH=~/mdev/node/bin:$PATH python3 -m unittest follow.tests.test_differential
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from follow.tests.diff.harness import compare, generate, run_python

ROOT = Path(__file__).resolve().parents[2]
CORE_JS = ROOT / "microduck" / "vision_follow" / "core.js"
RUNNER = Path(__file__).resolve().parent / "diff" / "run_core.cjs"
NODE = os.environ.get("FOLLOW_NODE") or shutil.which("node") or str(Path.home() / "mdev" / "node" / "bin" / "node")
HAVE_NODE = Path(NODE).is_file() and CORE_JS.is_file()


def run_js(scenarios):
    with tempfile.TemporaryDirectory() as tmp:
        src, dst = Path(tmp) / "in.json", Path(tmp) / "out.json"
        src.write_text(json.dumps(scenarios), encoding="utf-8")
        subprocess.run([NODE, str(RUNNER), str(CORE_JS), str(src), str(dst)], check=True, timeout=120)
        return json.loads(dst.read_text(encoding="utf-8"))


@unittest.skipUnless(HAVE_NODE, "needs Node and the upstream checkout")
class DifferentialTest(unittest.TestCase):
    def check(self, seed, n):
        scenarios = generate(seed, n)
        js, py = run_js(scenarios), run_python(scenarios)
        for i, (a, b) in enumerate(zip(js, py)):
            problem = compare(a, b)
            if problem:
                steps = scenarios[i]["steps"]
                index = int(problem.split("[", 2)[1].split("]")[0]) if problem.startswith(".steps[") else None
                context = steps[index] if index is not None and index < len(steps) else None
                self.fail(f"seed {seed} scenario {i} ({scenarios[i]['kind']}): {problem}\n"
                          f"options={scenarios[i]['options']}\nstep={json.dumps(context, ensure_ascii=False)[:600]}")
        return sum(len(s["steps"]) for s in scenarios)

    def test_many_random_scenarios(self):
        total = sum(self.check(seed, 400) for seed in (1, 2, 3))
        self.assertGreater(total, 20000)


if __name__ == "__main__":
    unittest.main()
