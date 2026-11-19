"""h2m probe: diagnostic perturbations of an open function.

NOT for submitted source: a probe adds code retail does not have (a store of a
variable into an external global, a dummy scratch value, `volatile`, an escaped
address) to measure what the compiler's register/slot allocator needs.  Each
variant is compiled once; the probe's own instructions are found through their
relocations (symbols `probe_*`), removed, and the rest compared with retail:

  raw    lock/dist/sem/first of the variant as it is (also put in the ledger)
  net    aligned distance / first offset with the probe instructions removed,
         padding dropped and labels masked (NET-MATCH = retail's code + probe)
  rd     aligned instruction pairs that differ only by register
  site   whether retail's instruction at the BASE's first net difference is
         now reproduced
  at     register each probed variable was read from (its home at the probe)

Spec: one variant family per line, `&` joins perturbations into one variant,
`#` comments.  WHERE is a file line number (insert before it), `+N` (body line),
`top`, `end`, or `dD` (next to VAR's first use at loop depth D).
  use VAR xK[..M] @WHERE [-> REG] [as store|add|touch]
  unuse VAR                      caching forms that lower VAR's use count
  scratch [xK[..M]] @WHERE       K dummy scratch values live across the statement
  decl-order A B [C [D]]         permutations of those locals' declarations
  volatile VAR                   slot VAR (address escapes through a call)
  raw @WHERE: text               replace @WHERE: text          delete @WHERE
--auto: rival callee-saved pairs from the retail->ours register map, variables
located by a +1 probe, then use-titration K=1..4 at each loop depth for both."""
import itertools, json, os, re, struct
from . import asm, classify, ledger, measure, metrics, mutators, objcmp, project

DECLS = ('extern long probe_sink_85[64];\nextern void probe_touch_85(long);\n'
         'extern void probe_addr_85(void const *);\n')
SAVED = ('esi', 'edi', 'ebx', 'ebp')
PAD = re.compile(r'^(nop|int3|lea (e\w\w), (?:dword ptr )?\[\2\]|mov (e\w\w), \3|lea esp, \[esp\])$')
LOOP = re.compile(r'^(for|while|do)\b')
INT_TYPE = re.compile(r'^(?:const\s+)?(?:unsigned\s+|signed\s+)?(?:long|int|short|char|dword|word|byte)(?:\s+const)?$')


# ----------------------------------------------------------------- source model

class _Cx(mutators.Ctx):
    def __init__(self, text, start):
        import permute, random
        self.context = permute.function_context(text, start)
        self.rng = random.Random(0)
        self._types = {}


