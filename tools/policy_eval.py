"""Offline policy evaluation: plan (cand/direct) + truth (failing) + per-test CPU -> score.

usage: python policy_eval.py tests.json cpu_all.json plan.json truth.json [truth_is_answers]
"""
import itertools
import json
import sys

sys.path.insert(0, __file__.rsplit("/", 2)[0] + "/repo/app")
from pack import pack  # noqa: E402

tests = {t["number"]: t for t in json.load(open(sys.argv[1]))["tests"]}
cpu = {int(k): v for k, v in json.load(open(sys.argv[2]))["cpu"].items()}
plan = json.load(open(sys.argv[3]))
truth = json.load(open(sys.argv[4]))
if "commits" in truth:
    truth = {k: v["failing"] for k, v in truth["commits"].items()}
else:
    truth = {k: v["failing"] for k, v in truth.items()}
sec = {n: t["seconds"] for n, t in tests.items()}
ms = {n: round(s * 1000) for n, s in sec.items()}


def simulate(R=1.5, BIG=2.0, OV=0.6, CF=1.0, lump=True, verbose=False, run_pooled_if_big=False, pooled_direct=False):
    total = cpu_tot = 0.0
    misses = []
    for cid, p in plan.items():
        cand = set(p["cand"])
        fail = set(truth.get(cid, []))
        c = {n: cpu.get(n, sec[n]) * CF for n in cand}
        big = {n for n in cand if sec[n] >= BIG}
        pooled = {n for n in cand - big if c[n] > sec[n] * R}
        cheap = cand - big - pooled
        if p.get("syntax"):
            sel = set(tests)
            cost = pack(sorted(sel), ms)[0] / 1000
            total += cost
            if verbose:
                print(f"  {cid} SYNTAX cost={cost:.1f}")
            continue
        if run_pooled_if_big and big:
            cheap |= pooled
            pooled = set()
        run_cost = sum(c[n] for n in cheap) + (OV if cheap else 0)
        if lump and cheap and not big:
            delta = (pack(sorted(pooled | cheap), ms)[0] - pack(sorted(pooled), ms)[0]) / 1000
            if delta < run_cost:
                pooled |= cheap
                cheap = set()
                run_cost = 0
        sel = set(pooled)
        ran_fail = cheap & fail
        sel |= ran_fail
        if big and (ran_fail or not cheap or (pooled_direct and (pooled & set(p["direct"])))):
            sel |= big
        cost = pack(sorted(sel), ms)[0] / 1000
        total += cost
        cpu_tot += run_cost
        if fail - sel:
            misses.append((cid, sorted(fail - sel)[:5]))
        if verbose:
            print(f"  {cid} cost={cost:.1f} cpu={run_cost:.1f} fail={len(fail)} miss={bool(fail - sel)}")
    return total, cpu_tot, misses


if __name__ == "__main__":
    for pdx in (False, True):
        for R in (1.2, 1.5):
            tot, c, m = simulate(R, 2.0, pooled_direct=pdx)
            print(f"pooled_direct={pdx} R={R}: sim={tot:.1f} cpu={c:.1f} misses={len(m)} total={tot + c + 300 * len(m):.1f} {[x[0] for x in m]}")
