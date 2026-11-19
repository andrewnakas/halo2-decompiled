"""Structural beam search over one function body (`h2m search VA`).

Candidates come from h2m.mutators (the techniques that matched functions, as
enumerations, plus the upstream permuter's clean mutations); each is scored
through scorer.Scorer.text (ledger-cached, origin = 'search:<mutator>', parent
= the parent's text hash), so the ledger learns which mutators pay off.
Beam: the N best states by metrics.key (match, dist, sem, lock); ties keep
whichever came first, so neutral moves are kept as stepping stones.
Every improvement is ablated back toward the starting body (each changed hunk
reverted alone, kept reverted when the result is no worse) and written to
<H2M_OUT>/<va>/search/best_NNNN.cpp (or flag_NNNN.cpp when it adds a one-use
local and is not a MATCH), with a line in search/log.txt."""
WEDGE = 45  # seconds a batch of compiles may take

import collections, concurrent.futures, difflib, itertools, os, random, re, time
from . import classify, ledger, metrics, mutators, project, scorer


class State:
    __slots__ = ('body', 'text', 'hash', 'v', 'key', 'flags', 'path', 'order', 'expanded', 'pending', 'exhausted')

    def __init__(self, body, text, v, flags, path, order):
        self.body, self.text, self.v = body, text, v
        self.hash = ledger.digest(text)
        self.key = metrics.key(v)
        self.flags, self.path, self.order = flags, path, order
        self.expanded, self.pending, self.exhausted = 0, None, False


def fmt(v):
    if 'error' in v:
        return 'ERROR'
    if v.get('match'):
        return 'MATCH'
    return f"lock {v['lock']} dist {v['dist']} sem {v['sem']}"


def ledger_rates():
    """origin -> (tries, improvements over the parent) across every function."""
    q = '''select c.origin, count(*), sum(case when c.ok=1 and p.ok=1 and (c.match > p.match or c.dist < p.dist
           or (c.dist = p.dist and c.sem < p.sem) or (c.dist = p.dist and c.sem = p.sem and c.lock < p.lock))
           then 1 else 0 end) from variant c join variant p on p.va = c.va and p.hash = c.parent
           where c.origin like 'search:%' group by c.origin'''
    with ledger._lock:
        rows = ledger.db().execute(q).fetchall()
    return {o[len('search:'):]: (n, s or 0) for o, n, s in rows}