class Fn:
    """The body the perturbations are applied to: the function marked with va, or
    (helper=NAME) a file-local helper that is inlined into it."""

    def __init__(self, va, text, helper=None):
        self.va, self.text = va, text
        if helper:
            mk = re.search(r'^[^\n;{}#/]*\b%s\s*\([^;{}]*\)\s*(?:const\s*)?\{' % re.escape(helper), text, re.M)
            if not mk:
                raise SystemExit(f'no function body named {helper} in the file')
            self.s = mk.end()
            self.e = project.reduced.match_brace(text, mk.end() - 1)
            self.sig_s = mk.start()
        else:
            self.s, self.e = project.reduced.find_body(text, va)
            mk = re.search(r'^[ \t]*//[ \t]*@retail[ \t]+0x0*%x\b[^\n]*\n' % va, text, re.M | re.I)
            self.sig_s = mk.end()
        self.body = text[self.s:self.e]
        self.m = mutators.Model(self.body)
        self.line0 = text.count('\n', 0, self.s) + 1          # file line of body line 0
        self.sig = text[self.sig_s:self.s]
        self.member = '::' in self.sig.split('(')[0]
        self.cx = _Cx(text, self.s)
        self.types = dict(self.cx.types(self.body))
        self.loop = self._loops()

    def _loops(self):
        m, out, stack = self.m, [], []
        for i, l in enumerate(m.lines):
            out.append(sum(stack))
            c = mutators.code(l)
            for k, ch in enumerate(c):
                if ch == '{':
                    head = c[:k].strip()
                    if not head:
                        j, par = m.prev_sig(i), 0
                        while j >= 0:
                            cj = mutators.code(m.lines[j])
                            par += cj.count(')') - cj.count('(')
                            if par <= 0:
                                break
                            j = m.prev_sig(j)
                        head = mutators.code(m.lines[j]).strip() if j >= 0 else ''
                    stack.append(bool(LOOP.match(head)))
                elif ch == '}' and stack:
                    stack.pop()
        for i, l in enumerate(m.lines):                       # braceless loop bodies
            c = mutators.code(l).strip()
            if re.match(r'(for|while)\b', c) and c.count('(') == c.count(')') and c.endswith(')'):
                j = m.next_sig(i)
                if j < len(m.lines) and not mutators.code(m.lines[j]).strip().startswith('{'):
                    e = m.statement_end(j)
                    for q in range(j, (e if e is not None else j) + 1):
                        out[q] += 1
        return out

    def decl_line(self, var):
        for i, l in enumerate(self.m.lines):
            d = mutators.declaration(l)
            if d and d.group('name') == var:
                return i
        pat = re.compile(r'^\s*(?!return\b|delete\b|goto\b|else\b)[A-Za-z_][\w:<>\s\*&]*[\s\*&]%s\s*(=(?!=)|;|\(|\[)' % re.escape(var))
        for i, l in enumerate(self.m.lines):                  # a declaration over several lines
            if pat.match(mutators.code(l)) and self.m.starts_statement(i):
                return i
        return None

    def decl_end(self, var):
        d = self.decl_line(var)
        if d is None:
            return None
        e = self.m.statement_end(d)
        return d if e is None else e

    def scope(self, var):
        d = self.decl_line(var)
        if d is None:
            return 0, len(self.m.lines) - 1
        return self.decl_end(var) + 1, self.m.close[self.m.opener[d]]

    def braceless(self, i):
        """Line i is the one-line body of a braceless for/while/if: a probe must wrap it."""
        m = self.m
        c = mutators.code(m.lines[i]).strip()
        p = m.prev_sig(i)
        ps = mutators.code(m.lines[p]).strip() if p >= 0 else ''
        return bool(c.endswith(';') and not c.startswith(('{', '}')) and (re.match(r'(for|while|if)\b', ps) and ps.endswith(')') or ps == 'else'))

    def mention_lines(self, var):
        a, b = self.scope(var)
        return [i for i in range(a, b) if mutators.mentions(self.m.lines[i], var)]

    def depths(self, var):
        if var == 'this':
            return sorted(set(self.loop))
        return sorted({self.loop[i] for i in self.mention_lines(var)} | {self.loop[min(self.scope(var)[0], len(self.loop) - 1)]})

    def where(self, spec, var=None):
        """Body line index to insert before."""
        m = self.m
        if re.fullmatch(r'\d+', spec):
            return int(spec) - self.line0
        if spec.startswith('+'):
            return int(spec[1:])
        top = m.statements(-1)
        if spec == 'top':
            return top[0][0]
        if spec == 'end':
            if top and mutators.code(m.lines[top[-1][0]]).strip().startswith('return'):
                return top[-1][0]
            return len(m.lines) - 1
        d = re.fullmatch(r'd(\d+)', spec)
        if not d:
            raise ValueError(f'bad place @{spec}')
        depth = int(d.group(1))
        a, b = self.scope(var) if var else (0, len(m.lines) - 1)
        ok = [i for i in range(a, b) if self.loop[i] == depth and (m.starts_statement(i) or self.braceless(i))]
        near = [i for i in ok if var and var != 'this' and mutators.mentions(m.lines[i], var)]
        if near:
            return near[0]
        if depth == 0 and (var is None or var == 'this' or self.decl_line(var) is None):
            return top[0][0]
        if ok:
            return ok[0]
        raise ValueError(f'no statement at loop depth {depth} in the scope of {var}')

    def ind(self, i):
        j = i if i < len(self.m.lines) and mutators.significant(self.m.lines, i) else self.m.prev_sig(i)
        s = mutators.indent(self.m.lines[j]) if j >= 0 else '\t'
        if i >= len(self.m.lines) - 1 or mutators.code(self.m.lines[i]).strip().startswith('}'):
            s = (mutators.indent(self.m.lines[i]) if i < len(self.m.lines) else '') + '\t'
        return s or '\t'

    def build(self, edits):
        """edits: [(body line, 'ins'|'rep'|'sig', text)] against the original lines."""
        lines = self.m.lines[:]
        sig = self.sig
        order = sorted(enumerate(edits), key=lambda e: (-e[1][0], e[1][1] == 'ins', -e[0]))
        for _, (i, kind, t) in order:
            if kind == 'ins':
                lines.insert(i, t)
            elif kind == 'rep':
                lines[i] = t
            elif kind == 'sig':
                sig = t(sig)
        text = self.text[:self.sig_s] + sig + '\n'.join(lines) + self.text[self.e:]
        inc = [m.end() for m in re.finditer(r'^[ \t]*#[ \t]*include[^\n]*\n', self.text[:self.sig_s], re.M)]
        if inc:
            at = inc[-1]
        else:                                                       # after the leading comment lines
            at = 0
            while self.text.startswith('//', at):
                at = self.text.find('\n', at) + 1
        return text[:at] + DECLS + text[at:]

    def aliases(self):
        """Names that a local merely renames (`T *x = (T *)x_;`): probing both is one experiment."""
        out = set()
        for l in self.m.lines:
            d = mutators.declaration(l)
            if d and d.group('init'):
                init = d.group('init').lstrip('=').strip()
                m = re.fullmatch(r'(?:\([^()]*\)\s*)*(\w+)', init)
                if m and (m.group(1) in self.types or m.group(1) == 'this'):
                    out.add(m.group(1))
        return out


