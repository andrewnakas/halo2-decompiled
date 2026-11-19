"""Index of every MATCHED function's retail code (no compiles): the project's
own source<->code pairs, queried by instruction sequence.

find(seq)      matched functions containing the masked sequence contiguously
nearest(va)    matched functions sharing the most rare 3-grams with va
Sequences are instruction texts at level 'stack' (registers, slots and labels
masked; offsets and constants kept), or 'loose' with every number masked."""
import collections, math, os, pickle
from . import asm, project

N = 3
_cache = {}


def load(level='stack'):
    if level in _cache:
        return _cache[level]
    path = os.path.join(project.OUT, f'corpus_{level}.pkl')
    csvp = os.path.join(project.ROOT, 'config', 'functions.csv')
    if os.path.exists(path) and os.path.getmtime(path) > os.path.getmtime(csvp):
        _cache[level] = pickle.load(open(path, 'rb'))
        return _cache[level]
    seqs, grams = {}, collections.defaultdict(list)
    for va, r in project.rows().items():
        if r['status'] != 'matched' or not r['source']:
            continue
        s = [asm.mask(i.text, level) for i in asm.decode(project.retail(va), va)]
        seqs[va] = s
        for k in range(len(s) - N + 1):
            grams[tuple(s[k:k + N])].append((va, k))
    os.makedirs(project.OUT, exist_ok=True)
    pickle.dump((seqs, dict(grams)), open(path, 'wb'))
    _cache[level] = (seqs, dict(grams))
    return _cache[level]


def find(seq, level='stack', limit=30):
    """[(va, position)] of matched functions containing seq (already masked)."""
    seqs, grams = load(level)
    if len(seq) < N:
        return []
    keys = [tuple(seq[k:k + N]) for k in range(len(seq) - N + 1)]
    k0 = min(range(len(keys)), key=lambda k: len(grams.get(keys[k], ())))
    out = []
    for va, pos in grams.get(keys[k0], ()):
        start = pos - k0
        if start >= 0 and seqs[va][start:start + len(seq)] == seq:
            out.append((va, start))
    out.sort(key=lambda h: len(seqs[h[0]]))
    return out[:limit]


def rarity(seq, level='stack'):
    """How many matched functions contain each 3-gram of seq (0 = retail idiom seen nowhere)."""
    _, grams = load(level)
    return [len({v for v, _ in grams.get(tuple(seq[k:k + N]), ())}) for k in range(len(seq) - N + 1)]


def nearest(va, level='stack', limit=8, owner=None):
    seqs, grams = load(level)
    total = len(seqs)
    s = [asm.mask(i.text, level) for i in asm.decode(project.retail(va), va)]
    score = collections.Counter()
    for g in {tuple(s[k:k + N]) for k in range(len(s) - N + 1)}:
        hits = {v for v, _ in grams.get(g, ())}
        if not hits or len(hits) > total // 20:
            continue
        w = math.log(total / len(hits))
        for v in hits:
            score[v] += w
    rows = project.rows()
    out = [(sc / math.sqrt(len(seqs[v]) + 20), v) for v, sc in score.items()
           if v != va and (not owner or rows[v]['owner'].startswith(owner))]
    return sorted(out, reverse=True)[:limit]


def exemplars(d, level='stack', context=1, limit=5):
    """For each divergent region of diagnosis d: matched functions whose retail
    code contains retail's instructions of that region (with a little context)."""
    x, out = d['x'], []
    for op, i1, i2, j1, j2 in d['ops']:
        if op == 'equal' or i2 == i1:
            continue
        lo, hi = max(0, i1 - context), min(len(x), i2 + context)
        while hi - lo < N and (lo > 0 or hi < len(x)):
            lo, hi = max(0, lo - 1), min(len(x), hi + 1)
        seq = [asm.mask(i.text, level) for i in x[lo:hi]]
        hits = find(seq, level, limit)
        if not hits and hi - lo > i2 - i1 >= N:
            seq = [asm.mask(i.text, level) for i in x[i1:i2]]
            hits = find(seq, level, limit)
        out.append(dict(r0=x[i1].off, seq=seq, hits=hits))
    return out
