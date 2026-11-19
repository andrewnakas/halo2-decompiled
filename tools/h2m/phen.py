"""h2m phen: phenomenon reducer.

1. Find a retail instruction pattern in EVERY matched function of
   config/functions.csv (Havok and game; retail bytes from the XBE, no compile;
   cache in <H2M_OUT>/corpus/).  Same owner first, then smallest function.
2. --reduce N: for the best N hits that compile standalone (not /GL game code)
   shrink the matched function's source (statements, blocks, lines, locals)
   while the compiled function still contains the pattern, and print the
   minimal source that shows the phenomenon.  State is kept on disk; a call
   spends at most --budget compiles, rerun until DONE.

Pattern: instructions separated by `;`, in the disassembler's spelling, or
--at VA:START-END / VA:START+COUNT (hex retail offsets of any function).
  ?      any one instruction          *   any text inside an instruction
Levels: canon  registers bound consistently (ebx in the pattern = one register
               everywhere, different pattern registers = different registers),
               frame slots and labels masked, constants kept          (default)
        loose  canon + every number masked
        stack  registers free, frame slots masked     regs  registers free
        exact  the text as written"""
import array, collections, hashlib, json, os, pickle, re
from . import asm, mutators, project, scorer

CORPUS = os.path.join(project.OUT, 'corpus')
R32 = 'eax|ebx|ecx|edx|esi|edi|ebp'
R16 = 'ax|bx|cx|dx|si|di|bp'
R8 = '[abcd][lh]'
TOK = re.compile(r'\b(%s|%s|%s)\b' % (R32, R16, R8))
WILD = re.compile(r'\*(?!\d)')                           # `*` is a wildcard unless it is a scale (eax*2)
FAM = {'al': 'eax', 'ah': 'eax', 'ax': 'eax', 'bl': 'ebx', 'bh': 'ebx', 'bx': 'ebx', 'cl': 'ecx', 'ch': 'ecx',
       'cx': 'ecx', 'dl': 'edx', 'dh': 'edx', 'dx': 'edx', 'si': 'esi', 'di': 'edi', 'bp': 'ebp'}


# ----------------------------------------------------------------- corpus

def corpus(rebuild=False):
    """({va: (offsets array, [texts])}, {loose-masked text: array of vas}) of all matched functions."""
    os.makedirs(CORPUS, exist_ok=True)
    path = os.path.join(CORPUS, 'all_matched_v1.pkl')
    csvp = os.path.join(project.ROOT, 'config', 'functions.csv')
    if not rebuild and os.path.exists(path) and os.path.getmtime(path) > os.path.getmtime(csvp):
        return pickle.load(open(path, 'rb'))
    funcs, index = {}, collections.defaultdict(set)
    for va, r in project.rows().items():
        if r['status'] != 'matched':
            continue
        try:
            ins = asm.decode(project.retail(va), va)
        except Exception:  # noqa: BLE001
            continue
        funcs[va] = (array.array('I', [i.off for i in ins]), [i.text for i in ins])
        for i in ins:
            index[asm.mask(i.text, 'loose')].add(va)
    index = {k: array.array('I', sorted(v)) for k, v in index.items()}
    tmp = path + f'.{os.getpid()}'
    pickle.dump((funcs, index), open(tmp, 'wb'), protocol=4)
    os.replace(tmp, path)
    return funcs, index


# ----------------------------------------------------------------- pattern

def _prep(text, level):
    t = text
    if level in ('canon', 'loose', 'stack'):
        t = asm.STK.sub('[S]', t)
    if level != 'exact':
        t = asm.LBL.sub('L', t)
    if level == 'loose':
        t = asm.NUM.sub('N', t)
    return t