# ----------------------------------------------------------------- perturbations
# a family = [(label, fn(Fn, counter) -> edits, meta)]

def _val(var):
    return f'(long){var}' if re.fullmatch(r'\w+', var) else f'(long)({var})'


def fam_use(fn, var, ks, place, form='store', target=None):
    out = []
    for k in ks:
        def edits(fn, n, k=k):
            i = fn.where(place, var)
            ind = fn.ind(i)
            e = []
            for _ in range(k):
                j = next(n)
                e.append((i, 'ins', {'store': f'{ind}probe_sink_85[{j}] = {_val(var)};',
                                     'add': f'{ind}probe_sink_85[{j}] += {_val(var)};',
                                     'touch': f'{ind}probe_touch_85({_val(var)});'}[form]))
            if fn.braceless(i):
                return [(i, 'rep', mutators.indent(fn.m.lines[i]) + '{ ' + ' '.join(x[2].strip() for x in e) + ' '
                         + fn.m.lines[i].strip() + ' }')]
            return e
        out.append((f'use {var} x{k} @{place}' + ('' if form == 'store' else f' as {form}'), edits,
                    dict(kind='use', var=var, k=k, place=place, form=form, target=target)))
    return out


def fam_scratch(fn, ks, place):
    out = []
    for k in ks:
        def edits(fn, n, k=k):
            i = fn.where(place)
            e = fn.m.statement_end(i)
            ind = fn.ind(i)
            ids = [next(n) for _ in range(2 * k)]
            ed = [(i, 'ins', f'{ind}long probe_tmp{q}_85 = probe_sink_85[{ids[2 * q]}];') for q in range(k)]
            ed += [((e if e is not None else i) + 1, 'ins', f'{ind}probe_sink_85[{ids[2 * q + 1]}] = probe_tmp{q}_85;')
                   for q in range(k)]
            return ed
        out.append((f'scratch x{k} @{place}', edits, dict(kind='scratch', k=k, place=place)))
    return out


def fam_decl_order(fn, names):
    idx = [fn.decl_line(n) for n in names]
    if any(i is None for i in idx):
        raise ValueError('decl-order: not a single-line local declaration: ' + ', '.join(n for n, i in zip(names, idx) if i is None))
    out = []
    pos = sorted(idx)
    for perm in itertools.islice(itertools.permutations(range(len(names))), 1, 24):
        def edits(fn, n, perm=perm):
            src = [fn.m.lines[i] for i in pos]
            return [(pos[a], 'rep', mutators.indent(src[a]) + src[b].strip()) for a, b in enumerate(perm) if a != b]
        order = ' '.join(mutators.declaration(fn.m.lines[pos[b]]).group('name') for b in perm)
        out.append((f'decl-order {order}', edits, dict(kind='decl-order')))
    return out


def fam_volatile(fn, var):
    def edits(fn, n):
        i = fn.decl_line(var)
        if i is not None:
            return [(i, 'rep', re.sub(r'\b%s\b' % re.escape(var), 'volatile ' + var, fn.m.lines[i], count=1))]
        return [(0, 'sig', lambda s: re.sub(r'\b%s\b(?=\s*[,)])' % re.escape(var), 'volatile ' + var, s, count=1))]
    return [(f'volatile {var}', edits, dict(kind='volatile', var=var))]


def fam_slot(fn, var):
    def edits(fn, n):
        i = fn.decl_end(var)
        at = fn.where('top') if i is None else i + 1
        return [(at, 'ins', f'{fn.ind(at)}probe_addr_85((void const *)&{var});')]
    return [(f'slot {var}', edits, dict(kind='slot', var=var))]


def fam_raw(fn, kind, place, text):
    def edits(fn, n):
        i = fn.where(place)
        if kind == 'raw':
            return [(i, 'ins', fn.ind(i) + text)]
        return [(i, 'rep', (mutators.indent(fn.m.lines[i]) + text) if kind == 'replace' else '')]
    return [(f'{kind} @{place}' + (f': {text}' if text else ''), edits, dict(kind=kind))]


