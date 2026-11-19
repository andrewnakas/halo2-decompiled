"""Turn a retail/ours pair into a diagnosis: which kind of gap, and where.

Classes (a function can carry several):
  convention   retail reads eax/ebx/esi/edi/ebp at entry that ours does not (/GL unit)
  semantic     operations one side has and the other lacks (masked multiset)
  width        the semantic gap is movsx/movzx/operand-size only
  fold         retail keeps an fdiv/fmul/imul/shift pair ours folded (or reverse)
  stack        prologue frame differs
  regs         same operations, different registers (bijection reported)
  schedule     same instructions with registers, different order
Regions are the aligned non-equal blocks, each with its own class."""
import collections, re
from . import asm, metrics

REGS = ('eax', 'ebx', 'ecx', 'edx', 'esi', 'edi', 'ebp')
_TOK = re.compile(r'\b(eax|ebx|ecx|edx|esi|edi|ebp|ax|bx|cx|dx|si|di|bp|[abcd][lh])\b')
_FAM = {'al': 'eax', 'ah': 'eax', 'ax': 'eax', 'bl': 'ebx', 'bh': 'ebx', 'bx': 'ebx', 'cl': 'ecx', 'ch': 'ecx',
        'cx': 'ecx', 'dl': 'edx', 'dh': 'edx', 'dx': 'edx', 'si': 'esi', 'di': 'edi', 'bp': 'ebp'}
_WIDTH = re.compile(r'^(movsx|movzx|cwde|cdq)\b|\b(word|byte) ptr\b|\bR[wb]\b')
_FOLD = re.compile(r'^(fdivr?p?|fmulp?|fdiv|imul|idiv|div|shl|sar|shr|lea)\b')


def region_class(xs, ys):
    a = collections.Counter(asm.mask(i.text, 'stack') for i in xs)
    b = collections.Counter(asm.mask(i.text, 'stack') for i in ys)
    if a == b:
        if collections.Counter(i.text for i in xs) == collections.Counter(i.text for i in ys):
            return 'schedule'
        if collections.Counter(asm.mask(i.text, 'regs') for i in xs) != \
                collections.Counter(asm.mask(i.text, 'regs') for i in ys):
            return 'stack'
        return 'regs'
    only = list((a - b).elements()) + list((b - a).elements())
    if all(_WIDTH.search(t) for t in only):
        return 'width'
    return 'semantic'


def reg_map(x, y, ops):
    """retail register -> Counter(ours register) over aligned instruction pairs
    whose masked spelling agrees (equal blocks included)."""
    m = collections.defaultdict(collections.Counter)
    for op, i1, i2, j1, j2 in ops:
        if i2 - i1 != j2 - j1:
            continue
        for k in range(i2 - i1):
            a, b = x[i1 + k].text, y[j1 + k].text
            if a == b or asm.mask(a, 'regs') != asm.mask(b, 'regs'):
                continue
            for ra, rb in zip(_TOK.findall(a), _TOK.findall(b)):
                m[_FAM.get(ra, ra)][_FAM.get(rb, rb)] += 1
    return {r: c.most_common(3) for r, c in m.items() if any(o != r for o, _ in c.items())}


_ADD = re.compile(r'^add (e[abcd]x|e[sd]i|ebp), (0x[0-9a-f]+|\d+)$')
_RMW = re.compile(r'^(inc|dec|add|sub|or|and) (dword|word|byte) ptr \[(?!esp)')


def _byref(ins):
    """Offsets N where `add R, N` is followed by a plain `[R]` read: the address
    of a member is formed first, as an inline helper taking it by reference does."""
    out = collections.Counter()
    for k, i in enumerate(ins):
        m = _ADD.match(i.text)
        if m and any(f'[{m.group(1)}]' in j.text for j in ins[k + 1:k + 4]):
            out[int(m.group(2), 0)] += 1
    return out


