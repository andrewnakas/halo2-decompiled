"""The image backend: scores LTCG (/GL) game functions on a reduced image.

ImageScorer(va).text(full_source) has scorer.Scorer's interface. A variant of
the target's whole source file is written into a private scratch root
(<H2M_OUT>/scratch/<id>/: copies of the tree's src, include, config and build
caches, made once with upstream's permute.prepare_scratch and reused), the
reduced image is built there by upstream's own check.fast_check (tools/check.py
--fast), and the function's bytes are
read back from the reduced image and its map.

    lock, match   upstream's own numbers: the entry check.fast_check writes to
                  build/report-fast.json (score, status) - what
                  `check.py --fast VA` prints for the same sources
    dist, sem,    metrics.vector on the image's bytes, with the image's address
    frame, first  fields (base relocations + /MAPINFO:FIXUPS) as the mask

Upstream's modules are loaded privately from <ROOT>/tools (tools()), so the
patches made here never reach the object backend's copies.

Every result depends on the scratch's other sources and on the baseline full
build, so ledger rows are keyed by sha1('img:<baseline stamp>:<digest of the
headers>' + text): a new baseline build of the tree or a header edit starts a
new cache (an edit to ANOTHER .cpp of the reduced image does not: a limit);
the object backend's rows are never read or replaced.

One build at a time per scratch (a file lock next to it); use one scratch id
per concurrent user (H2M_SCRATCH or --scratch). `h2m.py scratch rm` deletes it."""
import contextlib, filecmp, importlib, io, json, os, re, shutil, sys, threading, time, types
from . import asm, ledger, metrics, objcmp, project

SCRATCHES = os.path.join(project.OUT, 'scratch')
SYNCED = ('src', 'include', 'config')
JOBS = 2                                   # sources compiled at once
_tools = None
_tools_lock = threading.Lock()


def tools():
    """Upstream's build, check, reduced, permute, xbe ... loaded from
    <ROOT>/tools as private copies (a namespace). When XDK_DIR/lib lacks
    libraries the build names, they are left out of the reduced link (and the
    D3D member objects with d3d8ltcg.lib), so a function that needs no SDK
    library can still be scored; with a complete XDK_DIR nothing is changed."""
    global _tools
    with _tools_lock:
        if _tools is not None:
            return _tools
        folder = os.path.join(project.ROOT, 'tools')
        names = {n[:-3] for n in os.listdir(folder) if n.endswith('.py')}
        stashed = {n: sys.modules.pop(n) for n in names if n in sys.modules}
        sys.path.insert(0, folder)
        try:
            for n in ('build', 'check', 'reduced', 'permute', 'xbe', 'linkmap', 'pe', 'inventory'):
                importlib.import_module(n)
            ns = types.SimpleNamespace(**{n: sys.modules[n] for n in names if n in sys.modules})
        finally:
            sys.path.remove(folder)
            for n in names:
                sys.modules.pop(n, None)
            sys.modules.update(stashed)
        xdk = ns.xbe.xdk_dir()
        lib = os.path.join(xdk, 'lib')
        if os.path.isdir(lib):
            have = {n.lower() for n in os.listdir(lib)}
            if ns.build.D3D_LIBRARY.lower() not in have:
                ns.build.d3d_objects = lambda root, xdk: []
            ns.build.LIBRARIES[:] = [l for l in ns.build.LIBRARIES if l.lower() in have]
        ns.retail = ns.xbe.Xbe(ns.xbe.retail_xbe_path())

        def as_build(function):
            """The parse cache pickles build.Marked by module name: while it is
            read or written, sys.modules['build'] is the private copy."""
            def wrapped(path):
                shared = sys.modules.get('build')
                sys.modules['build'] = ns.build
                try:
                    return function(path)
                finally:
                    if shared is None:
                        del sys.modules['build']
                    else:
                        sys.modules['build'] = shared
            return wrapped
        ns.build.load_parse_cache = as_build(ns.build.load_parse_cache)
        ns.build.save_parse_cache = as_build(ns.build.save_parse_cache)
        _tools = ns
        return ns


def headers_token():
    """A digest of what every source of the tree is compiled against: the
    headers under include/ and src/, and config/files.json (the flags)."""
    import hashlib
    h = hashlib.sha1()
    paths = [os.path.join(project.ROOT, 'config', 'files.json')]
    for base in ('include', 'src'):
        for d, _, names in os.walk(os.path.join(project.ROOT, base)):
            paths += [os.path.join(d, n) for n in names if n.endswith('.h')]
    for path in sorted(paths):
        h.update(path.encode() + b'\0' + open(path, 'rb').read())
    return h.hexdigest()[:12]


