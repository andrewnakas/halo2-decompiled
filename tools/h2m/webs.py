"""h2m webs VA [FILE]: per variable-like web, retail's location vs ours.

A register web is a register between one pure definition (a write that does not
read it: mov/lea/xor r,r/pop/call result) and the next, taken linearly over
retail's code; a stack web is one frame cell.  Retail and ours are aligned on
the instruction text with registers and slots masked, so renamed instructions
pair up.  Per web: how many aligned uses, where ours keeps the value, and the
first retail offset where the two part.  Renamings that are consistent over the
function are grouped: a transposition is one line `PURE SWAP esi<->edi from +0x..`,
a longer cycle `ROTATION`, a constant slot shift one line per run of cells."""
import collections, difflib, re
import capstone
from . import asm, classify, project, scorer

SAVED = ('esi', 'edi', 'ebx', 'ebp')
REGS = ('eax', 'ecx', 'edx', 'ebx', 'esi', 'edi', 'ebp')
FAM = dict(classify._FAM)
STK = re.compile(r'\[(esp|ebp)(?: ([-+]) ([^\]]+))?\]')


def access(data, base):
    """Per instruction: (offset, reads, writes) as 32-bit register families."""
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
    md.detail = True
    out = []
    for i in md.disasm(bytes(data), base):
        parts = i.op_str.split(', ')
        if i.mnemonic in ('xor', 'sub', 'sbb') and len(parts) == 2 and parts[0] == parts[1] and i.mnemonic != 'sbb':
            reads, writes = set(), {parts[0]}
        else:
            try:
                r, w = i.regs_access()
            except capstone.CsError:
                r, w = [], []
            reads, writes = {i.reg_name(x) for x in r}, {i.reg_name(x) for x in w}
        if i.mnemonic == 'call':
            writes |= {'eax', 'ecx', 'edx'}
        reads = {FAM.get(x, x) for x in reads} & set(REGS)
        writes = {FAM.get(x, x) for x in writes} & set(REGS)
        out.append((i.address - base, reads, writes, i.mnemonic))
    return out


def reg_webs(x, acc):
    """[(reg, first index, last index)] of retail, linear segmentation at pure definitions."""
    webs, cur = [], {}
    for k, (off, reads, writes, mnem) in enumerate(acc):
        if k >= len(x):
            break
        for r in REGS:
            pure = r in writes and r not in reads
            if mnem == 'push' and x[k].ops == r and r in SAVED and r not in cur:
                continue                                   # prologue save
            if pure or r not in cur:
                if r in reads or r in writes:
                    if r in cur:
                        webs.append((r, cur[r][0], cur[r][1]))
                    cur[r] = [k, k]
            elif r in reads or r in writes:
                cur[r][1] = k
    webs += [(r, a, b) for r, (a, b) in cur.items()]
    return sorted(webs, key=lambda w: (w[1], w[0]))


def slot(t):
    m = STK.search(t)
    if not m:
        return None
    base, sign, rest = m.groups()
    if rest is None:
        return base + '+0x0'
    rest = rest.replace(' ', '')
    num = re.findall(r'0x[0-9a-f]+|\b\d+\b', rest)
    regs = re.sub(r'(\+|-)?(0x[0-9a-f]+|\b\d+\b)', '', rest)
    if regs:
        return None                                        # indexed frame access: not one cell
    return f'{base}{sign}{int(num[-1], 0) if num else 0:#x}'


def pairs(x, y):
    """Aligned (retail index, ours index) pairs whose masked text agrees."""
    mx = [asm.mask(i.text, 'stack') for i in x]
    my = [asm.mask(i.text, 'stack') for i in y]
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, mx, my, autojunk=False).get_opcodes():
        if op == 'equal':
            out += [(i1 + k, j1 + k) for k in range(i2 - i1)]
    return out


def table(theirs, va, x, y):
    acc = access(theirs, va)
    webs = reg_webs(x, acc)
    pr = pairs(x, y)
    # register -> list of (index, ours register)
    seen = collections.defaultdict(list)
    cells = collections.defaultdict(list)
    for i, j in pr:
        a, b = x[i].text, y[j].text
        for ra, rb in zip(classify._TOK.findall(a), classify._TOK.findall(b)):
            seen[FAM.get(ra, ra)].append((i, FAM.get(rb, rb)))
        sa, sb = slot(a), slot(b)
        if sa and sb:
            cells[sa].append((i, sb))
    rows = []
    for r, a, b in webs:
        hits = [(i, o) for i, o in seen.get(r, ()) if a <= i <= b]
        if not hits:
            continue
        c = collections.Counter(o for _, o in hits)
        part = next((i for i, o in hits if o != r), None)
        kind = 'same' if set(c) == {r} else 'renamed' if len(c) == 1 else 'split'
        rows.append(dict(reg=r, a=a, b=b, n=len(hits), ours=c, part=part, kind=kind, to=c.most_common(1)[0][0]))
    return rows, cells, len(pr)


def groups(rows):
    """Consistent cycles among the webs: [(cycle tuple, first offset index, webs, pairs, pure?)]."""
    dom = {}
    for r in REGS:
        c = collections.Counter()
        for w in rows:
            if w['reg'] == r and w['kind'] == 'renamed':
                c[w['to']] += w['n']
        if c:
            dom[r] = c.most_common(1)[0][0]
    out, used = [], set()
    for r in dom:
        if r in used:
            continue
        cyc, q = [r], dom[r]
        while q in dom and q not in cyc:
            cyc.append(q)
            q = dom[q]
        if q != r or len(cyc) < 2:
            continue
        member = [w for w in rows if w['reg'] in cyc]
        moved = [w for w in member if w['kind'] == 'renamed' and w['to'] == dom[w['reg']]]
        start = min(w['a'] for w in moved)
        stray = [w for w in member if w not in moved and w['b'] >= start]
        used.update(cyc)
        out.append((tuple(cyc), min(w['part'] for w in moved), moved, sum(w['n'] for w in moved), len(stray),
                    [w for w in member if w not in moved and w['a'] < start and w['b'] < start]))
    return out


