#!/usr/bin/env python
"""h2m: matching workbench.  Run from the repository root: python tools/h2m.py CMD ...
(docs/H2M_WORKBENCH.md has one example per command; VA is hex, with or without 0x)

  score VA [FILE...]        metric vector of the tree's source, or of whole-file variants
  diff VA [FILE]            diagnosis + aligned side-by-side
  triage [VA...] [--jobs N] classify every open Havok function (or the given VAs)
  try VA VARIANTS.txt       function-body variants separated by a line of =====
  exemplars VA [FILE]       matched functions containing retail's idiom at each divergent region
  idiom VA [FILE]           each retail-only instruction looked up in matched code of the same owner
  asm FILE... [--sym S]     compile any source and print each function's code (lab experiments)
  history VA                best variants recorded in the ledger
  search VA [--start F] [--time S] [--jobs 2] [--beam N] [--only m1,m2] [--seed S]
                            structural beam search with the matching techniques (h2m/search.py)
  probe VA [FILE] --spec S | --auto   diagnostic perturbations: use-titration, scratch shifts, ... (h2m/probe.py)
  phen PATTERN | --at VA:A-B [--reduce N]   matched functions showing a retail pattern, source reduced (h2m/phen.py)
  webs VA [FILE]            per register/stack web: retail's location vs ours, swaps grouped (h2m/webs.py)
  retail VA [LO HI]         raw retail listing, no masking, float immediates / float constants decoded
  scratch [show|rm]         the image backend's private build root (<H2M_OUT>/scratch/<id>)
  fastcheck VA... [--tree DIR]   tools/check.py --fast for the VAs (through h2m/run_check.py), one run at a time per tree

Backends (--backend auto|object|image; auto = by the function's owner in functions.csv):
  object  one /GL- source compiled alone (Havok)          image  LTCG game functions on a reduced
  image built in a scratch root (h2m/image.py; --scratch ID or H2M_SCRATCH)

Environment: RETAIL_XBE, XDK_DIR as for tools/check.py; H2M_ROOT (tree to work on, default this repository),
H2M_OUT (work files, default <tree>/build/h2m), H2M_DB (ledger, default <H2M_OUT>/h2m.db), H2M_TU, H2M_SCRATCH.
"""
import argparse, concurrent.futures, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from h2m import classify, corpus, ledger, metrics, project, scorer  # noqa: E402


def fmt(v):
    if 'error' in v:
        return 'ERROR ' + v['error'].strip().splitlines()[-1][:120]
    if v['match']:
        return 'MATCH'
    return f"lock {v['lock']:4d}  dist {v['dist']:4d}  sem {v['sem']:3d}  frame {v['frame']:+d}  first +{v['first']:#x}"


def new_scorer(va, sym=None, base=None, a=None):
    """The scorer of the function's backend: scorer.Scorer (object) unless the
    function is a game function or --backend image asks for the reduced image."""
    backend = getattr(a, 'backend', None) or 'auto'
    row = project.rows().get(va)
    if backend == 'object' or (backend == 'auto' and (row is None or row['owner'] != 'game')):
        return scorer.Scorer(va, sym, base)
    from h2m import image
    s = image.ImageScorer(va, sym, base, scratch=getattr(a, 'scratch', None), extra=getattr(a, 'extra', None))
    if s.extra:
        print('[img] extra functions in the reduced image: ' + ','.join(f'{v:x}' for v in sorted(s.extra)), file=sys.stderr)
    return s


def cmd_score(a):
    s = new_scorer(a.va, a.sym, a=a)
    for f in a.files or [s.path]:
        print(f'{fmt(s.text(project.read(f), origin="file", note=os.path.basename(f)))}  {os.path.relpath(f)}')