def scratch_id(given=None):
    name = given or os.environ.get('H2M_SCRATCH') or 'main'
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', name):
        raise SystemExit(f'scratch id {name!r}: letters, digits, _ . - only')
    return name


class Scratch:
    """A private copy of the tree that reduced images are built in."""
    _open = {}

    def __init__(self, name=None):
        self.name = scratch_id(name)
        self.root = os.path.join(SCRATCHES, self.name)
        self.lock_path = self.root + '.lock'
        self.stamp_path = os.path.join(self.root, 'h2m_scratch.json')
        self._thread = threading.RLock()
        self._ready = False
        self.stamp = None

    @classmethod
    def get(cls, name=None):
        name = scratch_id(name)
        if name not in cls._open:
            cls._open[name] = cls(name)
        return cls._open[name]

    @contextlib.contextmanager
    def locked(self):
        """One build at a time: across threads and across processes."""
        os.makedirs(SCRATCHES, exist_ok=True)
        with self._thread, project.file_lock(self.lock_path):
            yield self

    @staticmethod
    def baseline():
        """What identifies the tree's last full build ('' without one)."""
        path = os.path.join(project.ROOT, 'build', 'halo2.map')
        return str(os.stat(path).st_mtime_ns) if os.path.exists(path) else ''

    def prepare(self):
        """Call with the lock held: creates the scratch once, then brings its
        src/include/config in line with the tree (a variant left by a killed
        run, or sources edited in the tree since). Changed files are rewritten,
        never copied with their old times: an object built from the replaced
        text must become stale."""
        if self._ready:
            return
        t = tools()
        if not os.path.exists(self.stamp_path):
            if os.path.isdir(self.root):
                shutil.rmtree(self.root)              # a half-made scratch
            os.makedirs(self.root)
            print(f'[img] creating scratch {self.root} from {project.ROOT}', file=sys.stderr, flush=True)
            t.permute.prepare_scratch(self.root)
            out = os.path.join(self.root, 'build')
            os.makedirs(out, exist_ok=True)
            for name in (t.build.EXE_NAME, t.build.MAP_NAME):
                src = os.path.join(project.ROOT, 'build', name)
                if os.path.exists(src):
                    shutil.copyfile(src, os.path.join(out, name))   # stamped now: after the copied objects
            json.dump(dict(root=project.ROOT, baseline=self.baseline(), created=time.time()),
                      open(self.stamp_path + '.tmp', 'w'))
            os.replace(self.stamp_path + '.tmp', self.stamp_path)
        self.stamp = json.load(open(self.stamp_path))
        if self.stamp['root'] != project.ROOT:
            raise SystemExit(f'scratch {self.root} was made from {self.stamp["root"]}, not {project.ROOT}: '
                             'use another scratch id or delete it (h2m.py scratch rm)')
        if self.stamp['baseline'] != self.baseline():
            print(f'[img] WARNING: {project.ROOT} was fully built again after scratch {self.name} was made; '
                  'delete the scratch (h2m.py scratch rm) to take the new build caches and map',
                  file=sys.stderr, flush=True)
        self.sync()
        self._ready = True

    def sync(self):
        changed = 0
        for top in SYNCED:
            a, b = os.path.join(project.ROOT, top), os.path.join(self.root, top)
            for d, _, names in os.walk(a):
                rel = os.path.relpath(d, a)
                os.makedirs(os.path.join(b, rel), exist_ok=True)
                for n in names:
                    x, y = os.path.join(d, n), os.path.join(b, rel, n)
                    if not os.path.exists(y) or not filecmp.cmp(x, y, shallow=False):
                        shutil.copyfile(x, y)
                        changed += 1
            for d, _, names in os.walk(b):
                rel = os.path.relpath(d, b)
                for n in names:
                    if not os.path.exists(os.path.join(a, rel, n)):
                        os.remove(os.path.join(d, n))
                        changed += 1
        if changed:
            print(f'[img] scratch {self.name}: {changed} files brought in line with the tree', file=sys.stderr,
                  flush=True)

    def write(self, rel, text):
        """Writes a source of the scratch unless it already holds text."""
        path = os.path.join(self.root, *rel.split('/'))
        try:
            if open(path, encoding='utf-8', newline='').read() == text:
                return
        except (OSError, UnicodeDecodeError):
            pass
        with open(path, 'w', encoding='utf-8', newline='') as f:
            f.write(text)

    def remove(self):
        with self.locked():
            shutil.rmtree(self.root, ignore_errors=True)
            self._ready = False


