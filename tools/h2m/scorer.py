"""The one compile -> parse -> pick -> measure path (object backend, /GL- files).

Scorer(va).text(full_source) -> metric vector (or {'error': ...}); results are
cached in the ledger by source hash, work files are unique per hash, so any
number of scorers and threads can run at once."""
import concurrent.futures, os
from . import asm, ledger, measure, metrics, objcmp, project


class Scorer:
    def __init__(self, va, sym=None, base=None, origin='manual'):
        self.va, self.sym = va, sym
        self.path = project.source_path(va)
        self.base = project.read(base or self.path)
        self.theirs = project.retail(va)
        self.dir = project.workdir(va)
        self.origin = origin
        self.name = ledger.fact(va, 'symbol') if not sym else None
        self.last = {}                                   # hash -> (name, bytes, masked) of this process

    def compile(self, text, keep=False):
        """[(name, bytes, masked)] of the whole object."""
        h = ledger.digest(text)
        stem = os.path.basename(self.path)
        src = os.path.join(self.dir, f'w_{h}_{stem}')
        obj = src[:-4] + '.obj'
        open(src, 'w', encoding='utf-8').write(text)
        try:
            objcmp.compile_obj(src, obj)
            return objcmp.coff_functions(open(obj, 'rb').read())
        finally:
            for p in (() if keep else (src, obj)):
                if os.path.exists(p):
                    os.remove(p)

    def function(self, text):
        funcs = self.compile(text)
        if self.name:
            f = next((f for f in funcs if f[0] == self.name), None)
            if f:
                return f, funcs
        f = measure.pick(funcs, self.theirs, self.va, self.sym)
        if f and not self.name:
            self.name = f[0]
            ledger.set_fact(self.va, 'symbol', f[0])
        return f, funcs

    def text(self, text, parent=None, origin=None, note='', save=None):
        h = ledger.digest(text)
        v = ledger.get(self.va, h)
        if v is None or (save and 'error' not in v):
            try:
                f, _ = self.function(text)
                if f is None:
                    v = dict(error='function absent from object')
                else:
                    self.last[h] = f
                    v = metrics.vector(f[1], f[2], self.theirs, self.va)
            except SystemExit as e:
                v = dict(error=str(e)[-1500:])
            path = None
            if save and 'error' not in v:
                path = os.path.join(self.dir, save)
                open(path, 'w', encoding='utf-8').write(text)
            ledger.put(self.va, h, v, parent, origin or self.origin, note, path)
        return v

    def body(self, body, **kw):
        return self.text(project.splice(self.base, self.va, body), **kw)

    def many(self, texts, jobs=2, **kw):
        with concurrent.futures.ThreadPoolExecutor(jobs) as pool:
            return list(pool.map(lambda t: self.text(t, **kw), texts))

    def siblings(self, text):
        """{va: matched?} for every marker of the file: nothing else may be lost."""
        funcs = self.compile(text)
        out = {}
        for m in objcmp.MARK.finditer(text):
            va = int(m.group(1), 16)
            if va not in project.rows():
                continue
            theirs = project.retail(va)
            out[va] = any(objcmp.compare(d, mk, theirs) is None for _, d, mk in funcs)
        return out

    def listing(self, text):
        """(retail instructions, our instructions, our function tuple)."""
        f, _ = self.function(text)
        return asm.decode(self.theirs, self.va), asm.ours(f[1], f[2]), f