def report(va, sym=None, text=None, full=True, a=None):
    s = new_scorer(va, sym, a=a)
    text = text or s.base
    rx, ry, f = s.listing(text)
    image = getattr(s, 'backend', 'object') == 'image'
    v = s.vector(s.built(text)) if image else metrics.vector(f[1], f[2], s.theirs, va)
    d = classify.diagnose(s.theirs, va, f[1], f[2])
    lines = [f'{va:#x} {project.rows()[va]["source"]} size {len(s.theirs)}  {fmt(v)}',
             f'  classes: {" ".join(d["classes"])}   frame retail {d["frame"][0]:#x} ours {d["frame"][1]:#x}'
             + (f'   REGISTER INPUTS {d["convention"]}' if d['convention'] else '')]
    if d['missing']:
        lines.append('  retail only: ' + '; '.join(f'{k} x{n}' for k, n in d['missing'][:8]))
    if d['extra']:
        lines.append('  ours only:   ' + '; '.join(f'{k} x{n}' for k, n in d['extra'][:8]))
    if d['reg_map']:
        lines.append('  registers (retail->ours): ' + '  '.join(
            f'{r}->' + '/'.join(f'{o}x{n}' for o, n in c) for r, c in sorted(d['reg_map'].items())))
    for h in d['hints']:
        lines.append('  HINT: ' + h)
    for c in (s.caveats(text) if image else ()):
        lines.append('  CAVEAT (reduced image): ' + c)
    if full:
        lines += classify.side_by_side(d)
    return v, d, lines


def cmd_diff(a):
    _, _, lines = report(a.va, a.sym, project.read(a.files[0]) if a.files else None, a=a)
    print('\n'.join(lines))


def cmd_triage(a):
    vas = a.vas or project.open_vas(a.owner)
    if a.limit:
        vas = vas[:a.limit]
    rows = project.rows()
    if a.backend == 'image' or (a.backend == 'auto' and any(rows[va]['owner'] == 'game' for va in vas if va in rows)):
        a.jobs = 1                                     # one reduced-image build at a time per scratch
    def one(va):
        try:
            return va, report(va, full=False, a=a)
        except BaseException as e:  # noqa: BLE001
            return va, (dict(error=str(e)[-300:]), None, [f'{va:#x} ERROR {str(e).strip().splitlines()[-1][:150]}'])
    with concurrent.futures.ThreadPoolExecutor(a.jobs) as pool:
        res = list(pool.map(one, vas))
    res.sort(key=lambda r: metrics.key(r[1][0]) if 'error' not in r[1][0] else (9,))
    for va, (v, d, lines) in res:
        print('\n'.join(lines))
        if d:
            ledger.set_fact(va, 'classes', d['classes'])


def cmd_try(a):
    s = new_scorer(a.va, a.sym, a.start, a)
    text = s.base
    import re
    m = re.search(r'//\s*@retail\s+0x0*%x\b[^\n]*\n' % a.va, text, re.I)
    end = text.index('\n}', m.end()) + 2
    print(f'repo: {fmt(s.text(text, origin="repo"))}')
    for k, var in enumerate(project.read(a.files[0]).split('\n=====\n')):
        t = text[:m.end()] + var.strip('\n') + text[end:]
        v = s.text(t, origin='try', note=f'{os.path.basename(a.files[0])}#{k}', save=f'try_{ledger.digest(t)}.cpp')
        print(f'{k:3d}: {fmt(v)}')


def cmd_exemplars(a):
    v, d, _ = report(a.va, a.sym, project.read(a.files[0]) if a.files else None, full=False, a=a)
    rows = project.rows()
    for level in ('stack', 'loose'):
        print(f'== retail idioms of the divergent regions found in matched functions (level {level})')
        for e in corpus.exemplars(d, level):
            if not e['hits']:
                continue
            print(f'+{e["r0"]:#x}: ' + ' ; '.join(e['seq']))
            for va, pos in e['hits']:
                print(f'      {va:#x} {rows[va]["source"]} (instruction {pos})')
    print('== nearest matched functions')
    for sc, va in corpus.nearest(a.va, owner=None):
        print(f'  {sc:6.1f} {va:#x} {rows[va]["source"]} size {rows[va]["size"]}')