def fam_unuse(fn, var, limit=8):
    """(a) every member chain rooted at VAR read on 2+ lines cached in one local;
    (b) VAR re-read from a memory home on one line (the deepest lines first)."""
    m, out = fn.m, []
    occ = {}
    lines = fn.mention_lines(var)
    for i in lines:
        for key, a, b in mutators._chain_reads(m.lines[i]):
            if re.match(r'%s(?:->|\.|\[)' % re.escape(var), key):
                occ.setdefault(key, []).append((i, a, b))
    for key, hits in sorted(occ.items(), key=lambda kv: -len(kv[1])):
        rows = sorted({i for i, _, _ in hits})
        if len(rows) < 2:
            continue
        t = fn.cx.etype(key, fn.body)
        if not t:
            continue
        op = m.opener[rows[0]]
        while op != -1 and not all(op < r <= m.close[op] for r in rows):
            op = m.opener[op]
        slot = next((s for s, e in m.statements(op) if s <= rows[0] <= e), None)
        if slot is None:
            continue
        name = mutators.fresh(fn.body, 'probe_' + mutators.field_name(key))

        def edits(fn, n, hits=hits, slot=slot, t=t, name=name):
            ed, first = [], None
            for i in sorted({i for i, _, _ in hits}):
                l = m.lines[i]
                for _, a, b in sorted((h for h in hits if h[0] == i), key=lambda h: -h[1]):
                    first = first or l[a:b]
                    l = l[:a] + name + l[b:]
                ed.append((i, 'rep', l))
            i0, a0, b0 = hits[0]
            ed.append((slot, 'ins', f'{mutators.indent(m.lines[slot])}{mutators.decl_text(t, name)} = {m.lines[i0][a0:b0]};'))
            return ed
        out.append((f'unuse {var}: cache {key} ({len(rows)} lines -> 1)', edits, dict(kind='unuse', var=var)))
    t = fn.types.get(var)
    if t and var != 'this':
        d = fn.decl_end(var)
        for i in sorted((i for i in lines if m.starts_statement(i) and m.statement_end(i) == i),
                        key=lambda i: (-fn.loop[i], i))[:4]:
            def edits(fn, n, i=i):
                j = next(n)
                at = fn.where('top') if d is None else d + 1
                new = re.sub(r'(?<!->)(?<!\.)(?<!::)\b%s\b' % re.escape(var), f'(({t})probe_sink_85[{j}])', m.lines[i])
                return [(at, 'ins', f'{fn.ind(at)}probe_sink_85[{j}] = {_val(var)};'), (i, 'rep', new)]
            out.append((f'unuse {var}: reload from a home on line {fn.line0 + i} (depth {fn.loop[i]})', edits,
                        dict(kind='unuse', var=var)))
    return out[:limit]


def _ks(tok):
    m = re.fullmatch(r'x(\d+)(?:\.\.(\d+))?', tok)
    return list(range(int(m.group(1)), int(m.group(2) or m.group(1)) + 1))


def parse(fn, spec):
    """spec text -> [[family, ...] per line]"""
    out = []
    for raw in spec.splitlines():
        line = raw.split('#')[0].strip() if not re.match(r'\s*(raw|replace)\b', raw) else raw.strip()
        if not line:
            continue
        fams = []
        for part in (line.split(' & ') if not line.startswith(('raw', 'replace')) else [line]):
            w = part.split()
            if w[0] == 'use':
                var = w[1]
                ks = _ks(next((t for t in w[2:] if re.fullmatch(r'x\d+(\.\.\d+)?', t)), 'x1'))
                place = next((t[1:] for t in w[2:] if t.startswith('@')), 'd0')
                target = w[w.index('->') + 1] if '->' in w else None
                form = w[w.index('as') + 1] if 'as' in w else 'store'
                fams.append(fam_use(fn, var, ks, place, form, target))
            elif w[0] == 'unuse':
                fams.append(fam_unuse(fn, w[1]))
            elif w[0] == 'scratch':
                ks = _ks(next((t for t in w[1:] if t.startswith('x')), 'x1'))
                fams.append(fam_scratch(fn, ks, next(t[1:] for t in w[1:] if t.startswith('@'))))
            elif w[0] == 'decl-order':
                fams.append(fam_decl_order(fn, w[1:]))
            elif w[0] == 'volatile':
                fams.append(fam_volatile(fn, w[1]))
            elif w[0] == 'slot':
                fams.append(fam_slot(fn, w[1]))
            elif w[0] in ('raw', 'replace', 'delete'):
                mm = re.match(r'(raw|replace|delete)\s+@(\S+?)(?::\s?(.*))?$', part)
                fams.append(fam_raw(fn, mm.group(1), mm.group(2), mm.group(3) or ''))
            else:
                raise ValueError(f'unknown perturbation: {part}')
        out.append(fams)
    return out


