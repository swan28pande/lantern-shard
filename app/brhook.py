"""Overlay hook: makes a patched copy of selected sqlglot/test files visible.

Loaded in every interpreter through brhook.pth (site-packages).  It does nothing
unless BR_OVERLAY names a manifest, so spawned subprocesses of a test see the
same patched files as the test itself.
"""

import builtins
import importlib.abc
import importlib.util
import io
import json
import os
import sys

REPO = os.environ.get("SQLGLOT_DIR", "/opt/sqlglot")
_installed = None


class _Finder(importlib.abc.MetaPathFinder):
    def __init__(self, mods, deleted):
        self.mods = mods
        self.deleted = deleted

    def find_spec(self, fullname, path=None, target=None):
        if fullname in self.deleted:
            raise ModuleNotFoundError("No module named %r" % fullname, name=fullname)
        hit = self.mods.get(fullname)
        if hit is None:
            return None
        file, is_pkg = hit
        if is_pkg:
            return importlib.util.spec_from_file_location(
                fullname, file, submodule_search_locations=[os.path.dirname(file), os.path.join(REPO, *fullname.split("."))]
            )
        return importlib.util.spec_from_file_location(fullname, file)


def module_name(rel):
    if not rel.endswith(".py"):
        return None
    parts = rel[:-3].split("/")
    if parts[0] not in ("sqlglot", "tests"):
        return None
    if parts[-1] == "__init__":
        return ".".join(parts[:-1]), True
    return ".".join(parts), False


def install(manifest):
    """manifest: {"files": {rel: overlay_abs_path}, "deleted": [rel, ...]}"""
    global _installed
    if _installed is not None:
        return
    _installed = manifest
    mods, deleted_mods, redirect, deleted_files = {}, set(), {}, set()
    for rel, dst in manifest.get("files", {}).items():
        redirect[os.path.join(REPO, rel)] = dst
        mn = module_name(rel)
        if mn:
            mods[mn[0]] = (dst, mn[1])
    for rel in manifest.get("deleted", []):
        deleted_files.add(os.path.join(REPO, rel))
        mn = module_name(rel)
        if mn:
            deleted_mods.add(mn[0])

    touched = set(mods) | deleted_mods
    if any(m in sys.modules for m in touched):
        for name in list(sys.modules):
            if name in ("sqlglot", "tests") or name.startswith(("sqlglot.", "tests.")):
                del sys.modules[name]
        importlib.invalidate_caches()
    if mods or deleted_mods:
        sys.meta_path.insert(0, _Finder(mods, deleted_mods))

    if not (redirect or deleted_files):
        return
    real_open = builtins.open
    real_io_open = io.open

    def _map(file):
        if isinstance(file, (str, bytes, os.PathLike)):
            p = os.fsdecode(file)
            ap = os.path.normpath(p if os.path.isabs(p) else os.path.join(os.getcwd(), p))
            if ap in redirect:
                return redirect[ap]
            if ap in deleted_files:
                raise FileNotFoundError(2, "No such file or directory", p)
        return file

    def patched_open(file, *a, **k):
        return real_open(_map(file), *a, **k)

    def patched_io_open(file, *a, **k):
        return real_io_open(_map(file), *a, **k)

    builtins.open = patched_open
    io.open = patched_io_open
    try:
        import pathlib

        def path_open(self, mode="r", buffering=-1, encoding=None, errors=None, newline=None):
            if "b" not in mode and encoding is None:
                encoding = io.text_encoding(encoding)
            return real_io_open(_map(str(self)), mode, buffering, encoding, errors, newline)

        def read_text(self, encoding=None, errors=None, newline=None):
            encoding = io.text_encoding(encoding)
            with real_io_open(_map(str(self)), "r", encoding=encoding, errors=errors, newline=newline) as f:
                return f.read()

        def read_bytes(self):
            with real_io_open(_map(str(self)), "rb") as f:
                return f.read()

        pathlib.Path.open = path_open
        pathlib.Path.read_text = read_text
        pathlib.Path.read_bytes = read_bytes
    except Exception:
        pass


def install_from_env():
    path = os.environ.get("BR_OVERLAY")
    if not path:
        return
    try:
        with io.open(path, encoding="utf-8") as f:
            install(json.load(f))
    except Exception as e:  # never break an interpreter over this
        sys.stderr.write("brhook: %r\n" % (e,))


install_from_env()