class Pattern:
    def __init__(self, items, level='canon'):
        self.level = level
        self.items = [i.strip() for i in items if i.strip()]
        self.rx, self.regs, self.keys = [], [], []
        for it in self.items:
            if it == '?':
                self.rx.append(None)
                self.regs.append([])
                self.keys.append(None)
                continue
            t = _prep(it, level)
            regs, out, pos = [], '', 0
            for m in (TOK.finditer(t) if level != 'exact' else ()):
                out += self._lit(t[pos:m.start()])
                g = m.group(1)
                out += '(%s)' % (R32 if len(g) == 3 else R8 if g[1] in 'lh' else R16)
                regs.append(FAM.get(g, g))
                pos = m.end()
            out += self._lit(t[pos:])
            self.rx.append(re.compile(out + r'\Z'))
            self.regs.append(regs)
            self.keys.append(None if WILD.search(it) else asm.mask(it, 'loose'))
        self.bind = level in ('canon', 'loose')
        if not any(r is not None for r in self.rx):
            raise SystemExit('pattern needs at least one concrete instruction')

    @staticmethod
    def _lit(s):
        return '.*?'.join(re.escape(p) for p in WILD.split(s))

    def at(self, texts, k):
        """Whether the pattern matches texts[k:k+len]."""
        if k + len(self.rx) > len(texts):
            return False
        fwd, back = {}, {}
        for rx, regs, t in zip(self.rx, self.regs, texts[k:k + len(self.rx)]):
            if rx is None:
                continue
            m = rx.match(_prep(t, self.level))
            if not m:
                return False
            if self.bind:
                for p, g in zip(regs, m.groups()):
                    g = FAM.get(g, g)
                    if fwd.setdefault(p, g) != g or back.setdefault(g, p) != p:
                        return False
        return True

    def find(self, texts):
        first = next(k for k, r in enumerate(self.rx) if r is not None)
        rx = self.rx[first]
        out = []
        for k in range(first, len(texts)):
            if rx.match(_prep(texts[k], self.level)) and self.at(texts, k - first):
                out.append(k - first)
        return out

    def candidates(self, funcs, index):
        sets = [set(index.get(k, ())) for k in self.keys if k is not None]
        if not sets:
            return list(funcs)
        sets.sort(key=len)
        s = sets[0]
        for o in sets[1:]:
            s &= o
        return sorted(s)

    def __str__(self):
        return ' ; '.join(self.items)


def pattern_from(a):
    if a.at:
        m = re.fullmatch(r'(?:0x)?([0-9a-fA-F]+):(?:0x)?([0-9a-fA-F]+)(?:([-+])(?:0x)?([0-9a-fA-F]+))?', a.at)
        if not m:
            raise SystemExit('--at VA:START-END or VA:START+COUNT (hex)')
        va, start = int(m.group(1), 16), int(m.group(2), 16)
        x = asm.decode(project.retail(va), va)
        if m.group(3) == '+':
            k = next(k for k, i in enumerate(x) if i.off >= start)
            sel = x[k:k + int(m.group(4), 16)]
        else:
            end = int(m.group(4), 16) if m.group(4) else start + 1
            sel = [i for i in x if start <= i.off < end]
        return [i.text for i in sel], va
    if not a.pattern:
        raise SystemExit('give a pattern or --at')
    return [p for p in a.pattern.split(';')], None


# ----------------------------------------------------------------- search

def search(pat, owner, limit=None):
    funcs, index = corpus()
    rows = project.rows()
    hits = []
    for va in pat.candidates(funcs, index):
        offs, texts = funcs[va]
        pos = pat.find(texts)
        if pos:
            r = rows[va]
            hits.append(dict(va=va, owner=r['owner'], size=int(r['size']), source=r['source'], n=len(pos),
                             off=offs[pos[0]], k=pos[0], insns=len(texts)))
    hits.sort(key=lambda h: (not h['owner'].startswith(owner), h['size']))
    return hits, len(funcs)


