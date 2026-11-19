"""objcmp: compile one /GL- source (Havok) alone with the project's compiler
and compare each function of the object file with the retail bytes of the
@retail markers in that source. No link, so nothing that needs LTCG.

Relocation fields (4 bytes at each COFF relocation) are masked on both sides;
everything else must be identical. Pairing is by content: each marker's retail
bytes are compared with every function in the object of the same length.

usage (from the repository root): PYTHONPATH=tools python -m h2m.objcmp [--keep] src/unknown_XXXX.cpp [...]
Prints per marker: MATCH / DIFF (first differing offset) / NOROW.
"""
import json, os, re, struct, sys
from . import project
import build, xbe  # noqa: E402  (the tree's tools/, on sys.path via project)

ROOT = project.ROOT
MARK = re.compile(r'//\s*@retail\s+0x([0-9a-fA-F]+)')


# tools/build.py never compiles src/X.cpp alone but build/gen/X_tu.cpp = `#include "X.cpp"`, the generated
# vtable callers, then `#pragma auto_inline(off)` + `#pragma inline_depth(0)` and the generated callers. MSVC
# instantiates implicit template specialisations at the END of the translation unit, i.e. after those pragmas, so
# a template helper (even __forceinline) is NOT inlined in the real build while it is when the file is compiled
# alone (measured on 0x2e4660: 2305 bytes alone, 2321 in the build, frame 0x118 vs 0x108). H2M_TU=1 (default)
# compiles through the same tail; H2M_TU=0 compiles the file alone. Ledger entries are salted by the mode.
TU = os.environ.get('H2M_TU', '1') != '0'
TU_SALT = 'h2m-tu1\n' if TU else ''


def tu_wrap(path):
    """The file cl should compile for path: path itself (H2M_TU=0), or a wrapper next to it holding what
    tools/build.py's generated translation unit adds around a source that matters to its code (the vtable
    callers of build/gen/<stem>_tu.cpp when there are any, and the two pragmas). Caller removes the wrapper (!= path)."""
    if not TU:
        return path
    stem = os.path.splitext(os.path.basename(path))[0]
    lines = [f'/* h2m: {os.path.basename(path)} wrapped like the translation unit tools/build.py generates */',
             f'#include "{os.path.abspath(path)}"']
    for cand in (stem, re.sub(r'^w_[0-9a-f]{16}_', '', stem)):
        gen = os.path.join(ROOT, 'build', 'gen', f'{cand}_tu.cpp')
        if os.path.exists(gen):
            g = open(gen, encoding='utf-8', errors='replace').read().splitlines()
            vt = [l for l in g if re.match(rf'void standin_{re.escape(cand)}_vtable_\d+\(', l)]
            if vt:
                lines += ['#include <new>'] + [l for l in g if f'standin_{cand}_arguments[' in l] + vt
            break
    lines += ['#pragma auto_inline(off)', '#pragma inline_depth(0)']
    wrapper = os.path.splitext(path)[0] + '__tu.cpp'
    open(wrapper, 'w', encoding='utf-8').write('\n'.join(lines) + '\n')
    return wrapper


def compile_obj(path, obj):
    text = open(path, encoding='utf-8', errors='replace').read()
    config = json.load(open(os.path.join(ROOT, 'config', 'files.json'), encoding='utf-8'))
    flags = build.file_flags(text, os.path.basename(path), config)
    if os.environ.get('OBJCMP_FLAGS'):
        flags = os.environ['OBJCMP_FLAGS'].split()
    wrapped = tu_wrap(path)
    args = ['/c', *build.source_flags(flags), '/I', os.path.join(ROOT, 'include'),
            '/I', os.path.join(ROOT, 'src'), os.path.abspath(wrapped), '/Fo' + os.path.abspath(obj)]
    try:
        build.run_tool('CL.Exe', args, ROOT, xbe.xdk_dir())
    finally:
        if wrapped != path and os.path.exists(wrapped):
            os.remove(wrapped)
    return flags


