"""Every variant gets a vector, not one number.

lock   upstream check.py score (lockstep + length difference): what reviewers see
dist   aligned instruction distance (difflib), registers kept
sem    multiset distance with registers/stack/labels masked (order ignored)
frame  ours - retail prologue `sub esp` bytes
first  retail offset of the first aligned difference (-1 if none)
match  byte match with relocation fields masked"""
import difflib
from . import asm, objcmp, project  # noqa: F401  (project puts the tree's tools/ on sys.path)
import check  # noqa: E402


def aligned(x, y):
    """Opcodes aligning retail instructions x with ours y (texts at level reloc)."""
    sm = difflib.SequenceMatcher(None, [i.text for i in x], [i.text for i in y], autojunk=False)
    return sm.get_opcodes()


def vector(data, masked, theirs, va):
    if objcmp.compare(data, masked, theirs) is None:
        return dict(match=1, lock=0, dist=0, sem=0, frame=0, first=-1)
    b = asm.strip_pad(data)
    count, _, _ = check.differing_instructions(b, 0, theirs, va, {o for o in masked if o < len(b)})
    x, y = asm.decode(theirs, va), asm.ours(data, masked)
    ops = aligned(x, y)
    dist = sum(max(i2 - i1, j2 - j1) for op, i1, i2, j1, j2 in ops if op != 'equal')
    first = next((x[i1].off if i1 < len(x) else len(theirs) for op, i1, i2, j1, j2 in ops if op != 'equal'), -1)
    a, c = asm.bag(x), asm.bag(y)
    sem = sum((a - c).values()) + sum((c - a).values())
    return dict(match=0, lock=count + abs(len(b) - len(theirs)), dist=dist, sem=sem,
                frame=asm.frame(y) - asm.frame(x), first=first)


def key(v):
    """Search order: a match first, then aligned distance, then semantic, then lockstep."""
    return (0 if v.get('match') else 1, v.get('dist', 10 ** 6), v.get('sem', 10 ** 6), v.get('lock', 10 ** 6))