def cmd_idiom(a):
    """Each instruction only retail has, looked up in matched code of the same owner."""
    from h2m import asm
    v, d, _ = report(a.va, a.sym, project.read(a.files[0]) if a.files else None, full=False, a=a)
    rows = project.rows()
    owner = rows[a.va]['owner']
    seqs, _ = corpus.load('loose')
    x = d['x']
    ours = {asm.mask(i.text, 'loose') for i in d['y']}
    seen = set()
    for k, i in enumerate(x):
        t = asm.mask(i.text, 'loose')
        if t in ours or t in seen:
            continue
        seen.add(t)
        hits = []
        for va, s in seqs.items():
            if not rows[va]['owner'].startswith(owner):
                continue
            for n, u in enumerate(s):
                if u == t:
                    ctx = s[max(0, n - 2):n + 3]
                    near = [asm.mask(j.text, 'loose') for j in x[max(0, k - 2):k + 3]]
                    hits.append((-len(set(ctx) & set(near)), len(s), va, ' ; '.join(ctx)))
                    break
        hits.sort()
        print(f'+{i.off:#x} {i.text}   [{t}]  in {len(hits)} matched functions')
        for sc, n, va, ctx in hits[:a.top]:
            print(f'    {va:#x} {rows[va]["source"]} ({n} insns, {-sc} shared): {ctx}')


def cmd_asm(a):
    """Compile any /GL- source (a lab experiment or a variant) and print the code
    of every function in it, or of those whose mangled name contains --sym."""
    import hashlib
    from h2m import asm, objcmp
    for f in a.files:
        obj = os.path.join(project.OUT, 'lab_' + hashlib.sha1(os.path.abspath(f).encode()).hexdigest()[:10] + '.obj')
        os.makedirs(project.OUT, exist_ok=True)
        try:
            objcmp.compile_obj(f, obj)
        except SystemExit as e:
            print(f'{f}: COMPILE FAILED\n{str(e)[-1500:]}')
            continue
        for name, data, masked in objcmp.coff_functions(open(obj, 'rb').read()):
            if a.sym and a.sym not in name:
                continue
            print(f'== {name} ({len(asm.strip_pad(data))} bytes)')
            for i in asm.ours(data, masked):
                print(f'   {i.off:04x} {i.text}')
        os.remove(obj)


def cmd_history(a):
    for h, origin, note, match, lock, dist, sem, frame, path, t in ledger.history(a.va):
        print(f'{"MATCH" if match else f"dist {dist:4d} lock {lock:4d} sem {sem:3d}"}  {origin:10} {note or ""}  {path or h}')


def cmd_scratch(a):
    """show: where the image backend's scratch root is and what it was made from; rm: delete it."""
    from h2m import image
    s = image.Scratch.get(a.scratch)
    if a.action == 'rm':
        s.remove()
        print(f'removed {s.root}')
    elif os.path.exists(s.stamp_path):
        print(f'{s.root}: {open(s.stamp_path).read()}')
    else:
        print(f'{s.root}: not created yet (the first image-backend score creates it)')


def cmd_search(a):
    from h2m import search
    only = set(a.only.split(',')) if a.only else None
    s = search.Search(a.va, a.start, a.time, a.jobs, a.beam, a.seed, only)
    best = s.run()
    if best is not None:
        print(f'best: {search.fmt(best.v)}  (files and log in {os.path.relpath(s.out)})')
    sys.stdout.flush()
    if s.wedged:
        os._exit(0)   # threads stuck on wedged compiles would keep the interpreter alive


def cmd_probe(a):
    from h2m import probe
    sys.exit(probe.main(a))


def cmd_phen(a):
    from h2m import phen
    sys.exit(phen.main(a))


