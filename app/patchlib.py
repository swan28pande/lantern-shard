"""Parse unified diffs and apply them to files of the pinned checkout."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass
class Hunk:
    old_start: int
    old_len: int
    new_start: int
    new_len: int
    lines: list[str] = field(default_factory=list)  # each starts with ' ', '-', '+', or '\\'


@dataclass
class FilePatch:
    old_path: str | None  # None when the file is new
    new_path: str | None  # None when the file is deleted
    hunks: list[Hunk] = field(default_factory=list)
    binary: bool = False

    @property
    def path(self) -> str:
        return self.new_path or self.old_path  # type: ignore[return-value]

    def removed_old_lines(self) -> list[int]:
        """Old-file line numbers of removed lines."""
        out = []
        for h in self.hunks:
            o = h.old_start
            for ln in h.lines:
                c = ln[:1]
                if c == " ":
                    o += 1
                elif c == "-":
                    out.append(o)
                    o += 1
        return out

    def touched_old_lines(self) -> list[int]:
        """Old-file lines that were removed; for pure insertions, the old lines around the insertion."""
        out = set()
        for h in self.hunks:
            o = h.old_start if h.old_len > 0 else h.old_start + 1
            block_removed: list[int] = []
            block_added = False

            def flush():
                if block_removed:
                    out.update(block_removed)
                elif block_added:
                    out.add(max(o - 1, 1))
                    out.add(o)

            for ln in h.lines:
                c = ln[:1]
                if c == " ":
                    flush()
                    block_removed, block_added = [], False
                    o += 1
                elif c == "-":
                    block_removed.append(o)
                    o += 1
                elif c == "+":
                    block_added = True
            flush()
        return sorted(out)

    def added_lines(self) -> list[str]:
        return [ln[1:] for h in self.hunks for ln in h.lines if ln[:1] == "+"]

    def removed_lines(self) -> list[str]:
        return [ln[1:] for h in self.hunks for ln in h.lines if ln[:1] == "-"]


def _strip_prefix(p: str) -> str | None:
    p = p.split("\t")[0].strip()
    if p == "/dev/null":
        return None
    if p.startswith(("a/", "b/")):
        return p[2:]
    return p


def parse_patch(text: str) -> list[FilePatch]:
    files: list[FilePatch] = []
    cur: FilePatch | None = None
    hunk: Hunk | None = None
    lines = text.split("\n")
    i = 0
    rename_from = rename_to = None
    while i < len(lines):
        line = lines[i]
        if line.startswith("diff --git "):
            m = re.match(r"diff --git a/(.*) b/(.*)$", line)
            cur = FilePatch(m.group(1) if m else None, m.group(2) if m else None)
            files.append(cur)
            hunk = None
            rename_from = rename_to = None
            i += 1
            continue
        if line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            old = _strip_prefix(line[4:])
            new = _strip_prefix(lines[i + 1][4:])
            if cur is None or cur.hunks:
                cur = FilePatch(old, new)
                files.append(cur)
            else:
                cur.old_path, cur.new_path = old, new
            hunk = None
            i += 2
            continue
        if cur is not None and hunk is None and not line.startswith("@@"):
            if line.startswith("new file mode"):
                cur.old_path = None
            elif line.startswith("deleted file mode"):
                cur.new_path = None
            elif line.startswith("rename from "):
                cur.old_path = line[len("rename from "):]
            elif line.startswith("rename to "):
                cur.new_path = line[len("rename to "):]
            elif line.startswith("Binary files") or line.startswith("GIT binary patch"):
                cur.binary = True
            i += 1
            continue
        m = HUNK_RE.match(line)
        if m and cur is not None:
            hunk = Hunk(
                int(m.group(1)),
                int(m.group(2)) if m.group(2) is not None else 1,
                int(m.group(3)),
                int(m.group(4)) if m.group(4) is not None else 1,
            )
            cur.hunks.append(hunk)
            # consume exactly the hunk body
            o_left, n_left = hunk.old_len, hunk.new_len
            i += 1
            while i < len(lines) and (o_left > 0 or n_left > 0):
                ln = lines[i]
                c = ln[:1]
                if c == " " or ln == "":
                    hunk.lines.append(" " + ln[1:] if ln else " ")
                    o_left -= 1
                    n_left -= 1
                elif c == "-":
                    hunk.lines.append(ln)
                    o_left -= 1
                elif c == "+":
                    hunk.lines.append(ln)
                    n_left -= 1
                elif c == "\\":
                    hunk.lines.append(ln)
                else:
                    break
                i += 1
            while i < len(lines) and lines[i].startswith("\\"):
                hunk.lines.append(lines[i])
                i += 1
            hunk = None
            continue
        i += 1
    return files


def apply_file_patch(original: str | None, fp: FilePatch) -> str | None:
    """Return the new content, or None if the file is deleted."""
    if fp.new_path is None:
        return None
    src = (original or "").split("\n")
    # keep track of whether the original ended with newline
    if original is not None and original.endswith("\n"):
        src = src[:-1]
        had_nl = True
    else:
        had_nl = original is None
    out: list[str] = []
    pos = 0  # index into src (0-based)
    new_nl = had_nl
    for h in fp.hunks:
        start = h.old_start - 1 if h.old_len > 0 else h.old_start
        old_block = [ln[1:] for ln in h.lines if ln[:1] in " -"]
        start = _find(src, old_block, start, pos)
        out.extend(src[pos:start])
        pos = start
        last = None
        for ln in h.lines:
            c = ln[:1]
            if c == " ":
                out.append(src[pos])
                pos += 1
                last = c
            elif c == "-":
                pos += 1
                last = c
            elif c == "+":
                out.append(ln[1:])
                last = c
            elif c == "\\":
                # "\ No newline at end of file" refers to the previous line
                if last in ("+", " "):
                    new_nl = False
                if last == "-":
                    pass
    out.extend(src[pos:])
    text = "\n".join(out)
    if new_nl and (out or original):
        text += "\n"
    return text


def _find(src: list[str], block: list[str], guess: int, lo: int) -> int:
    if not block:
        return max(guess, lo)
    n = len(block)
    if src[guess : guess + n] == block:
        return guess
    for d in range(1, len(src) + 1):
        for cand in (guess - d, guess + d):
            if lo <= cand <= len(src) - n and src[cand : cand + n] == block:
                return cand
        if guess - d < lo and guess + d > len(src):
            break
    raise ValueError("hunk does not apply")