def render(va, theirs, x, y, show_all=False):
    rows, cells, npairs = table(theirs, va, x, y)
    diff = [w for w in rows if w['kind'] != 'same']
    out = [f'{va:#x} webs: {len(rows)} retail register webs with aligned uses ({npairs} aligned instructions of {len(x)}), '
           f'{len(diff)} located differently']
    grouped = set()
    for cyc, first, moved, n, strays, before in groups(rows):
        pure = not strays
        name = ('PURE SWAP ' + '<->'.join(cyc)) if len(cyc) == 2 else ('PURE ROTATION ' + '->'.join(cyc + (cyc[0],)))
        if not pure:
            name = name.replace('PURE ', '') + f' ({strays} later webs of these registers agree or go elsewhere)'
        if not any(r in SAVED for r in cyc):
            name = 'SCRATCH ' + name
        out.append(f'{name} from +{x[first].off:#x}: retail {" / ".join(cyc)} = ours {" / ".join(next(w["to"] for w in moved if w["reg"] == r) for r in cyc)}'
                   f'  ({len(moved)} webs, {n} aligned uses' + (f'; {len(before)} earlier webs agree' if before else '') + ')')
        if pure:
            grouped.update(id(w) for w in moved)
    cyc_regs = {r for g in groups(rows) for r in g[0]}
    chain = collections.defaultdict(list)
    for w in rows:
        if w['kind'] == 'renamed' and w['reg'] not in cyc_regs and (w['reg'] in SAVED or w['to'] in SAVED):
            chain[(w['reg'], w['to'])].append(w)
    for (r, o), ws in sorted(chain.items(), key=lambda kv: min(w['a'] for w in kv[1])):
        same = sum(1 for w in rows if w['reg'] == r and w['kind'] == 'same')
        out.append(f'RENAME {r}->{o} from +{x[min(w["part"] for w in ws)].off:#x}  ({len(ws)} webs, {sum(w["n"] for w in ws)} aligned uses; '
                   f'{same} other {r} webs agree)')
    out.append('  reg   retail range     uses  ours                parts at  defined by')
    for w in rows:
        if (w['kind'] == 'same' and not show_all) or (id(w) in grouped and not show_all):
            continue
        ours = ' '.join(f'{o} x{n}' for o, n in w['ours'].most_common())
        part = f'+{x[w["part"]].off:#06x}' if w['part'] is not None else '   -   '
        tag = {'same': ' ', 'renamed': '*', 'split': 'S'}[w['kind']] + ('g' if id(w) in grouped else ' ')
        out.append(f'{tag}{w["reg"]:4} +{x[w["a"]].off:#06x}..+{x[w["b"]].off:#06x} {w["n"]:4d}  {ours:20.20}  {part}  {x[w["a"]].text}')
    if grouped and not show_all:
        out.append(f'  ({len(grouped)} webs covered by the grouped lines above; --all lists them)')
    # stack cells
    def key(s):
        return (s[:3], int(s[4:], 16) * (1 if s[3] == '+' else -1))
    lines, run = [], None
    for s in sorted(cells, key=key):
        c = collections.Counter(o for _, o in cells[s])
        if set(c) == {s}:
            if show_all:
                lines.append(f'   {s:12} same x{len(cells[s])}')
            run = None
            continue
        part = next(i for i, o in cells[s] if o != s)
        to = c.most_common(1)[0][0]
        delta = (key(to)[1] - key(s)[1]) if len(c) == 1 and to[:3] == s[:3] else None
        if delta is not None and run and run['delta'] == delta and run['base'] == s[:3]:
            run['cells'].append(s)
            run['n'] += len(cells[s])
            run['part'] = min(run['part'], part)
        else:
            run = dict(delta=delta, base=s[:3], cells=[s], n=len(cells[s]), part=part,
                       text=', '.join(f'{o} x{n}' for o, n in c.most_common()))
            lines.append(run)
    out.append(f'stack cells: {len(cells)} with aligned uses, {sum(len(r["cells"]) if isinstance(r, dict) else 0 for r in lines)} located differently')
    for r in lines:
        if not isinstance(r, dict):
            out.append(r)
        elif r['delta'] is not None:
            span = r['cells'][0] if len(r['cells']) == 1 else f'{r["cells"][0]}..{r["cells"][-1]}'
            out.append(f' * [{span}] ours {r["delta"]:+#x} ({len(r["cells"])} cells, {r["n"]} uses) from +{x[r["part"]].off:#x}')
        else:
            out.append(f' S [{r["cells"][0]}] ours {r["text"]} from +{x[r["part"]].off:#x}')
    return out


def main(a):
    row = project.rows().get(a.va)
    if row is not None and row['owner'] == 'game':
        from . import image
        s = image.ImageScorer(a.va, a.sym)
    else:
        s = scorer.Scorer(a.va, a.sym)
    text = project.read(a.files[0]) if a.files else s.base
    x, y, f = s.listing(text)
    print('\n'.join(render(a.va, s.theirs, x, y, a.all)))
    return 0