def extra_vas(va, given=None):
    """Functions kept in the reduced image besides the target (callees the full
    build needs to see to build the target the same way). given: 'va,va' from
    --extra ('none' clears); else $H2M_EXTRA; else what the ledger remembers for
    this function. A given list is remembered."""
    given = given if given is not None else os.environ.get('H2M_EXTRA')
    if given is None:
        return set(ledger.fact(va, 'extra', []))
    vas = set() if given.strip().lower() in ('', 'none') else {int(x, 16) for x in given.split(',') if x.strip()}
    unknown = sorted(v for v in vas if v not in project.rows())
    if unknown:
        raise SystemExit('--extra: not in config/functions.csv: ' + ', '.join(f'{v:#x}' for v in unknown))
    ledger.set_fact(va, 'extra', sorted(vas))
    return vas


class Built:
    """One variant on the reduced image: the report entry of each checked
    function, the target's code, and what the check printed."""

    def __init__(self, entries, function, output, seconds):
        self.entries, self.function, self.output, self.seconds = entries, function, output, seconds


class ImageScorer:
    backend = 'image'

    def __init__(self, va, sym=None, base=None, origin='manual', scratch=None, jobs=JOBS, extra=None):
        self.va, self.sym = va, sym
        self.extra = extra_vas(va, extra)
        self.rel = project.rows()[va]['source']
        if not self.rel:
            raise SystemExit(f'{va:#x} has no source in config/functions.csv')
        self.path = project.source_path(va)
        self.pristine = open(self.path, encoding='utf-8', newline='').read()
        self.base = project.read(base or self.path)
        self.theirs = project.retail(va)
        self.dir = project.workdir(va)
        self.origin = origin
        self.jobs = max(1, min(int(jobs), JOBS))
        self.scratch = Scratch.get(scratch)
        self.name = None
        self.last = {}                                   # hash -> Built of this process
        self.seconds = []                                # wall time of each build
        self._salt = None

    # -- keys -------------------------------------------------------------
    def salt(self):
        if self._salt is None:
            with self.scratch.locked():
                self.scratch.prepare()
            self._salt = f'img:{self.scratch.stamp["baseline"]}:{headers_token()}\n'
            if self.extra:
                self._salt += 'extra:' + ','.join(f'{v:x}' for v in sorted(self.extra)) + '\n'
        return self._salt

    def digest(self, text):
        return ledger.digest(self.salt() + text)

    # -- building ---------------------------------------------------------
    def build(self, text, wanted=None):
        """Builds the reduced image for the functions at wanted (default: the
        target) with text as the target's source; a Built. Raises SystemExit
        when the compile or link fails."""
        t = tools()
        wanted = set(wanted or [self.va]) | self.extra
        started = time.time()
        out = io.StringIO()
        with self.scratch.locked():
            self.scratch.prepare()
            root = self.scratch.root
            report_path = os.path.join(root, t.check.FAST_REPORT)
            if os.path.exists(report_path):
                os.remove(report_path)
            self.scratch.write(self.rel, text)
            try:
                with contextlib.redirect_stdout(out):
                    t.check.fast_check(wanted, t.retail, self.jobs, root=root)
                if not os.path.exists(report_path):
                    raise SystemExit('check --fast: ' + out.getvalue().strip()[-1500:])
                entries = {int(k, 16): e for k, e in json.load(open(report_path)).items()}
                function = self._function(t, root) if self.va in wanted else None
            finally:
                self.scratch.write(self.rel, self.pristine)
        seconds = time.time() - started
        self.seconds.append(seconds)
        m = re.search(r'^time .*$', out.getvalue(), re.M)
        print(f'[img {self.va:#x}] {m.group(0) if m else f"time {seconds:.1f}s"}', file=sys.stderr, flush=True)
        return Built(entries, function, out.getvalue(), seconds)

    def _function(self, t, root):
        """(symbol name, our code as check compares it, masked offsets) of the
        target in the reduced image just built, or None when it is not there."""
        image_path = os.path.join(root, t.check.FAST_IMAGE)
        ours = t.reduced.Image.read(os.path.splitext(image_path)[0] + '.map', image_path)
        marked = t.build.marked_sources(root)
        m = next(m for m in marked if m.retail == self.va)
        symbol = t.check.resolve(ours.linkmap, m, t.check.StandinCalls(ours.linkmap, ours.image, marked))
        if symbol is None:
            return None
        start, end = ours.linkmap.extent(symbol)
        code = ours.image.read(start, end - start)
        relative = t.check._within(ours.relative, start - 3, end)
        data = t.check.extract(code, self.theirs)
        # a relative field that stays inside the function (a recursive call) is left
        # unmasked: it then decodes to a label, as retail's does
        inside = [f for f in t.check.field_starts(start, len(data), relative)
                  if 0 <= f + 4 + int.from_bytes(data[f:f + 4], 'little', signed=True) < len(data)]
        fixups = [*t.check._within(ours.absolute, start - 3, end), *relative]
        masked = t.check.masked_offsets(start, len(data), fixups) - {f + i for f in inside for i in range(4)}
        return symbol.name, data, masked

    def built(self, text):
        h = self.digest(text)
        if h not in self.last:
            self.last[h] = self.build(text)
            with open(os.path.join(self.dir, 'img_last_check.txt'), 'w', encoding='utf-8') as f:
                f.write(self.last[h].output)
        return self.last[h]

    def vector(self, built):
        """The metric vector of a Built: lock and match are upstream's."""
        e = built.entries.get(self.va)
        if e is None or built.function is None or e.get('score') is None:
            return dict(error='function absent from the reduced image (folded into an identical function, '
                              'or nothing references it)')
        if e['status'] == 'matched':
            return dict(match=1, lock=0, dist=0, sem=0, frame=0, first=-1)
        name, data, masked = built.function
        v = metrics.vector(data, masked, self.theirs, self.va)
        first = e.get('first_difference')
        return dict(v, match=0, lock=e['score'], first=v['first'] if v['first'] >= 0 or first is None else first)

    def caveats(self, text):
        b = self.last.get(self.digest(text))
        return [] if b is None else b.entries.get(self.va, {}).get('caveats', [])

    # -- scorer.Scorer's interface -----------------------------------------
    def text(self, text, parent=None, origin=None, note='', save=None):
        h = self.digest(text)
        v = ledger.get(self.va, h)
        if v is None or (save and 'error' not in v):
            keep = True
            try:
                v = self.vector(self.built(text))
            except SystemExit as e:
                v = dict(error=str(e)[-1500:])
                keep = 'timed out' not in v['error']
            path = None
            if save and 'error' not in v:
                path = os.path.join(self.dir, save)
                open(path, 'w', encoding='utf-8').write(text)
            if keep:
                ledger.put(self.va, h, v, parent, 'img:' + (origin or self.origin), note, path)
                ledger.set_fact(self.va, 'backend', 'image')
        return v

    def body(self, body, **kw):
        return self.text(project.splice(self.base, self.va, body), **kw)

    def many(self, texts, jobs=1, **kw):
        return [self.text(t, **kw) for t in texts]       # one build at a time per scratch

    def siblings(self, text):
        """{va: matched?} for every marker of the file, on one reduced image."""
        vas = sorted({int(m.group(1), 16) for m in objcmp.MARK.finditer(text)} & set(project.rows()))
        marked = {m.retail for m in tools().build.marked_sources(project.ROOT)}
        vas = [va for va in vas if va in marked]
        try:
            entries = self.build(text, vas).entries
        except SystemExit:
            return {va: False for va in vas}
        return {va: entries.get(va, {}).get('status') == 'matched' for va in vas}

    def function(self, text):
        b = self.built(text)
        return b.function, [b.function] if b.function else []

    def listing(self, text):
        """(retail instructions, our instructions, our function tuple)."""
        f = self.built(text).function
        if f is None:
            raise SystemExit('function absent from the reduced image')
        return asm.decode(self.theirs, self.va), asm.ours(f[1], f[2]), f


def backend_of(va, given=None):
    """'image' or 'object': given (--backend), else by the function's owner."""
    if given and given != 'auto':
        return given
    row = project.rows().get(va)
    return 'image' if row is not None and row['owner'] == 'game' else 'object'


def make(va, sym=None, base=None, origin='manual', backend=None, scratch=None):
    """The scorer of the function's backend."""
    if backend_of(va, backend) == 'image':
        return ImageScorer(va, sym, base, origin, scratch)
    from . import scorer
    return scorer.Scorer(va, sym, base, origin)