def variants(fn, groups):
    """[(label, text, metas)]; a family whose place cannot be resolved is reported, not fatal."""
    out, bad = [], []
    for fams in groups:
        for combo in itertools.product(*fams):
            n = itertools.count()
            label = ' & '.join(c[0] for c in combo)
            try:
                edits = [e for c in combo for e in c[1](fn, n)]
                out.append((label, fn.build(edits), [c[2] for c in combo]))
            except (ValueError, IndexError, TypeError) as e:
                bad.append(f'{label}: {e}')
    return out, bad


# ----------------------------------------------------------------- compile + analysis

def coff_probe(data):
    """{function: (bytes, masked offsets, {offset: probe symbol})} of an object."""
    _, nsec, _, symptr, nsym, opt, _ = struct.unpack_from('<HHIIIHH', data, 0)
    strtab = symptr + 18 * nsym

    def symname(k):
        raw = data[symptr + 18 * k:symptr + 18 * k + 8]
        if raw[:4] == b'\0\0\0\0':
            so = strtab + struct.unpack_from('<I', raw, 4)[0]
            return data[so:data.index(b'\0', so)].decode('latin1')
        return raw.rstrip(b'\0').decode('latin1')
    secs = []
    for i in range(nsec):
        o = 20 + opt + 40 * i
        _, _, rsize, rptr, relptr, _, nrel, _, ch = struct.unpack_from('<IIIIIIHHI', data, o + 8)
        rel = {}
        for r in range(nrel):
            off, sidx, _ = struct.unpack_from('<IIH', data, relptr + 10 * r)
            rel[off] = symname(sidx)
        secs.append((data[rptr:rptr + rsize] if rptr else b'', rel, ch))
    by_sec, i = {}, 0
    while i < nsym:
        value, secnum, typ, _, naux = struct.unpack_from('<IhHBB', data, symptr + 18 * i + 8)
        if secnum > 0 and (typ >> 4) == 2 and secs[secnum - 1][2] & 0x20:
            by_sec.setdefault(secnum, []).append((value, symname(i)))
        i += 1 + naux
    out = {}
    for secnum, items in by_sec.items():
        sdata, rel, _ = secs[secnum - 1]
        items.sort()
        for k, (value, name) in enumerate(items):
            end = items[k + 1][0] if k + 1 < len(items) else len(sdata)
            masked = {o - value + d for o in rel if value <= o < end for d in range(4)}
            out[name] = (sdata[value:end], masked, {o - value: s for o, s in rel.items() if value <= o < end and 'probe_' in s})
    return out


def split_probe(y, prel):
    """(instructions without the probe's own, [probe instruction texts]).  Offsets and
    labels of the kept instructions are moved up as if the probe code were absent."""
    drop = set()
    for k, i in enumerate(y):
        syms = [s for o, s in prel.items() if i.off <= o < i.off + i.size]
        if not syms:
            continue
        drop.add(k)
        if i.mnem == 'call':
            if k and y[k - 1].mnem == 'push':
                drop.add(k - 1)
            if k + 1 < len(y) and y[k + 1].text == 'add esp, 4':
                drop.add(k + 1)
    new_off, shift = {}, 0
    for k, i in enumerate(y):
        new_off[i.off] = i.off - shift
        if k in drop:
            shift += i.size
    kept = []
    for k, i in enumerate(y):
        if k in drop:
            continue
        t = asm.LBL.sub(lambda m: 'L%x' % new_off.get(int(m.group(0)[1:], 16), int(m.group(0)[1:], 16)), i.text)
        kept.append(asm.Ins(new_off[i.off], i.size, i.mnem, i.ops, t))
    return kept, [y[k].text for k in sorted(drop)]


def core(ins):
    return [(i.off, asm.LBL.sub('L', i.text)) for i in ins if not PAD.match(i.text)]