def cmd_webs(a):
    from h2m import webs
    sys.exit(webs.main(a))


def cmd_retail(a):
    """Raw retail listing (capstone, nothing masked): float immediates and the float/double at absolute
    memory operands decoded (h2m's own listings mask hex >= 0x10000)."""
    import capstone, re, struct
    data = bytes(project.retail(a.va))
    lo = int(a.lo, 16) if a.lo else 0
    hi = int(a.hi, 16) if a.hi else len(data)
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
    for i in md.disasm(data, 0):
        if not lo <= i.address < hi:
            continue
        txt = f'{i.mnemonic} {i.op_str}'.strip()
        notes = []
        for m in re.finditer(r'(?:(dword|qword) ptr )?\[(0x[0-9a-f]{5,8})\]', i.op_str):
            addr, width = int(m.group(2), 16), m.group(1)
            try:
                raw = bytes(project._xbe().read(addr, 8))
            except Exception:  # noqa: BLE001
                continue
            fl = struct.unpack('<f', raw[:4])[0]
            if width == 'qword' or i.mnemonic.startswith('f') and 'qword' in i.op_str:
                notes.append(f'[{addr:#x}] = {struct.unpack("<d", raw)[0]!r} (double)')
            else:
                notes.append(f'[{addr:#x}] = {fl!r}f / {struct.unpack("<I", raw[:4])[0]:#x}')
        for m in re.finditer(r'(?<![\[+\-*\w])(0x[0-9a-f]{6,8})\b(?!\])', i.op_str):
            v = int(m.group(1), 16)
            fl = struct.unpack('<f', struct.pack('<I', v))[0]
            if 1e-6 < abs(fl) < 1e7:
                notes.append(f'{m.group(1)} = {fl!r}f')
        print(f'{i.address:04x}  {i.bytes.hex():24} {txt:44}' + ('  ; ' + '; '.join(notes) if notes else ''))