def hints(x, y, missing, extra):
    """Source-level readings of the gap, each from a rule that produced a match."""
    out = []
    a, b = _byref(x), _byref(y)
    for off, n in (a - b).items():
        out.append(f'retail forms the address of the member at +{off:#x} then reads through it (x{n}): '
                   'pass that member BY REFERENCE to an inline helper (0x2e3370 append_from, 0x2daae0 length helper)')
    for off, n in (b - a).items():
        out.append(f'ours forms the address of the member at +{off:#x} (x{n}) where retail reads it directly: '
                   'drop the reference/pointer local or re-read the full chain there (0x2daae0 after the call)')
    ra = sum(1 for i in x if _RMW.match(i.text))
    rb = sum(1 for i in y if _RMW.match(i.text))
    if ra > rb:
        out.append(f'retail updates memory in place {ra - rb} more time(s): `T &r = member; --r;` (0x31bc90, 0x30ccd0)')
    if rb > ra:
        out.append(f'ours updates memory in place {rb - ra} more time(s): retail loads, changes, stores (use a local copy)')
    names = [k for k, _ in missing]
    if any(k.startswith('fdivr') for k in names) and any(k.startswith('fmul') for k in names):
        out.append('retail takes a reciprocal then multiplies: name it (`float const inverse = 1.0f / ...;`) '
                   'or use an inline helper so the divide is not folded (0x2daae0)')
    if any(k.startswith(('movsx', 'movzx')) for k, _ in extra) and not any(k.startswith(('movsx', 'movzx')) for k in names):
        out.append('ours widens a value retail keeps full width: make the local `long` (0x317d90 first)')
    return out


def diagnose(theirs, va, data, masked):
    x, y = asm.decode(theirs, va), asm.ours(data, masked)
    ops = metrics.aligned(x, y)
    regions = []
    for op, i1, i2, j1, j2 in ops:
        if op == 'equal':
            continue
        xs, ys = x[i1:i2], y[j1:j2]
        regions.append(dict(cls=region_class(xs, ys), r0=xs[0].off if xs else (x[i1].off if i1 < len(x) else len(theirs)),
                            o0=ys[0].off if ys else (y[j1].off if j1 < len(y) else -1),
                            retail=[i.text for i in xs], ours=[i.text for i in ys]))
    a, b = asm.bag(x), asm.bag(y)
    missing, extra = a - b, b - a
    classes = []
    lr, lo = asm.live_in(theirs, va), asm.live_in(asm.strip_pad(asm.apply_mask(data, masked)))
    conv = sorted((lr - lo) - {'ecx', 'edx'})
    if conv:
        classes.append('convention')
    fr, fo = asm.frame(x), asm.frame(y)
    if fr != fo:
        classes.append('stack')
    sem = sum(missing.values()) + sum(extra.values())
    if sem:
        only = list(missing.elements()) + list(extra.elements())
        if all(_WIDTH.search(t) for t in only):
            classes.append('width')
        else:
            classes.append('semantic')
            if any(_WIDTH.search(t) for t in only):
                classes.append('width')
        if any(_FOLD.search(t) for t in only):
            classes.append('fold')
    kinds = collections.Counter(r['cls'] for r in regions)
    if kinds.get('regs'):
        classes.append('regs')
    if kinds.get('schedule') or (not sem and regions and not kinds.get('regs')):
        classes.append('schedule')
    return dict(classes=classes or ['schedule'], regions=regions, missing=missing.most_common(12),
                extra=extra.most_common(12), frame=(fr, fo), live_in=(sorted(lr), sorted(lo)), convention=conv,
                reg_map=reg_map(x, y, ops), x=x, y=y, ops=ops,
                hints=hints(x, y, missing.most_common(40), extra.most_common(40)))


def side_by_side(d, context=2, width=46):
    x, y, out = d['x'], d['y'], []
    for op, i1, i2, j1, j2 in d['ops']:
        if op == 'equal':
            n = i2 - i1
            show = range(n) if n <= 2 * context else list(range(context)) + [None] + list(range(n - context, n))
            for k in show:
                out.append('   ...' if k is None else f'   {x[i1+k].off:04x} {x[i1+k].text:{width}.{width}} | {y[j1+k].off:04x} {y[j1+k].text}')
            continue
        out.append(f'-- {region_class(x[i1:i2], y[j1:j2])}')
        for k in range(max(i2 - i1, j2 - j1)):
            l = f'{x[i1+k].off:04x} {x[i1+k].text}' if i1 + k < i2 else ''
            r = f'{y[j1+k].off:04x} {y[j1+k].text}' if j1 + k < j2 else ''
            out.append(f'!! {l:{width+5}.{width+5}} | {r}')
    return out
