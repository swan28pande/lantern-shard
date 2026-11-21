"""Offline policy evaluation on labeled commits.

labels.json (from BR_MODE=label): {cid: {failing, cand, direct, ran}}
Simulates: run cheap candidates (CPU ~ recorded secs * factor), hand expensive per policy.
usage: python offline_eval.py tests.json labels.json [answers.json]
"""
import json
import sys

sys.path.insert(0, __file__.rsplit("/", 2)[0] + "/repo/app")
from pack import pack  # noqa: E402

tests = json.load(open(sys.argv[1]))["tests"]
labels = json.load(open(sys.argv[2]))
answers = json.load(open(sys.argv[3]))["commits"] if len(sys.argv) > 3 else None
ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
sec = {t["number"]: t["seconds"] for t in tests}


def policy(lab, run_max=1.0, factor=1.0, overhead=0.3, include_direct=True):
    cands = set(lab["cand"])
    direct = set(lab["direct"])
    failing = set(lab["failing"])
    exp_ = {n for n in cands if sec[n] >= run_max}
    cheap = cands - exp_
    unrun = {n for n in exp_ if (n in direct if include_direct else True)}
    cpu = sum(sec[n] for n in cheap) * factor + (overhead if cheap else 0)
    sel = (failing & cheap) | unrun
    cost = pack(sorted(sel), ms)[0] / 1000
    miss = bool(failing - sel)
    return cost, cpu, miss, failing - sel


for rm in (0.5, 1.0, 2.0, 5.0):
    tot = cpu = 0
    misses = []
    for cid, lab in sorted(labels.items()):
        if answers:
            lab = dict(lab, failing=answers[cid]["failing"])
        c, u, m, missed = policy(lab, run_max=rm)
        tot += c
        cpu += u
        if m:
            misses.append((cid, sorted(missed)[:6]))
    print(f"run_max={rm}: sim={tot:.1f} cpu~{cpu:.1f} misses={len(misses)} total~{tot + cpu + 300 * len(misses):.1f}")
    for x in misses[:12]:
        print("   ", x)