def relax(items, level, owner, show=3):
    """No hit: say which weaker forms of the pattern do occur (looser level, shorter windows)."""
    items = [i for i in items if i.strip()]
    forms = []
    if level not in ('loose',):
        forms.append(('level loose', items, 'loose'))
    for n in range(len(items) - 1, 0, -1):
        for k in range(len(items) - n + 1):
            forms.append((f'instructions {k + 1}..{k + n} only, level loose', items[k:k + n], 'loose'))
    print('relaxations (the phenomenon as given occurs in no matched function):')
    shown = 0
    for label, its, lv in forms[:12]:
        try:
            pat = Pattern(its, lv)
        except SystemExit:
            continue
        hits, _ = search(pat, owner)
        print(f'  {label}: {pat}  -> {len(hits)} functions')
        if hits and shown < 2:
            shown += 1
            for h in hits[:show]:
                print(f'      {h["va"]:#x} {h["owner"]:12.12} size {h["size"]:5d}  +{h["off"]:#06x}  {h["source"]}')


def standalone(h):
    return h['owner'] != 'game' and bool(h['source']) and os.path.exists(os.path.join(project.ROOT, h['source']))


# ----------------------------------------------------------------- reducer

DECL = re.compile(r'^\s*(?:[\w:]+\s+)*?([\w:]+(?:\s+const)?[\s\*&]*)\b([A-Za-z_]\w*)\s*=\s*([^;]+);\s*$')
BARE = re.compile(r'^\s*(?:[\w:]+\s+)+?[\s\*&]*([A-Za-z_]\w*)\s*;\s*$')
ASSIGN = re.compile(r'^\s*([A-Za-z_]\w*)\s*=\s*([^;=]+);\s*$')
HEAD = re.compile(r'^\s*(if|for|while|switch|else|do)\b')


def candidates(body):
    """Smaller bodies, biggest cuts first."""
    try:
        m = mutators.Model(body)
    except ValueError:
        return
    lines = m.lines
    spans = []
    for op in m.blocks():
        try:
            spans += m.statements(op)
        except Exception:  # noqa: BLE001
            pass
    spans = sorted(set(spans), key=lambda s: (-(s[1] - s[0]), s[0]))
    for s, e in spans:                                    # 1. delete a statement (compound ones first)
        if e > s:
            yield '\n'.join(lines[:s] + lines[e + 1:])
    for s, e in spans:                                    # 2. unwrap: keep a compound statement's block, drop its head
        if e == s:
            continue
        opens = [i for i in range(s, e + 1) if mutators.code(lines[i]).strip() == '{' and m.depth[i] == m.depth[s]]
        for o in opens:
            c = m.close.get(o)
            if c is None or c > e:
                continue
            inner = [l[1:] if l.startswith('\t') else l for l in lines[o + 1:c]]
            yield '\n'.join(lines[:s] + inner + lines[e + 1:])
            yield '\n'.join(lines[:s] + lines[o:c + 1] + lines[e + 1:])      # as a bare block (keeps scopes)
    for s, e in spans:                                    # 3. delete a one-line statement
        if e == s:
            yield '\n'.join(lines[:s] + lines[s + 1:])
    for i in range(1, len(lines) - 1):                    # 4. any other line (else arms, labels)
        st = mutators.code(lines[i]).strip()
        if st and st not in ('{', '}') and not any(s == i == e for s, e in spans):
            yield '\n'.join(lines[:i] + lines[i + 1:])
    for i in range(len(lines) - 1):                       #    an emptied block
        if mutators.code(lines[i]).strip() == '{' and mutators.code(lines[i + 1]).strip() == '}' and i > 0 \
                and not HEAD.match(lines[m.prev_sig(i)] if m.prev_sig(i) >= 0 else ''):
            yield '\n'.join(lines[:i] + lines[i + 2:])
    for i, l in enumerate(lines):                         # 5. inline a once-assigned local
        mm = DECL.match(l) or ASSIGN.match(l)
        if not mm:
            continue
        name, expr = (mm.group(2), mm.group(3)) if mm.re is DECL else (mm.group(1), mm.group(2))
        if not re.fullmatch(r'[\w\.\[\]\->\s\(\)\*&+]+', expr.strip()):
            continue
        rest = lines[:i] + lines[i + 1:]
        if re.findall(r'\b%s\s*(?:[-+*/|&^]?=[^=]|\+\+|--)' % re.escape(name), '\n'.join(rest)):
            continue
        sub = '(' + expr.strip() + ')' if re.search(r'[^\w]', expr.strip()) else expr.strip()
        new = [re.sub(r'(?<!->)(?<!\.)\b%s\b' % re.escape(name), sub, x)
               if not BARE.match(x) or BARE.match(x).group(1) != name else None for x in rest]
        yield '\n'.join(x for x in new if x is not None)
    for i, l in enumerate(lines):                         # 6. join a split declaration
        mm = BARE.match(l)
        if mm and not HEAD.match(l):
            for j in range(i + 1, len(lines)):
                a = ASSIGN.match(lines[j])
                if a and a.group(1) == mm.group(1):
                    decl = l.rstrip()[:-1].rstrip()
                    yield '\n'.join(lines[:i] + lines[i + 1:j]
                                    + [f'{mutators.indent(lines[j])}{decl.strip()} = {a.group(2).strip()};'] + lines[j + 1:])
                    break
    yield re.sub(r'\((&?[A-Za-z_]\w*)\)', r'\1', body)   # 7. redundant parentheses and casts
    yield re.sub(r'\((?:float|double|long|int)\)\s*', '', body)