class Search:
    def __init__(self, va, start=None, seconds=1200, jobs=2, beam=6, seed=0, only=None, log=print):
        self.va, self.seconds, self.jobs, self.width = va, seconds, max(1, min(jobs, 2)), beam
        import build  # a compile takes well under a second: a Wine wedge must not eat 10 minutes
        if not os.environ.get('HALO2_TOOL_TIMEOUT'):
            build.TOOL_TIMEOUT = min(build.TOOL_TIMEOUT, 30)
        self.rng = random.Random(seed)
        self.say = log
        self.text0 = project.read(start or project.source_path(va))
        self.body0 = project.body(self.text0, va)
        self.scorer = scorer.Scorer(va, base=start, origin='search')
        self.cx = mutators.Ctx(va, self.text0, seed)
        self.mutators = [m for m in mutators.ALL if not only or m.__name__ in only]
        self.out = os.path.join(project.workdir(va), 'search')
        os.makedirs(self.out, exist_ok=True)
        self.logf = open(os.path.join(self.out, 'log.txt'), 'a', encoding='utf-8')
        self.pool, self.seen = {}, set()
        self.counter = itertools.count()
        self.tries = collections.Counter()
        self.wins = collections.Counter()
        self.errors = collections.Counter()
        self.compiles = 0
        self.wedged = 0
        self.executor = concurrent.futures.ThreadPoolExecutor(self.jobs)
        self.written = 0

    # ---------------------------------------------------------------- text
    def full(self, body):
        text = project.splice(self.text0, self.va, body)
        need = [n for n in mutators.HELPERS if f'{n}_{self.va:x}(' in body and f'{n}_{self.va:x}(' not in self.text0]
        if not need:
            return text
        m = re.search(r'^[ \t]*//[ \t]*@retail[ \t]+0x0*%x\b' % self.va, text, re.M | re.I)
        at = m.start()
        while True:  # above the comment block that introduces the function
            prev = text.rfind('\n', 0, at - 1) + 1
            if at == 0 or not text[prev:at].lstrip().startswith('//'):
                break
            at = prev
        helpers = ''.join(mutators.HELPERS[n].format(va=f'{self.va:x}') + '\n' for n in need)
        return text[:at] + helpers + text[at:]

    def log(self, msg):
        self.say(msg)
        self.logf.write(time.strftime('%H:%M:%S ') + msg + '\n')
        self.logf.flush()

    # ---------------------------------------------------------------- weights
    def weights(self):
        try:
            s = self.scorer
            rx, ry, f = self.executor.submit(s.listing, self.text0).result(timeout=WEDGE)
            d = classify.diagnose(s.theirs, self.va, f[1], f[2])
            classes, hints = d['classes'], d['hints']
        except Exception as e:  # noqa: BLE001
            classes, hints = [], []
            self.log(f'diagnosis failed: {str(e)[-200:]}')
        rates = ledger_rates()
        n_all = sum(n for n, _ in rates.values())
        p = (sum(k for _, k in rates.values()) + 1) / (n_all + 50)   # overall improvement rate
        w = {}
        for m in self.mutators:
            n = m.__name__
            base = 0.5 if n.startswith('up_') else 1.0
            for c in classes:
                base += mutators.CLASS_WEIGHTS.get(c, {}).get(n, 1) - 1
            for text, table in mutators.HINT_WEIGHTS:
                if any(text in h for h in hints):
                    base *= table.get(n, 1)
            tries, succ = rates.get(n, (0, 0))
            factor = min(3.0, max(0.5, (succ + 20 * p) / (tries + 20) / p))   # shrunk toward the mean
            w[n] = max(0.0, base) * factor
        self.classes, self.hints = classes, hints
        return w

    # ---------------------------------------------------------------- scoring
    def score(self, items):
        """items: [(body, parent State, mutator, note)] -> [(item, text, v)].  A compile
        that hangs (Wine wedges cl.exe and build.run_tool can block on its pipes) is
        abandoned after WEDGE seconds: scored as an error, not cached, the pool replaced."""
        def one(it):
            body, parent, mut, note = it
            text = self.full(body)
            return text, self.scorer.text(text, parent=parent.hash if parent else None,
                                          origin=f'search:{mut}', note=note[:300])
        futs = [self.executor.submit(one, it) for it in items]
        done, stuck = concurrent.futures.wait(futs, timeout=WEDGE)
        if stuck:
            self.wedged += len(stuck)
            self.log(f'   {len(stuck)} compile(s) wedged; abandoned (thread left behind)')
            self.executor.shutdown(wait=False, cancel_futures=True)
            self.executor = concurrent.futures.ThreadPoolExecutor(self.jobs)
        self.compiles += len(items)
        out = []
        for it, f in zip(items, futs):
            if f in done and f.exception() is None:
                text, v = f.result()
            else:
                text, v = self.full(it[0]), dict(error='wedged or crashed: ' + repr(f.exception() if f in done else 'timeout'))
            out.append((it, text, v))
        return out

    def admit(self, body, text, v, parent, note):
        _, flags = mutators.policy(body, self.body0)
        st = State(body, text, v, flags, (parent.path if parent else []) + ([note] if note else []), next(self.counter))
        self.pool.setdefault(st.hash, st)
        return self.pool[st.hash]

    # ---------------------------------------------------------------- neighbours
    def neighbours(self, st):
        if st.pending is None:
            st.pending = {}
            for m in self.mutators:
                if self.w.get(m.__name__, 0) <= 0:
                    continue
                try:
                    cands = m(st.body, self.cx)
                except Exception:  # noqa: BLE001  (text the model cannot read)
                    cands = []
                keep = []
                for body, note, _ in cands:
                    if body == st.body:
                        continue
                    reason, _ = mutators.policy(body, self.body0)
                    if reason:
                        continue
                    keep.append((body, note))
                self.rng.shuffle(keep)
                if keep:
                    st.pending[m.__name__] = keep
        return st.pending

    def draw(self, st):
        pend = self.neighbours(st)
        while pend:
            names = list(pend)
            # diminishing returns: a mutator that keeps producing neutral moves gives way
            name = self.rng.choices(names, [self.w[n] / (1 + self.tries[n] / 300) ** 0.5 for n in names])[0]
            body, note = pend[name].pop()
            if not pend[name]:
                del pend[name]
            h = ledger.digest(self.full(body))
            if h in self.seen:
                continue
            self.seen.add(h)
            return body, name, note
        st.exhausted = True
        return None

    def beam(self):
        live = sorted(self.pool.values(), key=lambda s: (s.key, len(s.flags), len(s.path), s.order))
        return [s for s in live if not s.exhausted][:self.width]

    # ---------------------------------------------------------------- ablation
    def ablate(self, st):
        """Revert hunks of st against the start, kept reverted when no worse:
        first the groups of hunks that share a name the start lacks (a local and
        its uses), then each hunk alone; twice over."""
        a, best = self.body0.split('\n'), st
        names0 = set(re.findall(r'[A-Za-z_]\w*', self.body0))
        for _ in range(2):
            changed = False
            b = best.body.split('\n')
            ops = [o for o in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes() if o[0] != 'equal']
            if len(ops) <= 1:
                return best
            groups = collections.defaultdict(set)
            for k, o in enumerate(ops):
                for n in set(re.findall(r'[A-Za-z_]\w*', '\n'.join(b[o[3]:o[4]]))) - names0:
                    groups[n].add(k)
            trials = sorted({frozenset(g) for g in groups.values() if 1 < len(g) < len(ops)}, key=len, reverse=True)
            trials += [frozenset([k]) for k in range(len(ops))][:24]
            for group in trials:
                spans = {ops[k][1:3] for k in group}
                b = best.body.split('\n')
                cur = [o for o in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes() if o[0] != 'equal']
                sel = [o for o in cur if o[1:3] in spans]
                if len(sel) != len(spans) or len(cur) <= len(sel):
                    continue
                for _, i1, i2, j1, j2 in sorted(sel, key=lambda o: -o[3]):
                    b = b[:j1] + a[i1:i2] + b[j2:]
                body = '\n'.join(b)
                if mutators.policy(body, self.body0)[0]:
                    continue
                self.seen.add(ledger.digest(self.full(body)))
                what = ', '.join(f'{i1}-{i2}' for _, i1, i2, _, _ in sel)
                [(_, text, v)] = self.score([(body, best, 'ablate', f'revert start lines {what}')])
                if 'error' in v or metrics.key(v) > best.key:
                    continue
                best = self.admit(body, text, v, best, f'ablated: start lines {what} restored')
                changed = True
            if not changed:
                break
        return best

    # ---------------------------------------------------------------- output
    def write(self, st, start, kind=None):
        clean = not st.flags or st.v.get('match')
        self.written += 1
        name = f'{kind or ("best" if clean else "flag")}_{self.written:04d}.cpp'
        path = os.path.join(self.out, name)
        open(path, 'w', encoding='utf-8').write(st.text)
        diff = [l.rstrip('\n') for l in difflib.unified_diff(self.body0.splitlines(True), st.body.splitlines(True), n=0)
                if not l.startswith(('---', '+++'))]
        self.log(f'{name}: {fmt(st.v)} (start {fmt(start.v)}) after {self.compiles} candidates'
                 + (f'  FLAGS {"; ".join(st.flags)}' if st.flags else ''))
        for p in st.path:
            self.log(f'    step: {p}')
        for l in diff[:40]:
            self.log(f'    {l}')
        return path

    # ---------------------------------------------------------------- main loop
    def run(self):
        t0 = time.time()
        deadline = t0 + self.seconds
        [(_, text, v)] = self.score([(self.body0, None, 'start', 'start')])
        if 'error' in v:
            self.log(f'{self.va:#x}: the start does not compile: {v["error"][-400:]}')
            return None
        start = self.admit(self.body0, text, v, None, '')
        self.w = self.weights()
        self.log(f'== search {self.va:#x} start {fmt(v)} classes {" ".join(self.classes)}; '
                 f'{len(self.mutators)} mutators, beam {self.width}, jobs {self.jobs}, {self.seconds:.0f}s')
        top = sorted(self.w.items(), key=lambda kv: -kv[1])[:8]
        self.log('   weights: ' + ' '.join(f'{n}={x:.2f}' for n, x in top))
        best = flagged = lowest = start
        self.seen.add(start.hash)
        while time.time() < deadline and not best.v.get('match'):
            beam = self.beam()
            if not beam:
                self.log('   neighbourhood exhausted')
                break
            items = []
            ranks = [1.0 / (1 + r) / (1 + s.expanded / 40) for r, s in enumerate(beam)]
            for _ in range(self.jobs * 3):
                for _ in range(len(beam) + 2):
                    live = [(s, w) for s, w in zip(beam, ranks) if not s.exhausted]
                    if not live:
                        break
                    st = self.rng.choices([s for s, _ in live], [w for _, w in live])[0]
                    got = self.draw(st)
                    if got:
                        st.expanded += 1
                        items.append((got[0], st, got[1], got[2]))
                        break
            if not items:
                continue
            for (body, parent, mut, note), text, v in self.score(items):
                self.tries[mut] += 1
                if 'error' in v:
                    self.errors[mut] += 1
                    continue
                st = self.admit(body, text, v, parent, f'{mut}: {note}')
                if st.key < parent.key:
                    self.wins[mut] += 1
                if not st.flags and st.v['lock'] < lowest.v['lock'] and st.key >= best.key:
                    lowest = st     # the upstream score reviewers see, even when dist is not the best
                    self.write(st, start, 'lock')
                if st.key < best.key and st.key < flagged.key or (not st.flags and st.key < best.key):
                    a = self.ablate(st)
                    if not a.flags or a.v.get('match'):
                        if a.key < best.key:
                            best = a
                            if a.v.get('lock', 0) < lowest.v['lock']:
                                lowest = a
                            self.write(a, start)
                    elif a.key < flagged.key and a.key < best.key:
                        flagged = a
                        self.write(a, start)
        self.log(f'== done {self.va:#x}: start {fmt(start.v)} -> best {fmt(best.v)} in {time.time() - t0:.0f}s, '
                 f'{self.compiles} candidates scored (ledger hits included), {len(self.pool)} distinct states that compiled')
        self.log('   per mutator (tried/compile errors/improved the parent): ' + ' '.join(
            f'{n}={self.tries[n]}/{self.errors[n]}/{self.wins[n]}' for n in sorted(self.tries, key=lambda n: -self.tries[n])))
        if best is not start:
            try:
                f = self.executor.submit(lambda: (self.scorer.siblings(best.text), self.scorer.siblings(start.text)))
                sib, was = f.result(timeout=WEDGE)
                lost = [f'{k:#x}' for k, ok in was.items() if ok and not sib.get(k)]
                self.log('   siblings: ' + ('LOST MATCH ' + ' '.join(lost) if lost else 'no matched sibling lost'))
            except Exception as e:  # noqa: BLE001
                self.log(f'   siblings check failed: {str(e)[-200:]}')
        return best