def net(x, y):
    """Net comparison of retail x with ours y (probe already removed)."""
    import difflib
    cx, cy = core(x), core(y)
    ops = difflib.SequenceMatcher(None, [t for _, t in cx], [t for _, t in cy], autojunk=False).get_opcodes()
    dist = sum(max(i2 - i1, j2 - j1) for op, i1, i2, j1, j2 in ops if op != 'equal')
    first = next((cx[i1][0] if i1 < len(cx) else -1 for op, i1, i2, j1, j2 in ops if op != 'equal'), -1)
    a, b = asm.bag(x), asm.bag(y)
    sem = sum((a - b).values()) + sum((b - a).values())
    equal = {cx[k][0] for op, i1, i2, j1, j2 in ops if op == 'equal' for k in range(i1, i2)}
    mx = [asm.mask(t, 'stack') for _, t in cx]
    my = [asm.mask(t, 'stack') for _, t in cy]
    rd, rmap = 0, {}
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, mx, my, autojunk=False).get_opcodes():
        if op != 'equal':
            continue
        for k in range(i2 - i1):
            ta, tb = cx[i1 + k][1], cy[j1 + k][1]
            if ta != tb:
                rd += 1
                for ra, rb in zip(classify._TOK.findall(ta), classify._TOK.findall(tb)):
                    ra, rb = classify._FAM.get(ra, ra), classify._FAM.get(rb, rb)
                    if ra != rb:
                        rmap.setdefault(ra, {}).setdefault(rb, 0)
                        rmap[ra][rb] += 1
    return dict(dist=dist, first=first, sem=sem, rd=rd, equal=sorted(equal), rmap=rmap)


class Prober:
    def __init__(self, va, base_path=None, sym=None, out='probe', helper=None):
        self.va, self.sym = va, sym
        self.path = base_path or project.source_path(va)
        self.text = project.read(self.path)
        self.fn = Fn(va, self.text, helper)
        self.theirs = project.retail(va)
        self.x = asm.decode(self.theirs, va)
        self.dir = os.path.join(project.workdir(va), out)
        os.makedirs(self.dir, exist_ok=True)
        self.cache_path = os.path.join(project.workdir(va), 'probe_cache.json')
        try:
            self.cache = json.load(open(self.cache_path))
        except (OSError, ValueError):
            self.cache = {}
        self.name = ledger.fact(va, 'symbol') if not sym else None
        self.compiled = 0

    def _compile(self, text):
        h = ledger.digest(text)
        src = os.path.join(project.workdir(self.va), f'w_{h}_{os.path.basename(project.source_path(self.va))}')
        obj = src[:-4] + '.obj'
        open(src, 'w', encoding='utf-8').write(text)
        try:
            objcmp.compile_obj(src, obj)
            return coff_probe(open(obj, 'rb').read())
        finally:
            for p in (src, obj):
                if os.path.exists(p):
                    os.remove(p)

    def result(self, text, label='', budget=None):
        """Cached analysis of one variant; None when the compile budget is used up."""
        h = ledger.digest(text)
        if h in self.cache:
            return self.cache[h]
        if budget is not None and self.compiled >= budget:
            return None
        self.compiled += 1
        try:
            funcs = self._compile(text)
            trip = [(n, d, mk) for n, (d, mk, _) in funcs.items()]
            f = next((t for t in trip if t[0] == self.name), None) if self.name else None
            if f is None:
                f = measure.pick(trip, self.theirs, self.va, self.sym)
            if f is None:
                r = dict(error='function absent from object')
            else:
                if not self.name and not self.sym:
                    self.name = f[0]
                v = metrics.vector(f[1], f[2], self.theirs, self.va)
                y, probes = split_probe(asm.ours(f[1], f[2]), funcs[f[0]][2])
                r = dict(v=v, net=net(self.x, y), probes=probes)
        except SystemExit as e:
            r = dict(error=str(e)[-600:])
        ledger.put(self.va, h, r.get('v') or dict(error=r['error']), None, 'probe', label[:300], None)
        self.cache[h] = r
        tmp = self.cache_path + f'.{os.getpid()}'
        json.dump(self.cache, open(tmp, 'w'))
        os.replace(tmp, self.cache_path)
        return r

    def save(self, label, text, r):
        slug = re.sub(r'[^A-Za-z0-9]+', '_', label).strip('_')[:60]
        kind = 'probe_match_' if r['v'].get('match') else 'probe_netmatch_' if r['net']['dist'] == 0 else 'p_'
        p = os.path.join(self.dir, f'{kind}{slug}.cpp')
        open(p, 'w', encoding='utf-8').write(text)
        return p


def probe_regs(r):
    out = []
    for t in r.get('probes', ()):
        m = re.match(r'(?:mov|add) dword ptr \[ADDR\], (\w+)$', t) or re.match(r'push (e\w\w)$', t)
        if m:
            out.append(m.group(1))
    return out


def home(r):
    """Register the probed variable was read from (the last probe store), or None."""
    regs = probe_regs(r)
    return regs[-1] if regs else None