class Reducer:
    def __init__(self, va, pat, out):
        self.va, self.pat = va, pat
        self.s = scorer.Scorer(va)
        self.text0 = self.s.base
        self.state_path = os.path.join(out, f'red_{va:x}.json')
        self.out_path = os.path.join(out, f'reduced_{va:x}.cpp')
        try:
            self.st = json.load(open(self.state_path))
        except (OSError, ValueError):
            self.st = dict(body=None, tried={}, done=False, compiles=0, snippet=[], note='', name=None)
        self.used = 0

    def save(self):
        tmp = self.state_path + f'.{os.getpid()}'
        json.dump(self.st, open(tmp, 'w'))
        os.replace(tmp, self.state_path)

    def check(self, body):
        """True/False, or None when the budget is used up."""
        h = hashlib.sha1(body.encode('utf-8', 'replace')).hexdigest()[:16]
        if h in self.st['tried']:
            return self.st['tried'][h]
        if self.used >= self.budget:
            return None
        self.used += 1
        self.st['compiles'] += 1
        ok, snippet = False, []
        try:
            funcs = self.s.compile(project.splice(self.text0, self.va, body))
            name = self.st.get('name')
            if name:
                f = next((f for f in funcs if f[0] == name), None)
            else:
                from . import measure
                f = measure.pick(funcs, self.s.theirs, self.va, None)
                if f:
                    self.st['name'] = f[0]
            if f:
                ins = asm.ours(f[1], f[2])
                texts = [i.text for i in ins]
                pos = self.pat.find(texts)
                if pos:
                    ok = True
                    k = pos[0]
                    snippet = [f'{"> " if k <= q < k + len(self.pat.rx) else "  "}{ins[q].off:04x} {texts[q]}'
                               for q in range(max(0, k - 2), min(len(ins), k + len(self.pat.rx) + 2))]
        except (SystemExit, Exception):  # noqa: BLE001
            ok = False
        self.st['tried'][h] = ok
        if ok:
            self.st['last_snippet'] = snippet
            self.st['last_insns'] = len(texts)
        return ok

    def run(self, budget):
        self.budget = budget
        st = self.st
        if st['done']:
            return True
        if st['body'] is None:
            body = project.body(self.text0, self.va)
            r = self.check(body)
            if r is None:
                return False
            if not r:
                st.update(done=True, note='the repo source compiled alone does not show the pattern in this function (skipped)')
                self.save()
                return True
            st.update(body=body, snippet=st.get('last_snippet', []), insns=st.get('last_insns'))
        changed = True
        while changed:
            changed = False
            for c in candidates(st['body']):
                if c == st['body'] or not c.strip():
                    continue
                r = self.check(c)
                if r is None:
                    self.save()
                    return False
                if r:
                    st.update(body=c, snippet=st.get('last_snippet', []), insns=st.get('last_insns'))
                    changed = True
                    break
        st['done'] = True
        self.save()
        open(self.out_path, 'w', encoding='utf-8').write(project.splice(self.text0, self.va, st['body']))
        return True

    def signature(self):
        s, _ = project.reduced.find_body(self.text0, self.va)
        mk = self.text0.rfind('@retail', 0, s)
        return self.text0[self.text0.find('\n', mk) + 1:s].strip()

    def report(self):
        st = self.st
        if st.get('note'):
            return [f'  {st["note"]}']
        out = [f'  reduced in {st["compiles"]} compiles to {len([l for l in st["body"].split(chr(10)) if l.strip()])} lines '
               f'({st.get("insns")} instructions): {os.path.relpath(self.out_path)}',
               '  ' + self.signature().replace('\n', '\n  ') + st['body'].replace('\n', '\n  ') + '}']
        out += ['  compiled:'] + ['    ' + l for l in st.get('snippet', [])]
        return out