def coff_functions(data):
    machine, nsec, _, symptr, nsym, opt, _ = struct.unpack_from('<HHIIIHH', data, 0)
    secs = []
    for i in range(nsec):
        o = 20 + opt + 40 * i
        name = data[o:o + 8].rstrip(b'\0')
        vsize, va, rsize, rptr, relptr, _, nrel, _, ch = struct.unpack_from('<IIIIIIHHI', data, o + 8)
        relocs = set()
        for r in range(nrel):
            off, _, typ = struct.unpack_from('<IIH', data, relptr + 10 * r)
            relocs.update(range(off, off + 4))
        secs.append(dict(name=name, data=data[rptr:rptr + rsize] if rptr else b'', relocs=relocs, ch=ch))
    strtab = symptr + 18 * nsym
    syms, i = [], 0
    while i < nsym:
        o = symptr + 18 * i
        raw = data[o:o + 8]
        if raw[:4] == b'\0\0\0\0':
            so = strtab + struct.unpack_from('<I', raw, 4)[0]
            name = data[so:data.index(b'\0', so)].decode('latin1')
        else:
            name = raw.rstrip(b'\0').decode('latin1')
        value, secnum, typ, cls, naux = struct.unpack_from('<IhHBB', data, o + 8)
        syms.append((name, value, secnum, typ, cls))
        i += 1 + naux
    funcs = []
    by_sec = {}
    for name, value, secnum, typ, cls in syms:
        if secnum > 0 and (typ >> 4) == 2 and secs[secnum - 1]['ch'] & 0x20:
            by_sec.setdefault(secnum, []).append((value, name))
    for secnum, items in by_sec.items():
        s = secs[secnum - 1]
        items.sort()
        for k, (value, name) in enumerate(items):
            end = items[k + 1][0] if k + 1 < len(items) else len(s['data'])
            funcs.append((name, s['data'][value:end], {o - value for o in s['relocs'] if value <= o < end}))
    return funcs


def compare(ours, ours_masked, theirs):
    """First differing offset, or None. Trailing int3/nop padding in ours is ignored."""
    if len(ours) < len(theirs):
        return 'short'
    for k in range(len(theirs)):
        if k in ours_masked:
            continue
        if ours[k] != theirs[k]:
            return k
    if any(b not in (0xCC, 0x90) for b in ours[len(theirs):]):
        return 'long'
    return None


def main():
    keep = '--keep' in sys.argv
    paths = [a for a in sys.argv[1:] if not a.startswith('--')]
    import csv
    rows = {int(r['va'], 16): r for r in csv.DictReader(open(os.path.join(ROOT, 'config', 'functions.csv')))}
    retail = xbe.load(xbe.retail_xbe_path())
    out_dir = os.path.join(project.OUT, 'objcmp')
    os.makedirs(out_dir, exist_ok=True)
    totals = {'MATCH': 0, 'DIFF': 0}
    for path in paths:
        text = open(path, encoding='utf-8', errors='replace').read()
        vas = [int(m.group(1), 16) for m in MARK.finditer(text)]
        obj = os.path.join(out_dir, os.path.splitext(os.path.basename(path))[0] + '.obj')
        try:
            compile_obj(path, obj)
        except SystemExit as e:
            print(f'{path}: COMPILE FAILED\n{str(e)[:2000]}')
            continue
        funcs = coff_functions(open(obj, 'rb').read())
        for va in vas:
            row = rows.get(va)
            if not row:
                print(f'{va:08x} NOROW')
                continue
            size = int(row['size'])
            theirs = retail.read(va, size)
            best = None
            for name, data, masked in funcs:
                d = compare(data, masked, theirs)
                if d is None:
                    best = (name, None)
                    break
                if isinstance(d, int) and (best is None or (isinstance(best[1], int) and d > best[1])):
                    best = (name, d)
            status = 'MATCH' if best and best[1] is None else 'DIFF'
            totals[status] += 1
            print(f'{va:08x} {status:5} csv={row["status"]:7} size={size:5} '
                  f'{"" if not best else best[0][:70]}{"" if status == "MATCH" or not best else f" first@+{best[1]:#x}"}')
        if not keep:
            os.remove(obj)
    print('totals', totals)


if __name__ == '__main__':
    main()