def reached(base, r):
    """Retail's instruction at the base's first net difference is reproduced and nothing before it broke."""
    b, n = base['net']['first'], r['net']
    return b >= 0 and b in n['equal'] and (n['first'] < 0 or n['first'] > b)


def line(label, r, base, path=''):
    if r is None:
        return f'  {label:44.44}  (pending)'
    if 'error' in r:
        errs = [e for e in r['error'].splitlines() if ' error ' in e] or r['error'].strip().splitlines()[-1:]
        return f'  {label:44.44}  ERROR ' + errs[0].split(') : ')[-1][:110]
    v, n = r['v'], r['net']
    raw = 'MATCH' if v['match'] else f"lock {v['lock']:4d} dist {v['dist']:4d} sem {v['sem']:3d} first +{v['first']:#x}"
    tag = '= retail' if v['match'] else 'NET-MATCH' if n['dist'] == 0 else f"dist {n['dist']:4d} sem {n['sem']:3d} first +{n['first']:#x}"
    site = ''
    if base and base['net']['first'] >= 0:
        site = ' site=OK' if reached(base, r) else ' site=no'
    regs = probe_regs(r)
    at = (' at ' + ','.join(dict.fromkeys(regs))) if regs else ''
    return f'  {label:44.44}  raw {raw} | net {tag} rd {n["rd"]:3d}{site}{at}' + (f'  {os.path.relpath(path)}' if path else '')


def fmt_map(rmap):
    return '  '.join(f'{a}->' + '/'.join(f'{b}x{c}' for b, c in sorted(d.items(), key=lambda kv: -kv[1])[:3])
                     for a, d in sorted(rmap.items()))


def run(p, vs, budget, say=print, keep=True):
    """Score variants [(label, text, metas)] -> [(label, text, metas, result)], pending count."""
    base = p.result(p.fn.build([]), 'base', budget)
    out, pending = [], 0
    for label, text, metas in vs:
        r = p.result(text, label, budget)
        if r is None:
            pending += 1
        path = p.save(label, text, r) if (r and 'error' not in r and keep) else ''
        out.append((label, text, metas, r))
        say(line(label, r, base, path))
    return base, out, pending


def margins(base, res):
    """Use-titration summary lines: per (VAR, place, form) the smallest K that moves it."""
    groups = {}
    for label, text, metas, r in res:
        if len(metas) == 1 and metas[0]['kind'] == 'use' and r and 'error' not in r:
            m = metas[0]
            groups.setdefault((m['var'], m['place'], m['form'], m.get('target')), []).append((m['k'], r))
    out = []
    for (var, place, form, target), rows in sorted(groups.items(), key=lambda kv: str(kv[0])):
        rows.sort(key=lambda kr: kr[0])
        homes = [(k, home(r)) for k, r in rows]
        trail = ' '.join(f'+{k}:{h or "mem"}' for k, h in homes)
        depth = place[1:] if re.fullmatch(r'd\d+', place) else place
        msg = f'  {var} @{place} ({form}): {trail}'
        if target:
            k = next((k for k, h in homes if h == target), None)
            msg += (f'  => {var} needs +{k} use(s) at depth {depth} to win {target}' if k else
                    f'  => {var} does not win {target} with up to +{homes[-1][0]} uses at depth {depth}')
        else:
            k = next((k for k, h in homes if h != homes[0][1]), None)
            if k:
                msg += f'  => {var} moves {homes[0][1]} -> {dict(homes)[k]} at +{k} use(s) at depth {depth}'
        best = min(rows, key=lambda kr: (kr[1]['net']['dist'], kr[0]))
        ok = [k for k, r in rows if base and reached(base, r)]
        msg += f'; first site reached at +{ok[0]}' if ok else '; first site never reached'
        msg += f'; best net dist {best[1]["net"]["dist"]} at +{best[0]} (base {base["net"]["dist"]})'
        out.append(msg)
    return out


