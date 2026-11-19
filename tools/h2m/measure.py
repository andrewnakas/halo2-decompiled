"""measure: instruction-level scoring of one object function against retail.

distance(ours, masked, theirs, va)
    Aligned instruction distance (0 = match up to addresses). Our relocation
    fields are first overwritten with 0x7f bytes, so a call (e8 00000000 in the
    .obj) or a relocated immediate/displacement becomes an out-of-function
    address and normalises to ADDR, the same as retail's real address. Our side
    is the whole function (trailing int3/nop padding dropped), not cut to
    retail's length.

pick(funcs, theirs, va, sym=None)
    The object function that stands for the @retail VA: `sym` substring if
    given; else the single name containing the VA in hex; else the byte match;
    else the closest. Scoring only this function stops an unchanged sibling in
    the same file from hiding (or faking) progress.

score(funcs, theirs, va, name)
    0 on a byte match of `name`, its distance otherwise, 10**6 if absent.
"""
import os, re as _re
import capstone
from . import objcmp

_md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
_HEX = _re.compile(r'0x[0-9a-f]+')


def norm(data, base, size):
    """Normalised instruction texts: only a direct branch/call immediate can be a label:
    `call dword ptr [eax + 0x18]` is a vtable slot, not an offset. fs:[...] and
    absolute [0]/[0x0] forms normalize to one spelling."""
    out = []
    for i in _md.disasm(bytes(data[:size + 64]), base):
        if i.address - base >= size and i.mnemonic in ('int3', 'nop'):
            break
        ops = i.op_str
        branch = i.mnemonic.startswith(('j', 'call', 'loop')) and '[' not in ops and ops[:1].isdigit()
        if branch:
            v = int(ops, 0)
            ops = f'L{v - base:x}' if base <= v < base + size else 'ADDR'
        else:
            ops = _HEX.sub(lambda m: 'ADDR' if int(m.group(0), 16) >= 0x10000 else m.group(0), ops)
            ops = ops.replace('[0x0]', '[ADDR]').replace('ptr [0]', 'ptr [ADDR]')
            ops = _re.sub(r'fs:\[[^\]]*\]', 'fs:[ADDR]', ops)
        out.append(i.mnemonic + ' ' + ops)
        if i.address - base >= size:
            break
    return out


def _strip(data):
    end = len(data)
    while end and data[end - 1] in (0xCC, 0x90):
        end -= 1
    return data[:end]


def distance(ours, masked, theirs, va):
    import difflib
    b = bytearray(ours)
    for o in masked:
        if o < len(b):
            b[o] = 0x7F
    b = _strip(bytes(b))
    x = norm(theirs, va, len(theirs))
    y = norm(b, 0, len(b))
    x = [s.replace('[0x0]', '[ADDR]') for s in x]
    y = [s.replace('[0x0]', '[ADDR]') for s in y]
    sm = difflib.SequenceMatcher(None, x, y, autojunk=False)
    return sum(max(i2 - i1, j2 - j1) for op, i1, i2, j1, j2 in sm.get_opcodes() if op != 'equal')


def upstream(ours, masked, theirs, va):
    """tools/check.py's difference score on the object: instructions compared
    in lockstep (no alignment), relocation fields masked, plus the length
    difference in bytes. This is the number reviewers see as `score N`; one
    extra instruction early shifts every later one, unlike distance()."""
    import check  # the tree's tools/, on sys.path via project
    b = _strip(bytes(ours))
    count, lines, complete = check.differing_instructions(b, 0, theirs, va, set(o for o in masked if o < len(b)))
    return count + abs(len(b) - len(theirs))


def evaluate(f, theirs, va):
    """MEASURE=up in the environment scores with upstream(); the default is
    the aligned distance()."""
    name, data, masked = f
    if objcmp.compare(data, masked, theirs) is None:
        return 0
    if os.environ.get('MEASURE') == 'up':
        return upstream(data, masked, theirs, va)
    return distance(data, masked, theirs, va)


def pick(funcs, theirs, va, sym=None):
    if not funcs:
        return None
    if sym:
        c = [f for f in funcs if sym in f[0]]
        if c:
            return c[0]
    tag = f'{va:x}'
    c = [f for f in funcs if tag in f[0].lower()]
    best = min(funcs, key=lambda f: evaluate(f, theirs, va))
    if len(c) == 1:
        # a lone name carrying the address is usually the function, but it can be a
        # helper named after it (unknown_318a90, unknown_312e00): then the function
        # itself is the one far closer to retail's bytes
        return c[0] if evaluate(c[0], theirs, va) <= 2 * evaluate(best, theirs, va) + 50 else best
    return min(c, key=lambda f: evaluate(f, theirs, va)) if c else best


def score(funcs, theirs, va, name):
    for f in funcs:
        if f[0] == name:
            return evaluate(f, theirs, va)
    return 10 ** 6


def diff(ours, masked, theirs, va, context=2):
    """Aligned diff of normalized instructions (retail left, ours right), with offsets."""
    import difflib
    b = bytearray(ours)
    for o in masked:
        if o < len(b):
            b[o] = 0x7F
    b = _strip(bytes(b))
    md = _md
    def rows(data, base):
        offs = [i.address - base for i in md.disasm(bytes(data), base)]
        return offs
    x = [s.replace('[0x0]', '[ADDR]') for s in norm(theirs, va, len(theirs))]
    y = [s.replace('[0x0]', '[ADDR]') for s in norm(b, 0, len(b))]
    xo, yo = rows(theirs, va), rows(b, 0)
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, x, y, autojunk=False).get_opcodes():
        if op == 'equal':
            if i2 - i1 > 2 * context:
                for k in range(context):
                    out.append(f'   {xo[i1+k]:04x} {x[i1+k]:44.44} | {y[j1+k]}')
                out.append('   ...')
                for k in range(i2 - i1 - context, i2 - i1):
                    out.append(f'   {xo[i1+k]:04x} {x[i1+k]:44.44} | {y[j1+k]}')
            else:
                for k in range(i2 - i1):
                    out.append(f'   {xo[i1+k]:04x} {x[i1+k]:44.44} | {y[j1+k]}')
            continue
        for k in range(max(i2 - i1, j2 - j1)):
            l = f'{xo[i1+k]:04x} {x[i1+k]}' if i1 + k < i2 else ''
            r = f'{yo[j1+k]:04x} {y[j1+k]}' if j1 + k < j2 else ''
            out.append(f'!! {l:49.49} | {r}')
    return out
