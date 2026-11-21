"""Blast radius selector.

For each patch: work out which tests could see the change (maps built from the
pinned commit + static analysis), actually run the cheap ones against the
patched code, and print only the tests that fail, plus expensive at-risk tests
that are cheaper to hand to CI than to run here.
"""

from __future__ import annotations

import json
import os
import pickle
import sys
import time

APP = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP)

from patchlib import apply_file_patch, parse_patch  # noqa: E402
from pack import pack  # noqa: E402
import runner  # noqa: E402
from runner import Job, log  # noqa: E402

DATA = os.environ.get("BR_DATA", "/data")
REPO = runner.REPO
MAPS = os.environ.get("BR_MAPS", "/app/maps.pkl")

# Tests at least this long (recorded seconds) are never run here.
RUN_MAX = float(os.environ.get("BR_RUN_MAX", "5.0"))
# Cheap-run CPU budget per commit (recorded seconds); above it we hand tests to CI unrun.
CPU_FACTOR = float(os.environ.get("BR_CPU_FACTOR", "1.0"))
OVERHEAD = float(os.environ.get("BR_OVERHEAD", "0.8"))
HAND_RATIO = float(os.environ.get("BR_HAND_RATIO", "1.5"))
SKIP_MIN = float(os.environ.get("BR_SKIP_MIN", "2.0"))
LABEL = os.environ.get("BR_MODE") == "label"
ABORT_MIN = float(os.environ.get("BR_ABORT_MIN", "0.4"))


def main() -> None:
    # results go through a private copy of stdout; anything else that writes to fd 1 lands on stderr
    out_fd = os.dup(1)
    os.dup2(2, 1)
    out = os.fdopen(out_fd, "w", encoding="utf-8")
    t_start = time.process_time()
    tests = json.load(open(os.path.join(DATA, "tests.json")))["tests"]
    num_of_id = {t["id"]: t["number"] for t in tests}
    ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    sec = {t["number"]: t["seconds"] for t in tests}
    id_of = {t["number"]: t["id"] for t in tests}
    file_of = {t["number"]: t["file"] for t in tests}
    all_tests = set(ms)
    try:
        cpu_by_id = json.load(open(os.path.join(APP, "maps", "cpu.json")))
    except Exception:
        cpu_by_id = {}
    cpu = {n: cpu_by_id.get(id_of[n], sec[n]) * CPU_FACTOR for n in ms}

    with open(MAPS, "rb") as f:
        raw = pickle.load(f)
    from analyze import Maps, analyze

    maps = Maps(raw, num_of_id)

    commits = sorted(
        p[:-6] for p in os.listdir(os.path.join(DATA, "commits")) if p.endswith(".patch")
    )
    results: dict[str, list[list[int]]] = {}
    jobs: list[tuple[str, Job, set[int]]] = []
    plans = {}

    for cid in commits:
        try:
            text = open(os.path.join(DATA, "commits", cid + ".patch"), encoding="utf-8", errors="replace").read()
            fps = parse_patch(text)
            changes: dict[str, str | None] = {}
            for fp in fps:
                orig = None
                if fp.old_path:
                    try:
                        with open(os.path.join(REPO, fp.old_path), encoding="utf-8") as f:
                            orig = f.read()
                    except FileNotFoundError:
                        orig = None
                changes[fp.path] = apply_file_patch(orig, fp)
                if fp.old_path and fp.new_path and fp.old_path != fp.new_path:
                    changes[fp.old_path] = None
            an = analyze(fps, changes, maps, all_tests, file_of)
        except Exception as e:  # never crash: fall back to everything
            log(f"[{cid}] analysis failed: {type(e).__name__}: {e}")
            results[cid] = pack(sorted(all_tests), ms)[1]
            continue

        cands = set(all_tests) if an.everything else set(an.candidates)
        if LABEL:
            # ground-truth mode (local only): run a broad superset, expensive tests included
            broad = set(cands) | set(an.broad)
            if os.environ.get("BR_LABEL_FILE"):
                for fp in fps:
                    for p in {fp.old_path, fp.new_path} - {None}:
                        broad |= set(maps.tests(maps.file_tests.get(p)))
                        broad |= set(maps.tests(maps.opens.get(p)))
            skip = {int(x) for x in os.environ.get("BR_LABEL_SKIP", "").split(",") if x}
            broad -= skip
            order = sorted(broad, key=lambda n: (file_of[n], n))
            limits = {n: 60.0 + 20 * sec[n] for n in order}
            job = Job(cid, changes, [(n, id_of[n]) for n in order], limits)
            job.abort_on_fail = frozenset(n for n in order if sec[n] >= ABORT_MIN)
            jobs.append((cid, job, set()))
            plans[cid] = (sorted(an.candidates), sorted(an.direct), sorted(an.broad))
            continue
        # per test: running here costs its CPU; handing it to CI costs its recorded seconds
        hand = {n for n in cands if cpu[n] > sec[n] * HAND_RATIO or sec[n] >= RUN_MAX}
        if not (an.everything or an.syntax_error):
            # long tests that only reach the changed code indirectly are rarely broken by it
            hand = {n for n in hand if n in an.direct or sec[n] < SKIP_MIN}
        cheap = cands - hand - {n for n in cands if cpu[n] > sec[n] * HAND_RATIO or sec[n] >= RUN_MAX}
        unrun = set(hand)
        run_cost = sum(cpu[n] for n in cheap) + (OVERHEAD if cheap else 0)
        base = pack(sorted(unrun), ms)[0]
        all_unrun = pack(sorted(unrun | cheap), ms)[0]
        if cheap and (all_unrun - base) / 1000 < run_cost:
            unrun |= cheap
            cheap = set()
        log(
            f"[{cid}] {'; '.join(an.notes)[:300]} | cand={len(cands)} run={len(cheap)}"
            f" ({sum(sec[n] for n in cheap):.2f}s) unrun={len(unrun)} ({sum(sec[n] for n in unrun):.2f}s)"
        )
        plans[cid] = unrun
        if cheap:
            order = sorted(cheap, key=lambda n: (file_of[n], n))
            limits = {n: 6.0 + 4 * cpu[n] for n in order}
            job = Job(cid, changes, [(n, id_of[n]) for n in order], limits)
            job.abort_on_fail = frozenset(n for n in order if sec[n] >= ABORT_MIN)
            jobs.append((cid, job, unrun))
        else:
            results[cid] = pack(sorted(unrun), ms)[1]

    t_an = time.process_time() - t_start
    log(f"analysis cpu {t_an:.2f}s; {len(jobs)} commits need runs")
    if jobs:
        runner.preload(full=True)
        runner.run_jobs([j for _, j, _ in jobs], parallel=2)
    for cid, job, unrun in jobs:
        sel = set(job.failed) | unrun
        log(f"[{cid}] failed: {sorted(job.failed)}")
        results[cid] = pack(sorted(sel), ms)[1]

    if LABEL:
        lab = {cid: {"failing": sorted(job.failed), "ran": len(job.passed) + len(job.failed),
                     "cand": plans[cid][0], "direct": plans[cid][1], "broad": plans[cid][2], "cpu": job.cpu} for cid, job, _ in jobs}
        with open(os.environ["BR_LABEL_OUT"], "w") as f:
            json.dump(lab, f)
    for cid in commits:
        out.write(json.dumps({"commit": cid, "workers": results.get(cid, [])}) + "\n")
    out.flush()
    ru = os.times()
    log(f"cpu self {ru.user + ru.system:.2f}s children {ru.children_user + ru.children_system:.2f}s")


if __name__ == "__main__":
    main()
