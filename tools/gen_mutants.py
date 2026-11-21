"""Generate synthetic one-line mutation patches against the pinned checkout (local calibration only).

usage: python gen_mutants.py SQLGLOT_DIR OUT_DIR N SEED
"""

import difflib
import os
import random
import re
import sys

repo, out, n, seed = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
rng = random.Random(seed)
os.makedirs(os.path.join(out, "commits"), exist_ok=True)

MUTS = [
    (r"\band\b", "or"),
    (r"\bor\b", "and"),
    (r" == ", " != "),
    (r" != ", " == "),
    (r" < ", " <= "),
    (r" <= ", " < "),
    (r" > ", " >= "),
    (r" >= ", " > "),
    (r"\bis not\b", "is"),
    (r"\bis None\b", "== None"),
    (r"\bif not ", "if "),
    (r"\bTrue\b", "False"),
    (r"\bFalse\b", "True"),
    (r"\b0\b", "1"),
    (r"\b1\b", "2"),
    (r"\b2\b", "3"),
    (r" \+ 1\b", " - 1"),
    (r" - 1\b", " + 1"),
    (r"\breturn True\b", "return False"),
    (r"\bin\b", "not in"),
]

py_files = []
for base in ("sqlglot",):
    for dp, dn, fn in os.walk(os.path.join(repo, base)):
        for f in fn:
            if f.endswith(".py"):
                py_files.append(os.path.relpath(os.path.join(dp, f), repo))
fixtures = []
for dp, dn, fn in os.walk(os.path.join(repo, "tests", "fixtures")):
    for f in fn:
        if f.endswith(".sql"):
            fixtures.append(os.path.relpath(os.path.join(dp, f), repo))
py_files.sort()
fixtures.sort()

# weight files by size (bigger files get mutated more often)
weights = [os.path.getsize(os.path.join(repo, f)) ** 0.5 for f in py_files]


def diff(rel, old, new):
    d = difflib.unified_diff(old.splitlines(True), new.splitlines(True), f"a/{rel}", f"b/{rel}", n=3)
    return f"diff --git a/{rel} b/{rel}\n" + "".join(d)


def mutate_code():
    for _ in range(100):
        rel = rng.choices(py_files, weights)[0]
        src = open(os.path.join(repo, rel), encoding="utf-8").read()
        lines = src.split("\n")
        idxs = list(range(len(lines)))
        rng.shuffle(idxs)
        for i in idxs[:200]:
            ln = lines[i]
            st = ln.strip()
            if not st or st.startswith(("#", '"', "'", "import", "from", "@")) or '"""' in ln:
                continue
            opts = [(p, r) for p, r in MUTS if re.search(p, ln.split("#")[0])]
            if not opts:
                continue
            p, r = rng.choice(opts)
            ms = list(re.finditer(p, ln.split("#")[0]))
            m = rng.choice(ms)
            new_ln = ln[: m.start()] + r + ln[m.end():]
            new_lines = list(lines)
            new_lines[i] = new_ln
            return "code", diff(rel, src, "\n".join(new_lines))
    raise RuntimeError


def mutate_comment():
    rel = rng.choices(py_files, weights)[0]
    src = open(os.path.join(repo, rel), encoding="utf-8").read()
    lines = src.split("\n")
    cands = [i for i, l in enumerate(lines) if l.strip() and not l.strip().startswith(("#", '"', "'", ")", "]", "}"))]
    i = rng.choice(cands)
    indent = re.match(r"\s*", lines[i]).group(0)
    text = rng.choice(["# Cheap check first", "# TODO: revisit", "# Kept for backwards compatibility", "# noqa"])
    new_lines = lines[:i] + [indent + text] + lines[i:]
    return "comment", diff(rel, src, "\n".join(new_lines))


def mutate_fixture():
    rel = rng.choice(fixtures)
    src = open(os.path.join(repo, rel), encoding="utf-8").read()
    lines = src.split("\n")
    cands = [i for i, l in enumerate(lines) if l.strip() and not l.startswith(("#", "--"))]
    i = rng.choice(cands)
    toks = lines[i].split(" ")
    k = rng.randrange(len(toks))
    kind = rng.random()
    if kind < 0.4:
        toks.insert(k, rng.choice(["x", "1", "42"]))
    elif kind < 0.7 and len(toks) > 1:
        del toks[k]
    else:
        toks[k] = toks[k].replace("<", "").replace("(", "") if rng.random() < 0.5 else toks[k] + "_"
    new_lines = list(lines)
    new_lines[i] = " ".join(toks)
    if new_lines[i] == lines[i]:
        new_lines[i] += " x"
    return "fixture", diff(rel, src, "\n".join(new_lines))


for k in range(n):
    r = rng.random()
    if r < 0.6:
        kind, d = mutate_code()
    elif r < 0.75:
        kind, d = mutate_comment()
    elif r < 0.9:
        kind, d = mutate_fixture()
    else:
        kind = "multi"
        parts = [mutate_code()[1], mutate_comment()[1], mutate_fixture()[1]]
        d = "".join(rng.sample(parts, 2))
    cid = f"m{seed}_{k:03d}"
    with open(os.path.join(out, "commits", cid + ".patch"), "w") as f:
        f.write(d)
print("ok")