def main(a):
    if a.rebuild:
        corpus(rebuild=True)
    items, at_va = pattern_from(a)
    pat = Pattern(items, a.level)
    rows = project.rows()
    owner = a.owner or (rows[at_va]['owner'] if at_va in rows else 'third:havok')
    name = a.name or hashlib.sha1((a.level + '|' + str(pat)).encode()).hexdigest()[:10]
    hits, total = search(pat, owner)
    print(f'pattern ({a.level}): {pat}')
    same = sum(1 for h in hits if h['owner'].startswith(owner))
    print(f'{len(hits)} of {total} matched functions contain it ({same} with owner {owner}*, '
          f'{sum(1 for h in hits if standalone(h))} standalone)')
    for h in hits[:a.limit]:
        print(f'  {h["va"]:#x} {h["owner"]:12.12} size {h["size"]:5d}  +{h["off"]:#06x} x{h["n"]}  {h["source"]}'
              + ('' if standalone(h) else '   [game /GL: image backend, not reduced]'))
    if len(hits) > a.limit:
        print(f'  ... {len(hits) - a.limit} more (--limit)')
    if not hits:
        relax(items, a.level, owner)
    if not a.reduce and not a.only:
        return 0
    out = os.path.join(project.OUT, 'phen', name)
    os.makedirs(out, exist_ok=True)
    if a.only:
        want = [int(v, 16) for v in a.only.split(',')]
        todo = [h for h in hits if h['va'] in want]
    else:
        todo = [h for h in hits if standalone(h)][:a.reduce]
    left, pending = a.budget, False
    for h in todo:
        if not standalone(h):
            print(f'== {h["va"]:#x}: game /GL function, listed only')
            continue
        r = Reducer(h['va'], pat, out)
        done = r.run(left)
        left -= r.used
        print(f'== {h["va"]:#x} {h["source"]} (size {h["size"]})' + ('' if done else f'  PENDING ({r.st["compiles"]} compiles so far, '
              f'{len([l for l in (r.st["body"] or "").split(chr(10)) if l.strip()])} lines)'))
        if done:
            print('\n'.join(r.report()))
        else:
            pending = True
        if left <= 0 and not done:
            break
    if pending or any(not Reducer(h['va'], pat, out).st['done'] for h in todo if standalone(h)):
        print(f'PENDING: compile budget ({a.budget}) used up; run the same command again (state in {os.path.relpath(out)})')
        return 3
    print('DONE')
    return 0
