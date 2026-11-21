"""Run every test unpatched through the runner and record per-test CPU (local calibration)."""
import json, os, sys
sys.path.insert(0, "/app")
import runner
tests = json.load(open(sys.argv[1]))["tests"]
only = sys.argv[3] if len(sys.argv) > 3 else None
sel = [(t["number"], t["id"]) for t in tests if not only or only in t["id"]]
runner.preload(full=True)
job = runner.Job("base", {}, sel, {n: 600.0 for n, _ in sel})
runner.run_jobs([job], parallel=1)
json.dump({"cpu": job.cpu, "failed": sorted(job.failed)}, open(sys.argv[2], "w"))
print("failed", sorted(job.failed), file=sys.stderr)