def auto(p, budget, say=print, kmax=4, maxdepth=2, limit=14):
    """Rival pairs from the register map -> locate variables -> titrate both rivals."""
    fn = p.fn
    base = p.result(fn.build([]), 'base', budget)
    if base is None or 'error' in base:
        say('base: ' + (base or {}).get('error', 'pending'))
        return 1
    rmap = base['net']['rmap']
    say(f'base net dist {base["net"]["dist"]} first +{base["net"]["first"]:#x} rd {base["net"]["rd"]}   registers (retail->ours): {fmt_map(rmap)}')
    pairs = []
    for r, d in rmap.items():
        o = max(d, key=d.get)
        if (r in SAVED and o in SAVED) or ((r in SAVED or o in SAVED) and d[o] >= 2):
            pairs.append((r, o, d[o]))
    if not pairs:
        say('no callee-saved rival pair in the register map (scratch-only or semantic gap): use a spec with `scratch` lines')
        return 0
    demote = {o for r, o, _ in pairs if r not in SAVED}  # ours keeps a value callee-saved that retail holds in a scratch register
    say('rival pairs (retail wants <- ours has): ' + '  '.join(f'{r}<-{o} (x{n})' for r, o, n in sorted(pairs, key=lambda t: -t[2])))
    want = {o: r for r, o, _ in pairs}                   # ours register -> retail register of the same value
    involved = set(want) | set(want.values())
    skip = fn.aliases()
    cands = (['this'] if fn.member else []) + [v for v, t in fn.types.items()
                                               if t and (t.rstrip().endswith(('*', '&')) or INT_TYPE.match(t.strip()))]
    cands = [v for v in cands if v not in skip][:limit]
    # 1. locate: +1 use next to the variable's first use (its shallowest depth)
    loc = []
    for v in cands:
        d = fn.depths(v)[0]
        loc += fam_use(fn, v, [1], f'd{d}')
    vs, bad = variants(fn, [[[f]] for f in loc])
    say(f'-- locate ({len(vs)} variables, +1 use each)')
    _, res, pending = run(p, vs, budget, say)
    if pending:
        return 3
    where = {metas[0]['var']: home(r) for _, _, metas, r in res if r and 'error' not in r}
    say('homes under a +1 probe: ' + '  '.join(f'{v}={h or "mem"}' for v, h in where.items()))
    rivals = [v for v, h in where.items() if h in involved or (demote and h in SAVED)]
    if not rivals:
        say('no probed variable lives in a rival register (the rivals are temporaries): name expressions in a spec')
        return 0
    groups = []
    for v in rivals:
        tgt = want.get(where[v])
        tgt = tgt if tgt in SAVED else None
        for d in [d for d in fn.depths(v) if d <= maxdepth]:
            groups.append([fam_use(fn, v, list(range(1, kmax + 1)), f'd{d}', 'store', tgt)])
    for v in rivals:
        if where[v] in want.values() or where[v] in demote:   # the incumbent of a register retail gives to another value
            groups.append([fam_unuse(fn, v)])
    vs, bad = variants(fn, groups)
    say(f'-- titration ({len(vs)} variants; rivals: ' + ', '.join(f'{v}@{where[v]}' + (f'->{want[where[v]]}' if where[v] in want else '') for v in rivals) + ')')
    for b in bad:
        say('  skipped ' + b)
    _, res, pending = run(p, vs, budget, say)
    if pending:
        return 3
    summary(p, base, res, say)
    return 0


def summary(p, base, res, say=print):
    ms = margins(base, res)
    if ms:
        say('== margins')
        for m in ms:
            say(m)
    good = [(l, r) for l, _, _, r in res if r and 'error' not in r]
    hits = [(l, r) for l, r in good if r['v'].get('match') or r['net']['dist'] == 0]
    say('== MATCH / NET-MATCH: ' + (', '.join(('MATCH ' if r['v'].get('match') else 'NET-MATCH ') + l for l, r in hits) or 'none'))
    if good:
        good.sort(key=lambda lr: (lr[1]['net']['dist'], lr[1]['net']['rd']))
        say('== closest (net dist, rd): ' + '; '.join(f'{l} [{r["net"]["dist"]}, {r["net"]["rd"]}]' for l, r in good[:5]))


def main(a):
    p = Prober(a.va, a.files[0] if a.files else None, a.sym, a.out, a.fn)
    say = print
    say(f'{a.va:#x} probe on {os.path.relpath(p.path)}{" in " + a.fn if a.fn else ""} (body lines {p.fn.line0}..{p.fn.line0 + len(p.fn.m.lines) - 1}, '
        f'loop depths {sorted(set(p.fn.loop))})')
    if a.auto:
        code = auto(p, a.budget, say)
    else:
        spec = project.read(a.spec) if os.path.exists(a.spec) else a.spec.replace(';;', '\n')
        vs, bad = variants(p.fn, parse(p.fn, spec))
        for b in bad:
            say('  skipped ' + b)
        base, res, pending = run(p, vs, a.budget, say)
        if base and 'error' not in base:
            say(line('base', base, None))
        code = 3 if pending else 0
        if not pending:
            summary(p, base, res, say)
    if code == 3:
        say(f'PENDING: compile budget ({a.budget}) used up; run the same command again (results are cached)')
    return code