def cmd_fastcheck(a):
    """`check.py --fast VA...` through h2m/run_check.py in any tree (default: $H2M_ROOT or this repository);
    runs in one tree are serialised by a lock file, because they share TREE/build/fast."""
    import hashlib, subprocess
    tree = os.path.abspath(a.tree or project.ROOT)
    env = dict(os.environ, H2REPO=tree)
    cmd = [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'h2m', 'run_check.py'), '--fast']
    cmd += [f'0x{v:x}' for v in a.vas] + (a.args or [])
    lock = os.path.join(project.OUT, f'fastcheck_{hashlib.sha1(tree.encode()).hexdigest()[:10]}.lock')
    os.makedirs(project.OUT, exist_ok=True)
    print(f'[fastcheck] tree {tree}', file=sys.stderr)
    if a.dry:
        print('(cd ' + tree + ' && H2REPO=' + tree + ' ' + ' '.join(cmd) + ')')
        return
    with project.file_lock(lock):
        sys.exit(subprocess.run(cmd, cwd=tree, env=env).returncode)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='cmd', required=True)
    for name in ('score', 'diff', 'try', 'history', 'exemplars', 'idiom'):
        q = sub.add_parser(name)
        q.add_argument('va', type=lambda s: int(s, 16))
        q.add_argument('files', nargs='*')
        q.add_argument('--sym')
        q.add_argument('--start')
        q.add_argument('--top', type=int, default=5)
        q.add_argument('--backend', choices=('auto', 'object', 'image'), default='auto')
        q.add_argument('--scratch', help='image backend: scratch root id (default $H2M_SCRATCH or main)')
        q.add_argument('--extra', help='image backend: VA,VA of callees to keep in the reduced image (remembered per function; none clears)')
    q = sub.add_parser('asm')
    q.add_argument('files', nargs='+')
    q.add_argument('--sym')
    q = sub.add_parser('triage')
    q.add_argument('vas', nargs='*', type=lambda s: int(s, 16))
    q.add_argument('--jobs', type=int, default=2)
    q.add_argument('--owner', default='third:havok', help='owner prefix of the open functions to triage (e.g. game)')
    q.add_argument('--limit', type=int, default=0, help='only the first N functions')
    q.add_argument('--backend', choices=('auto', 'object', 'image'), default='auto')
    q.add_argument('--scratch', help='image backend: scratch root id (default $H2M_SCRATCH or main)')
    q = sub.add_parser('scratch')
    q.add_argument('action', nargs='?', choices=('show', 'rm'), default='show')
    q.add_argument('--scratch')
    q = sub.add_parser('search')
    q.add_argument('va', type=lambda s: int(s, 16))
    q.add_argument('--start')
    q.add_argument('--time', type=float, default=1200)
    q.add_argument('--jobs', type=int, default=2)
    q.add_argument('--beam', type=int, default=6)
    q.add_argument('--seed', type=int, default=0)
    q.add_argument('--only', help='comma-separated mutator names (h2m/mutators.py NAMES)')
    q = sub.add_parser('probe', help='diagnostic perturbations (h2m/probe.py; never for submitted source)')
    q.add_argument('va', type=lambda s: int(s, 16))
    q.add_argument('files', nargs='*')
    q.add_argument('--spec', help='spec file, or the spec text itself (;; separates lines)')
    q.add_argument('--auto', action='store_true', help='rival pairs from the register map, then use-titration')
    q.add_argument('--budget', type=int, default=8, help='compiles per call (the rest is PENDING; rerun)')
    q.add_argument('--out', default='probe', help='subdirectory of <H2M_OUT>/<va>/ for the variant files')
    q.add_argument('--fn', help='apply the perturbations inside this file-local helper (inlined into VA) instead')
    q.add_argument('--sym')
    q = sub.add_parser('phen', help='phenomenon reducer (h2m/phen.py)')
    q.add_argument('pattern', nargs='?', help='instructions separated by ; (registers are bound consistently, * = any text, ? = any instruction)')
    q.add_argument('--at', help='VA:START-END or VA:START+N: take the pattern from retail of that function (hex offsets)')
    q.add_argument('--level', choices=('canon', 'loose', 'stack', 'regs', 'exact'), default='canon')
    q.add_argument('--owner', help='owner prefix ranked first (default: the owner of --at, else third:havok)')
    q.add_argument('--limit', type=int, default=15)
    q.add_argument('--reduce', type=int, default=0, help='reduce the source of the best N standalone hits')
    q.add_argument('--only', help='VA,VA: reduce these hits only')
    q.add_argument('--budget', type=int, default=8, help='compiles per call (state is kept; rerun until DONE)')
    q.add_argument('--name', help='name of the phenomenon (directory under <H2M_OUT>/phen/)')
    q.add_argument('--rebuild', action='store_true', help='rebuild the corpus cache')
    q = sub.add_parser('webs', help='per-web retail vs ours location table (h2m/webs.py)')
    q.add_argument('va', type=lambda s: int(s, 16))
    q.add_argument('files', nargs='*')
    q.add_argument('--sym')
    q.add_argument('--all', action='store_true', help='also list the webs that agree')
    q = sub.add_parser('retail', help='raw retail listing with float constants decoded')
    q.add_argument('va', type=lambda s: int(s, 16))
    q.add_argument('lo', nargs='?')
    q.add_argument('hi', nargs='?')
    q = sub.add_parser('fastcheck', help='tools/check.py --fast for VAs in any tree')
    q.add_argument('vas', nargs='+', type=lambda s: int(s, 16))
    q.add_argument('--tree', help='tree root (default $H2M_ROOT, else this repository)')
    q.add_argument('--dry', action='store_true', help='only print the command')
    q.add_argument('--args', nargs=argparse.REMAINDER, help='extra check.py arguments')
    a = p.parse_args()
    globals()['cmd_' + a.cmd](a)


if __name__ == '__main__':
    main()
