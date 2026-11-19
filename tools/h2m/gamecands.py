"""Game candidate list: unmatched game functions with their baseline score, outside every
person's claim on the Active claims issue (#9).

usage:
    python tools/h2m/gamecands.py TREE BASELINE.json CLAIMS.md OUT.json [--mine VA,VA...]

TREE = a checkout of this repository, BASELINE = build/report.json of a full build + check of it,
CLAIMS = saved body of issue #9.  Prints a summary and writes OUT.json (sorted by baseline score).
Fields: va, size, src, csv (upstream status), base (baseline score), base_status, claimed (a person's row
covers it), finished (its file is named in the "Finished, not claimed" section), mine.
"""
import csv, json, os, re, sys


def main():
    tree, base, claims, out = map(os.path.abspath, sys.argv[1:5])
    mine = set()
    if '--mine' in sys.argv:
        mine = {int(v, 16) for v in sys.argv[sys.argv.index('--mine') + 1].split(',')}
    sys.path.insert(0, os.path.join(tree, 'tools'))
    os.chdir(tree)
    from ready import claim_rows, covers, _merge
    text = open(claims).read()
    person = _merge([s for who, spans in claim_rows(text) if who.startswith('@') and 'maintainer' not in who.lower()
                     for s in spans])
    fin = text.split('## Finished, not claimed', 1)[-1]
    finished_files = set(re.findall(r'`(src/[^`]+)`', fin))
    report = json.load(open(base))
    rows = []
    for r in csv.DictReader(open(os.path.join(tree, 'config', 'functions.csv'))):
        if r['owner'] != 'game' or r['status'] == 'matched':
            continue
        va = int(r['va'], 16)
        b = report.get(r['va'], {})
        rows.append(dict(va=f'{va:x}', size=int(r['size']), src=r['source'], csv=r['status'],
                         base=b.get('score'), base_status=b.get('status'), ours=b.get('ours'),
                         claimed=bool(covers(person, va)) and va not in mine, mine=va in mine,
                         finished=r['source'] in finished_files))
    rows.sort(key=lambda x: (x['base'] is None, x['base'] or 0))
    json.dump(rows, open(out, 'w'), indent=0)
    free = [x for x in rows if not x['claimed'] and x['src']]
    print(len(rows), 'unmatched game functions;', len(free), 'with source outside person claims;',
          sum(1 for x in free if x['finished']), 'of those in finished files')
    for lo, hi in ((0, 1), (1, 6), (6, 21), (21, 61), (61, 201), (201, 10 ** 9)):
        print(f'  base {lo}..{hi - 1}:', sum(1 for x in free if x['base'] is not None and lo <= x['base'] < hi))
    print('  no base score:', sum(1 for x in free if x['base'] is None))


if __name__ == '__main__':
    main()
