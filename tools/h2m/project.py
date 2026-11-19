"""Roots, functions.csv rows, retail bytes, body splicing. One copy for every tool.

ROOT  the tree the tools work on: $H2M_ROOT, else the repository this file is in.
OUT   where every work file, cache and variant goes: $H2M_OUT, else ROOT/build/h2m
      (build/ is ignored by git).
DB    the ledger (SQLite): $H2M_DB, else OUT/h2m.db.
The retail executable and the toolchain are found the way the other tools find
them (xbe.retail_xbe_path(), xbe.xdk_dir(): RETAIL_XBE, XDK_DIR)."""
import contextlib, csv, functools, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))                       # tools/h2m/
ROOT = os.path.abspath(os.environ.get('H2M_ROOT') or os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))
import reduced, xbe  # noqa: E402

OUT = os.path.abspath(os.environ.get('H2M_OUT') or os.path.join(ROOT, 'build', 'h2m'))
DB = os.path.abspath(os.environ.get('H2M_DB') or os.path.join(OUT, 'h2m.db'))


@functools.lru_cache(None)
def rows():
    return {int(r['va'], 16): r for r in csv.DictReader(open(os.path.join(ROOT, 'config', 'functions.csv')))}


@functools.lru_cache(None)
def _xbe():
    return xbe.load(xbe.retail_xbe_path())


def retail(va):
    return _xbe().read(va, int(rows()[va]['size']))


def source_path(va):
    return os.path.join(ROOT, rows()[va]['source'])


def read(path):
    return open(path, encoding='utf-8', errors='replace').read()


def body(text, va):
    s, e = reduced.find_body(text, va)
    return text[s:e]


def splice(text, va, new_body):
    s, e = reduced.find_body(text, va)
    return text[:s] + new_body + text[e:]


def open_vas(owner='third:havok'):
    return sorted(va for va, r in rows().items()
                  if r['owner'].startswith(owner) and r['status'] != 'matched' and r['source'])


@contextlib.contextmanager
def file_lock(path):
    """Exclusive lock on path across processes (held until the block ends)."""
    with open(path, 'a+') as f:
        try:
            import fcntl
        except ImportError:                                # Windows
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            return
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def workdir(va):
    d = os.path.join(OUT, f'{va:x}')
    os.makedirs(d, exist_ok=True)
    return d
