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
BIG_MIN = float(os.environ.get("BR_BIG_MIN", "2.0"))
LABEL = os.environ.get("BR_MODE") == "label"
DEADLINE = float(os.environ.get("BR_DEADLINE", "140"))
T0 = time.monotonic()
PLAN = {} if os.environ.get("BR_MODE") == "plan" else None
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
    try:
        setup_cost = {k: tuple(v) for k, v in json.load(open(os.path.join(APP, "maps", "setup.json"))).items()}
    except Exception:
        setup_cost = {}
    class_of = {n: file_of[n][:-3].replace("/", ".") + "::" + id_of[n].split("::")[1] for n in ms}

    with open(MAPS, "rb") as f:
        raw = pickle.load(f)
    from analyze import Maps, analyze

    maps = Maps(raw, num_of_id)
    import analyze as _an

    _an.TEST_NAMES.update({n: id_of[n].split("::")[-1] for n in ms})

    commits = sorted(
        p[:-6] for p in os.listdir(os.path.join(DATA, "commits")) if p.endswith(".patch")
    )
    results: dict[str, list[list[int]]] = {}
    jobs: list[tuple[str, Job, set[int]]] = []
    plans = {}
    conditional: dict[str, set[int]] = {}

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
        if PLAN is not None:
            PLAN[cid] = {"cand": sorted(cands), "direct": sorted(an.direct), "broad": sorted(an.broad),
                         "syntax": an.syntax_error, "notes": an.notes}
            continue
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
        # big: long tests, never run here; only handed over when a run test fails (they never
        #      failed alone in calibration), or when nothing could be run to tell.
        # pooled: cost far more CPU here than their recorded seconds -> hand over directly.
        # cheap: run here; only real failures are printed.
        if an.syntax_error:
            # a module that no longer compiles breaks (nearly) every test file through the
            # dialect test helpers: hand everything over, nothing to verify
            log(f"[{cid}] syntax error: handing over all tests")
            results[cid] = pack(sorted(all_tests), ms)[1]
            continue
        big = {n for n in cands if sec[n] >= BIG_MIN}
        pooled = {n for n in cands - big if cpu[n] > sec[n] * HAND_RATIO}
        cheap = cands - big - pooled
        if an.everything:
            big_always = True
        else:
            big_always = False
        unrun = set(pooled)
        # per test class: running costs its setUpClass + the tests' CPU; handing costs their seconds
        groups: dict[str, set[int]] = {}
        for n in cheap:
            groups.setdefault(class_of[n], set()).add(n)
        for key, g in (groups.items() if not big else ()):
            # (when big tests wait on this run's verdict, keep every cheap test as a signal)
            imp, setup = setup_cost.get(key, (0.0, 0.0))
            if setup + 0.3 * imp + sum(cpu[n] for n in g) > sum(sec[n] for n in g) + 0.3:
                unrun |= g
                cheap -= g
        run_cost = sum(cpu[n] for n in cheap) + (OVERHEAD if cheap else 0)
        run_cost += sum(setup_cost.get(k, (0, 0))[1] for k in {class_of[n] for n in cheap})
        if cheap and not big and not an.syntax_error:
            base = pack(sorted(unrun), ms)[0]
            all_unrun = pack(sorted(unrun | cheap), ms)[0]
            if (all_unrun - base) / 1000 < run_cost:
                unrun |= cheap
                cheap = set()
        # a big test that reaches the changed lines with (almost) no cheap test beside it has no
        # witness to wait for: hand it over regardless
        forced = {b for b in big if b in an.direct and len(an.direct & cheap) < 3}
        # data files: a big test reading the changed file can fail on its own
        forced |= big & an.data_readers
        if forced:
            unrun |= forced
            big -= forced
        if big and cheap and not big_always:
            # running cheap tests only to decide about big ones: worth it unless handing over
            # everything (big included) is cheaper than the run itself
            run_then = run_cost + pack(sorted(unrun), ms)[0] / 1000
            hand_all = pack(sorted(unrun | cheap | big), ms)[0] / 1000
            if hand_all < run_then:
                unrun |= cheap | big
                cheap = set()
        if big and (not cheap or big_always):
            unrun |= big
            big = set()
        log(
            f"[{cid}] {'; '.join(an.notes)[:300]} | cand={len(cands)} run={len(cheap)}"
            f" ({sum(sec[n] for n in cheap):.2f}s) unrun={len(unrun)} ({sum(sec[n] for n in unrun):.2f}s)"
        )
        plans[cid] = unrun
        if cheap or an.syntax_error:
            order = sorted(cheap, key=lambda n: (file_of[n], n))
            limits = {n: 6.0 + 4 * cpu[n] for n in order}
            job = Job(cid, changes, [(n, id_of[n]) for n in order], limits)
            job.abort_on_fail = frozenset(n for n in order if sec[n] >= ABORT_MIN)
            if an.syntax_error:
                # import-time breakage: probe every test class's import + setUpClass
                probe = sorted(all_tests - cheap - unrun, key=lambda n: (file_of[n], n))
                job.probe_only = frozenset(probe)
                job.pending = sorted(job.pending + [(n, id_of[n]) for n in probe], key=lambda t: (file_of[t[0]], t[0]))
                for n in probe:
                    job.limits[n] = 30.0
            jobs.append((cid, job, unrun))
            conditional[cid] = big
        else:
            results[cid] = pack(sorted(unrun), ms)[1]

    if PLAN is not None:
        with open(os.environ["BR_LABEL_OUT"], "w") as f:
            json.dump(PLAN, f)
        return
    t_an = time.process_time() - t_start
    log(f"analysis cpu {t_an:.2f}s; {len(jobs)} commits need runs")
    if jobs:
        mods = {file_of[n][:-3].replace("/", ".") for _, j, _ in jobs for n, _ in j.pending}
        runner.preload(full=True, test_modules=mods)
        runner.run_jobs([j for _, j, _ in jobs], parallel=2, deadline=T0 + DEADLINE)
    for cid, job, unrun in jobs:
        # a test that passed but took far longer than usual may hit the labelers' timeout
        slow = {n for n, used in job.cpu.items() if used > 5 * cpu[n] + 1.0 and n not in job.failed}
        if slow:
            log(f"[{cid}] unusually slow (treated as failing): {sorted(slow)}")
            job.failed |= slow
        sel = set(job.failed) | unrun
        if job.aborted:
            # not verified in time: hand over everything that was going to be checked
            sel |= {n for n, _ in job.pending} | conditional.get(cid, set())
        if job.failed and conditional.get(cid):
            sel |= conditional[cid]
        log(f"[{cid}] failed: {sorted(job.failed)} child_cpu={job.child_cpu:.2f} tests_cpu={sum(job.cpu.values()):.2f} spawns={job.spawn_count}")
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
