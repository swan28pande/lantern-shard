"""Static structure of Python source files: which scope each line belongs to."""

from __future__ import annotations

import ast


class Scope:
    __slots__ = ("kind", "name", "start", "end", "body_start", "parent", "assigned")

    def __init__(self, kind, name, start, end, body_start, parent):
        self.kind = kind  # "module" | "class" | "func"
        self.name = name  # qualified name
        self.start = start  # first line incl. decorators
        self.end = end
        self.body_start = body_start  # first line of the body
        self.parent = parent
        self.assigned: list[str] = []

    def __repr__(self):
        return f"<{self.kind} {self.name} {self.start}-{self.end}>"


def build_scopes(src: str) -> list[Scope]:
    """Returns scopes in pre-order; scopes[0] is the module."""
    tree = ast.parse(src)
    nlines = src.count("\n") + 1
    mod = Scope("module", "", 1, nlines, 1, None)
    out = [mod]

    def visit(node, parent: Scope, prefix: str):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                start = min([child.lineno] + [d.lineno for d in child.decorator_list])
                body_start = child.body[0].lineno if child.body else child.lineno
                # a docstring is part of the body; that is fine
                kind = "class" if isinstance(child, ast.ClassDef) else "func"
                name = f"{prefix}{child.name}"
                s = Scope(kind, name, start, child.end_lineno, body_start, parent)
                out.append(s)
                visit(child, s, name + ".")
            else:
                visit(child, parent, prefix)

    visit(tree, mod, "")
    return out


def innermost(scopes: list[Scope], line: int) -> Scope:
    best = scopes[0]
    for s in scopes[1:]:
        if s.start <= line <= s.end and (s.end - s.start) <= (best.end - best.start):
            best = s
    return best


def enclosing_class(s: Scope) -> Scope | None:
    while s is not None and s.kind != "class":
        s = s.parent
    return s


def assigned_names_at(src: str, lines: set[int]) -> set[str]:
    """Names assigned/defined by statements covering any of the given lines (class/module level)."""
    tree = ast.parse(src)
    names: set[str] = set()

    def stmt_hits(st):
        return any(st.lineno <= ln <= (st.end_lineno or st.lineno) for ln in lines)

    def walk(body):
        for st in body:
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if stmt_hits(st):
                    names.add(st.name)
                continue
            if isinstance(st, ast.ClassDef):
                if any(st.lineno <= ln < (st.body[0].lineno if st.body else st.lineno) for ln in lines):
                    names.add(st.name)
                walk(st.body)
                continue
            if not stmt_hits(st):
                continue
            if isinstance(st, ast.Assign):
                for tgt in st.targets:
                    for n in ast.walk(tgt):
                        if isinstance(n, ast.Name):
                            names.add(n.id)
            elif isinstance(st, (ast.AnnAssign, ast.AugAssign)):
                for n in ast.walk(st.target):
                    if isinstance(n, ast.Name):
                        names.add(n.id)
            elif isinstance(st, (ast.Import, ast.ImportFrom)):
                for a in st.names:
                    names.add((a.asname or a.name).split(".")[0])
            elif isinstance(st, (ast.If, ast.Try, ast.With, ast.For, ast.While)):
                walk(getattr(st, "body", []))
                walk(getattr(st, "orelse", []))
                for h in getattr(st, "handlers", []):
                    walk(h.body)
                walk(getattr(st, "finalbody", []))

    walk(tree.body)
    return names
