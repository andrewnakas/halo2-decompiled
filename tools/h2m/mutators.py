"""Source-to-source mutators for `h2m search`: the techniques that produced
matches (docs/H2M_WORKBENCH.md, "Source techniques"), written as ENUMERATIONS over the function body, plus
the upstream permuter's clean mutations sampled at random.

Every mutator is  fn(body, cx) -> [(new_body, note, helpers)]  where helpers
is a tuple of helper names (HELPERS) the new body needs before the function.
cx is a Ctx (types of the body, the file's Context for field types, rng).
Text model: Allman-style C++ (braces on their own lines, as in repo/src);
anything the model cannot read is skipped, never guessed, and the compiler
rejects what slips through (a wrong reference type does not bind)."""
import collections, random, re
from . import local_mutations, project  # noqa: F401  (project puts the tree's tools/ on sys.path)
import permute  # noqa: E402

KEYWORDS = permute.KEYWORDS | {'sizeof', 'static_cast', 'reinterpret_cast', 'const_cast'}
JUMP = re.compile(r'^(return\b|continue\s*;|break\s*;|goto\b|throw\b)')
NARROW = ('unsigned char', 'signed char', 'unsigned short', 'char', 'short', 'byte', 'word', 'bool')
FALLBACK_TYPES = ('long', 'unsigned long', 'int', 'unsigned short', 'short', 'unsigned char', 'char', 'float')
ID = re.compile(r'[A-Za-z_]\w*')
CALL = re.compile(r'(?<![\w.])(?<!->)([A-Za-z_]\w*)\s*\(|(?:\.|->)\s*([A-Za-z_]\w*)\s*\(')

# helpers a mutator may add before the function (T2); {va} is the function's address
HELPERS = {
    'element': ('// element index of an array member, the member passed by reference\n'
                'template <class T> static __forceinline T &element_{va}(T *const &items, long index)\n'
                '{{\n\treturn items[index];\n}}\n'
                'template <class T, int N> static __forceinline T &element_{va}(T (&items)[N], long index)\n'
                '{{\n\treturn items[index];\n}}\n'),
    'value': ('// a member read through a reference to it\n'
              'template <class T> static __forceinline T const &value_{va}(T const &member)\n'
              '{{\n\treturn member;\n}}\n'),
}


class Ctx:
    def __init__(self, va, text, seed=0):
        self.va = va
        s, _ = project.reduced.find_body(text, va)
        self.context = permute.function_context(text, s)
        self.rng = random.Random(seed)
        self._types = {}

    def types(self, body):
        if body not in self._types:
            if len(self._types) > 64:
                self._types.clear()
            try:
                self._types[body] = permute.declared_types(body, self.context)
            except Exception:  # noqa: BLE001
                self._types[body] = {}
        return self._types[body]

    def etype(self, expr, body):
        try:
            t = permute.expr_type(expr, self.types(body), self.context)
        except Exception:  # noqa: BLE001
            return None
        return None if t in (None, 'void') else t.replace('real', 'float')


# ------------------------------------------------------------------ text model

def code(line):
    """The line without its // comment and literals' contents (for scanning)."""
    line = re.sub(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', '""', line)
    k = line.find('//')
    return line if k < 0 else line[:k]


def indent(line):
    return line[:len(line) - len(line.lstrip())]


def unit(body):
    return '\t' if '\n\t' in body else '    '


def significant(lines, i):
    s = code(lines[i]).strip()
    return bool(s)


class Model:
    """Blocks and statements of a body (lines)."""

    def __init__(self, body):
        self.body = body
        self.lines = body.split('\n')
        n = len(self.lines)
        self.depth = [0] * n          # open braces before the line
        self.opener = [-1] * n        # line of the innermost '{' enclosing the line's start
        self.close = {-1: n}          # opener line -> its closing line
        stack, d = [], 0
        for i, l in enumerate(self.lines):
            self.depth[i], self.opener[i] = d, (stack[-1] if stack else -1)
            for c in code(l):
                if c == '{':
                    stack.append(i)
                    d += 1
                elif c == '}':
                    if not stack:
                        raise ValueError('unbalanced')
                    self.close[stack.pop()] = i
                    d -= 1
        if stack:
            raise ValueError('unbalanced')

    def prev_sig(self, i):
        j = i - 1
        while j >= 0 and not significant(self.lines, j):
            j -= 1
        return j

    def next_sig(self, i):
        j = i + 1
        while j < len(self.lines) and not significant(self.lines, j):
            j += 1
        return j

    def starts_statement(self, i):
        s = code(self.lines[i]).strip()
        if not s or s.startswith(('{', '}', 'else', 'case', 'default', '#')) or s.endswith(':') and '?' not in s:
            return False
        p = self.prev_sig(i)
        if p < 0:
            return True
        ps = code(self.lines[p]).strip()
        return ps.endswith((';', '{', '}')) or (ps.endswith(':') and '?' not in ps)

    def statement_end(self, i):
        """Last line of the statement starting at line i (compound statements included)."""
        s0 = code(self.lines[i]).strip()
        d, par = 0, 0
        for q in range(i, len(self.lines)):
            c = code(self.lines[q])
            d += c.count('{') - c.count('}')
            par += c.count('(') - c.count(')')
            s = c.strip()
            if d < 0:
                return None
            if d == 0 and par == 0 and (s.endswith(';') or s.endswith('}')):
                if s0.startswith('do') and 'while' not in s:
                    continue
                n = self.next_sig(q)
                if n < len(self.lines) and code(self.lines[n]).strip().startswith('else'):
                    continue
                if s.endswith('}') and s0.startswith('do'):
                    continue
                return q
        return None

    def statements(self, op):
        """(start, end) of the statements directly in the block opened at line op (-1: the body)."""
        out, i, end = [], op + 1, self.close[op]
        while i < end:
            if not significant(self.lines, i):
                i += 1
                continue
            if not self.starts_statement(i):
                return out if out else []
            e = self.statement_end(i)
            if e is None or e >= end:
                return out
            out.append((i, e))
            i = e + 1
        return out

    def blocks(self):
        return sorted(self.close)

    def text(self, a, b):
        return '\n'.join(self.lines[a:b + 1])


def roots(expr):
    """Variables an expression reads (identifiers not after . or ->, not calls/keywords)."""
    out = set()
    for m in ID.finditer(expr):
        before = expr[:m.start()].rstrip()
        after = expr[m.end():].lstrip()
        if before.endswith(('.', '->', '::')) or after.startswith(('(', '::')) or m.group(0) in KEYWORDS:
            continue
        out.add(m.group(0))
    return out


def reads_memory(expr):
    return bool(re.search(r'->|\[|\.\s*[A-Za-z_]|(?:^|[=(,]\s*)\*\s*[A-Za-z_(]', expr)) or has_call(expr)


def has_call(text):
    for m in CALL.finditer(code(text)):
        name = m.group(1) or m.group(2)
        if name in KEYWORDS or name.startswith('_mm_') or name in permute.SCALAR_WORDS:
            continue
        return True
    return False


ASSIGN_OP = r'\s*(?:(?:<<|>>|[-+*/%&|^])?=(?!=)|\+\+|--)'
STORE_LHS = re.compile(r'(?:(?:->|\.)\s*(?P<field>[A-Za-z_]\w*)\s*(?:\[[^\]]*\]\s*)*|(?P<index>\])|^\s*(?P<deref>\*))' + ASSIGN_OP
                       + r'|(?:\+\+|--)\s*[A-Za-z_][\w\s.\->\[\]]*(?:->|\.)\s*(?P<field2>[A-Za-z_]\w*)|(?P<sse>_mm_store)')


def stores_memory(text, fields=None):
    """Whether text stores to memory; with fields (names), only stores that may
    reach one of those members count (other members of a struct are assumed
    apart; stores through a plain pointer or an index count always)."""
    for l in text.split('\n'):
        for g in STORE_LHS.finditer(code(l)):
            f = g.group('field') or g.group('field2')
            if fields is None or not f or f in fields:
                return True
    return False


def fields_of(expr):
    return set(re.findall(r'(?:->|\.)\s*([A-Za-z_]\w*)', expr))


def writes(text, name):
    for l in text.split('\n'):
        c = code(l)
        if permute._writes_name(c, name):
            return True
        if re.search(r'^\s*[A-Za-z_][\w:<>\s\*&]*[\s\*&]%s\s*[;(\[]' % re.escape(name), c):
            return True
    return False


def mentions(text, name):
    """Whether text uses the variable name (not a member of that name)."""
    return re.search(r'(?<!->)(?<!\.)(?<!::)\b%s\b' % re.escape(name), code(text)) is not None


def can_cross(between, expr, name=None):
    """Whether a declaration `T name = expr;` may move across the text between."""
    if name and mentions(between, name):
        return False
    for r in roots(expr):
        if writes(between, r):
            return False
    if reads_memory(expr) or has_call(expr):
        if has_call(between) or stores_memory(between, None if has_call(expr) else fields_of(expr)) or re.search(r'\b(return|break|continue|goto)\b', code(between)):
            return False
    if has_call(expr) and reads_memory(between):
        return False
    return True


DECL = re.compile(r'^(?P<ind>[ \t]*)(?P<type>(?:(?:const|unsigned|signed|struct|class)\s+)*[A-Za-z_]\w*(?:::\w+)*'
                  r'(?:\s+(?:const|long|short|int|char))*(?:\s*[\*&]\s*(?:const\b)?)*)\s*\b(?P<name>[A-Za-z_]\w*)'
                  r'\s*(?P<init>=\s*[^;]+|\([^;]*\))?\s*;\s*(?://.*)?$')


def declaration(line):
    m = DECL.match(line)
    if not m:
        return None
    first = m.group('type').split()[0]
    if first in KEYWORDS or m.group('name') in KEYWORDS or first in ('return', 'delete', 'goto'):
        return None
    if not m.group('init') and not re.search(r'[\*&]|\s', m.group('type').strip()) and first in ('else',):
        return None
    return m


def fresh(body, base):
    used = set(ID.findall(body))
    if base not in used and base not in KEYWORDS:
        return base
    k = 2
    while f'{base}{k}' in used:
        k += 1
    return f'{base}{k}'


def field_name(chain):
    names = ID.findall(re.sub(r'\[[^\]]*\]', '', chain))
    return names[-1] if names else 'value'


def decl_text(t, name):
    t = t.strip()
    return f'{t}{name}' if t.endswith(('*', '&')) else f'{t} {name}'


def slots_before(m, i, upto_block=True):
    """Statement-start lines p < i (and i itself) in the block of line i, where a
    declaration can be inserted (insert before p)."""
    op = m.opener[i]
    return [s for s, _ in m.statements(op) if s <= i]


def slots_after(m, i):
    op = m.opener[i]
    out = [s for s, _ in m.statements(op) if s > i]
    return out + [m.close[op]] if op != -1 else out + [len(m.lines)]


# ------------------------------------------------------------------ mutators

def t4_decl_move(body, cx):
    """T4: move each declaration to every legal slot of its block (and into the
    nested block that holds all its uses)."""
    m, out = Model(body), []
    for i, l in enumerate(m.lines):
        d = declaration(l)
        if not d or not m.starts_statement(i) or m.statement_end(i) != i:
            continue
        name, init = d.group('name'), (d.group('init') or '').lstrip('=').strip()
        uses = [j for j in range(i + 1, len(m.lines)) if mentions(m.lines[j], name)]
        for p in slots_before(m, i):
            if p >= i or not can_cross(m.text(p, i - 1), init, name):
                continue
            new = m.lines[:p] + [indent(m.lines[p]) + l.strip()] + m.lines[p:i] + m.lines[i + 1:]
            out.append(('\n'.join(new), f'move {name} up {i - p} lines'))
        first = uses[0] if uses else len(m.lines)
        for p in slots_after(m, i):
            if p > first or p <= i + 1:
                continue
            if not can_cross(m.text(i + 1, p - 1), init, name):
                continue
            ind = indent(m.lines[p]) if p < len(m.lines) and code(m.lines[p]).strip() != '}' else indent(l)
            new = m.lines[:i] + m.lines[i + 1:p] + [ind + l.strip()] + m.lines[p:]
            out.append(('\n'.join(new), f'move {name} down to before line {p}'))
        # sink into the innermost block holding every use
        if uses:
            op = m.opener[uses[0]]
            while op != -1 and not all(op < u <= m.close[op] for u in uses):
                op = m.opener[op]
            if op > i:
                for s, _ in m.statements(op):
                    if s > uses[0]:
                        break
                    if not can_cross(m.text(i + 1, s - 1), init, name):
                        continue
                    new = m.lines[:i] + m.lines[i + 1:s] + [indent(m.lines[s]) + l.strip()] + m.lines[s:]
                    out.append(('\n'.join(new), f'sink {name} into the block at line {op}'))
    return [(b, n, ()) for b, n in out]


TRIVIAL = re.compile(r'\s*(?:[A-Za-z_]\w*|-?\d[\w.]*|&\s*[A-Za-z_]\w*|\(\s*[\w\s\*]+\)\s*&?\s*[A-Za-z_]\w*)\s*$')


def _call_args(line):
    out = []
    for open_, close in permute._call_spans(line):
        name = re.search(r'(\w+)\s*$', line[:open_])
        if name and (name.group(1).startswith('_mm_') and False):
            continue
        for a, b in permute._arg_spans(line, open_ + 1, close):
            part = line[a:b]
            arg = part.strip()
            if not arg or TRIVIAL.match(arg) or '++' in arg or '--' in arg or '=' in arg.replace('==', ''):
                continue
            s = a + len(part) - len(part.lstrip())
            out.append((s, s + len(arg), arg))
    return out


def _introduce(m, i, a, b, expr, t, name, note, body):
    """Read expr (line i, columns a:b) into a local declared at each legal slot."""
    out = []
    line = m.lines[i]
    for p in slots_before(m, i):
        if p < i and not can_cross(m.text(p, i - 1), expr, name):
            continue
        new = m.lines[:]
        new[i] = line[:a] + name + line[b:]
        new.insert(p, indent(m.lines[p]) + f'{decl_text(t, name)} = {expr};')
        out.append(('\n'.join(new), f'{note} at slot -{i - p}', ()))
    return out


def t4_arg_local(body, cx):
    """T4: read a call argument into a local declared at each slot before the call."""
    m, out = Model(body), []
    for i, l in enumerate(m.lines):
        if not m.starts_statement(i) or m.statement_end(i) != i:
            continue
        for a, b, arg in _call_args(l):
            t = cx.etype(arg, body)
            if not t:
                continue
            if t.endswith('*') and not t.startswith('const ') and '(' in arg and 'const' in arg:
                continue
            name = fresh(body, field_name(arg) if re.fullmatch(r'[\w\s.\->]+', arg) else 'argument')
            out += _introduce(m, i, a, b, arg, t, name, f'argument {arg} to local {name}', body)
    return out


def _chain_reads(line):
    try:
        return permute._chain_reads(code(line))
    except Exception:  # noqa: BLE001
        return []


def t4_member_local(body, cx):
    """Technique 10/4: a member read into a named local at each slot before its use."""
    m, out = Model(body), []
    seen = set()
    for i, l in enumerate(m.lines):
        if not m.starts_statement(i) or m.statement_end(i) != i:
            continue
        d = declaration(l)
        for key, a, b in _chain_reads(l):
            if d and d.group('init') and permute._squash(d.group('init').lstrip('=')) == key:
                continue
            if (i, key) in seen:
                continue
            seen.add((i, key))
            t = cx.etype(key, body)
            if not t or not permute._scalar(t):
                continue
            # every read of the chain on this line
            spans = [(x, y) for k, x, y in _chain_reads(l) if k == key]
            name = fresh(body, field_name(key))
            new_line = l
            for x, y in reversed(spans):
                new_line = new_line[:x] + name + new_line[y:]
            for p in slots_before(m, i):
                if p < i and not can_cross(m.text(p, i - 1), key, name):
                    continue
                new = m.lines[:]
                new[i] = new_line
                new.insert(p, indent(m.lines[p]) + f'{decl_text(t, name)} = {l[spans[0][0]:spans[0][1]]};')
                out.append(('\n'.join(new), f'member {key} to local {name} at slot -{i - p}', ()))
    return out


INPLACE = [
    re.compile(r'^(?P<ind>\s*)(?P<op>\+\+|--)\s*(?P<lhs>' + permute.LVALUE + r')\s*;\s*$'),
    re.compile(r'^(?P<ind>\s*)(?P<lhs>' + permute.LVALUE + r')\s*(?P<op>\+\+|--)\s*;\s*$'),
    re.compile(r'^(?P<ind>\s*)(?P<lhs>' + permute.LVALUE + r')\s*(?P<op>(?:<<|>>|[-+*/%&|^])=)\s*(?P<rhs>[^;]+);\s*$'),
]


def t1_inplace(body, cx):
    """T1: an in-place update of a member goes through a reference (`T &r = x.m; --r;`),
    the reference declared at each slot before the update."""
    m, out = Model(body), []
    for i, l in enumerate(m.lines):
        if not m.starts_statement(i):
            continue
        for rx in INPLACE:
            g = rx.match(code(l))
            if g and re.search(r'->|\.|\[', g.group('lhs')):
                break
        else:
            continue
        lhs = g.group('lhs').strip()
        t = cx.etype(lhs, body)
        types = [t] if t else list(FALLBACK_TYPES)
        name = fresh(body, field_name(lhs))
        op = g.group('op')
        stmt = f'{op}{name};' if op in ('++', '--') else f'{name} {op} {g.group("rhs").strip()};'
        for ty in types:
            for p in (slots_before(m, i) if t else [i]):
                if p < i and any(writes(m.text(p, i - 1), r) for r in roots(lhs)):
                    continue
                new = m.lines[:]
                new[i] = g.group('ind') + stmt
                new.insert(p, indent(m.lines[p]) + f'{decl_text(ty + " &", name)} = {lhs};')
                out.append(('\n'.join(new), f'in-place {lhs} through {ty} &{name} at slot -{i - p}', ()))
    return out


INDEXED = re.compile(r'(?<![\w.>])(?P<chain>[A-Za-z_]\w*(?:\s*(?:->|\.)\s*[A-Za-z_]\w*)+)\s*\[(?P<idx>[^\[\]]+)\]')


def t2_byref(body, cx):
    """T2: a member passed by reference to an inline helper: `a.b.data[i]` ->
    element_<va>(a.b.data, i); a member value read through value_<va>(member)."""
    out = []
    for g in INDEXED.finditer(body):
        line_start = body.rfind('\n', 0, g.start()) + 1
        if '//' in body[line_start:g.start()]:
            continue
        new = body[:g.start()] + f'element_{cx.va:x}({g.group("chain")}, {g.group("idx").strip()})' + body[g.end():]
        out.append((new, f'{g.group(0)} by reference to element helper', ('element',)))
    m = Model(body)
    for i, l in enumerate(m.lines):
        if not m.starts_statement(i):
            continue
        for key, a, b in _chain_reads(l):
            t = cx.etype(key, body)
            if not t or t.endswith('*'):
                continue
            new = m.lines[:]
            new[i] = l[:a] + f'value_{cx.va:x}({l[a:b]})' + l[b:]
            out.append(('\n'.join(new), f'{key} read through a reference helper', ('value',)))
    return out


def t11_constref(body, cx):
    """T11: `T const *const &x = obj->ptr;` at each slot; the pointer member read
    through it in the rest of the block."""
    m, out = Model(body), []
    done = set()
    for i, l in enumerate(m.lines):
        if not m.starts_statement(i):
            continue
        for key, a, b in _chain_reads(l):
            if key in done:
                continue
            t = cx.etype(key, body)
            if not t or not t.endswith('*'):
                continue
            done.add(key)
            op = m.opener[i]
            end = m.close[op]
            name = fresh(body, field_name(key))
            base = t[:-1].strip()
            forms = [f'{base} *const &'] if base.startswith('const ') or ' const' in base else \
                [f'{base} const *const &', f'{base} *const &']
            for form in forms:
                new = m.lines[:]
                for j in range(i, min(end, len(new))):
                    if j > i and any(permute._writes_chain(code(new[j]), key) for _ in [0]):
                        break
                    for k, x, y in reversed(_chain_reads(new[j])):
                        if k == key:
                            new[j] = new[j][:x] + name + new[j][y:]
                for p in slots_before(m, i):
                    if p < i and any(writes(m.text(p, i - 1), r) for r in roots(key)):
                        continue
                    n2 = new[:]
                    n2.insert(p, indent(m.lines[p]) + f'{decl_text(form, name)} = {l[a:b]};')
                    out.append(('\n'.join(n2), f'{key} bound as {form}{name} at slot -{i - p}', ()))
    return out


def t7_base(body, cx):
    """T7: a pointer/array member indexed in a statement read once into a base pointer."""
    m, out = Model(body), []
    for i, l in enumerate(m.lines):
        if not m.starts_statement(i) or m.statement_end(i) != i:
            continue
        keys = {}
        for g in INDEXED.finditer(code(l)):
            keys.setdefault(permute._squash(g.group('chain')), []).append(g)
        for key, gs in keys.items():
            t = cx.etype(key, body)
            if not t or not t.endswith('*'):
                continue
            name = fresh(body, 'base')
            new_line = l
            for g in reversed(gs):
                new_line = new_line[:g.start('chain')] + name + new_line[g.end('chain'):]
            for p in slots_before(m, i):
                if p < i and not can_cross(m.text(p, i - 1), key, name):
                    continue
                new = m.lines[:]
                new[i] = new_line
                new.insert(p, indent(m.lines[p]) + f'{decl_text(t, name)} = {gs[0].group("chain")};')
                out.append(('\n'.join(new), f'base pointer {name} for {key} at slot -{i - p}', ()))
    return out


def t9_width(body, cx):
    """T9: a narrow local made long/int (optionally cast back at each use); a long one made narrow."""
    m, out = Model(body), []
    for i, l in enumerate(m.lines):
        d = declaration(l)
        if not d:
            continue
        t = ' '.join(d.group('type').replace('const', ' ').split())
        if t not in NARROW and t not in ('long', 'int', 'unsigned long', 'unsigned int', 'unsigned'):
            continue
        name = d.group('name')
        if t in NARROW:
            targets = ('long', 'int', 'unsigned long')
        else:
            it = cx.etype((d.group('init') or '').lstrip('=').strip(), body) if d.group('init') else None
            targets = (it,) if it in NARROW else ()
        for w in targets:
            if w == t:
                continue
            ty = d.group('type')
            core = re.search(r'\b%s\b' % t.replace(' ', r'\s+'), ty)
            if not core:
                continue
            nl = l[:d.start('type')] + ty[:core.start()] + w + ty[core.end():] + l[d.end('type'):]
            new = m.lines[:]
            new[i] = nl
            out.append(('\n'.join(new), f'{name} {t} -> {w}', ()))
            if t in NARROW and t != 'bool':
                cast = '\n'.join(new[:i + 1] + [re.sub(r'(?<![\w.>])%s\b(?!\s*(?:=(?!=)|\+\+|--|\+=|-=))' % name,
                                                       f'({t}){name}', x) for x in new[i + 1:]])
                out.append((cast, f'{name} {t} -> {w}, cast back at each use', ()))
    return out


def _arms(m, s, e):
    """Arms (open, close) of an if/else-if/else chain occupying lines s..e, and
    whether it ends with a plain else.  None when an arm is not braced."""
    arms, i, final_else = [], s, False
    while i <= e:
        c = code(m.lines[i]).strip()
        if not (c.startswith('if') or c.startswith('else')):
            return None
        final_else = c.startswith('else') and not c[4:].strip().startswith('if')
        j = m.next_sig(i) if not c.endswith('{') else i
        if j > e or code(m.lines[j]).strip() != '{' and not code(m.lines[j]).rstrip().endswith('{'):
            return None
        if j not in m.close:
            return None
        arms.append((j, m.close[j]))
        i = m.next_sig(m.close[j])
    return arms, final_else


def ends_in_jump(m, op):
    sts = m.statements(op)
    if not sts:
        return False
    s, e = sts[-1]
    c = code(m.lines[s]).strip()
    if JUMP.match(c):
        return True
    if c.startswith('if'):
        a = _arms(m, s, e)
        return bool(a) and a[1] and all(ends_in_jump(m, o) for o, _ in a[0])
    if c == '{':
        return ends_in_jump(m, s)
    return False


LOOP = re.compile(r'^\s*(for|while)\s*\(')


def _drop_points(m, op, deep):
    """Close lines of the innermost blocks where control falls out of block op
    (op's own close; with deep, inside the falling arms of a trailing if/else)."""
    sts = m.statements(op)
    if deep and sts:
        s, e = sts[-1]
        if code(m.lines[s]).strip().startswith('if'):
            a = _arms(m, s, e)
            if a and a[1]:
                out = []
                for o, c in a[0]:
                    if not ends_in_jump(m, o):
                        out += _drop_points(m, o, deep)
                return out
    return [m.close[op]]


def t5_tail_dup(body, cx):
    """T5: the shared tail of a loop body after an if/else chain (up to a label or
    the end) written in each arm that falls through, followed by `continue;`."""
    m, out, u = Model(body), [], unit(body)
    for i, l in enumerate(m.lines):
        if not LOOP.match(code(l)) or code(l).strip().startswith('}'):
            continue
        op = m.next_sig(i) if not code(l).rstrip().endswith('{') else i
        if op not in m.close:
            continue
        sts = m.statements(op)
        for k, (s, e) in enumerate(sts):
            if not code(m.lines[s]).strip().startswith('if'):
                continue
            tail = sts[k + 1:]
            if not tail:
                continue
            body_tail = [code(m.text(a, b)) for a, b in tail]
            if any(re.search(r'\b(continue|break)\b', t) for t in body_tail[:-1]) or \
                    re.search(r'\bbreak\b', body_tail[-1]):
                continue
            ends_jump = bool(JUMP.match(body_tail[-1].strip()))
            a = _arms(m, s, e)
            if not a:
                continue
            arms, final_else = a
            falling = [x for x in arms if not ends_in_jump(m, x[0])]
            if not falling:
                continue
            tail_lines = m.lines[tail[0][0]:tail[-1][1] + 1]
            tind = indent(m.lines[tail[0][0]])
            for which in ('all', 'first'):
                if which == 'first' and len(falling) < 2:
                    continue
                for deep in (True, False):
                    points = []
                    for o, c in (falling if which == 'all' else falling[:1]):
                        points += _drop_points(m, o, deep)
                    if not deep and points == sum((_drop_points(m, o, True) for o, _ in falling), []):
                        continue
                    new = m.lines[:]
                    added = 0
                    for c in sorted(points, reverse=True):
                        aind = indent(m.lines[c]) + u
                        add = [aind + x[len(tind):] if x.startswith(tind) else x for x in tail_lines]
                        add += [] if ends_jump else [aind + 'continue;']
                        new[c:c] = add
                        added += len(add)
                    drop = which == 'all' and final_else
                    if drop:
                        a0, a1 = tail[0][0] + added, tail[-1][1] + added
                        del new[a0:a1 + 1]
                    out.append(('\n'.join(new), f'loop tail at line {tail[0][0]} copied into {which} falling arms'
                                + (' (innermost)' if deep else '') + (' (shared tail removed)' if drop else ''), ()))
    return out


FOR = re.compile(r'^(?P<ind>\s*)for\s*\((?P<init>[^;]*);(?P<cond>[^;]*);(?P<step>[^)]*(?:\([^)]*\)[^)]*)*)\)\s*$')


def loop_forms(body, cx):
    """for <-> while, the for-init declared before the loop (each slot), the for-step at the body's end."""
    m, out = Model(body), []
    for i, l in enumerate(m.lines):
        f = FOR.match(code(l).rstrip())
        if f:
            op = m.next_sig(i)
            if op not in m.close:
                continue
            init, cond, step = f.group('init').strip(), f.group('cond').strip(), f.group('step').strip()
            ind = f.group('ind')
            inner = indent(m.lines[op]) + unit(body)
            has_continue = re.search(r'\bcontinue\b', code(m.text(op, m.close[op])))
            if init:
                for p in slots_before(m, i):
                    if p < i and not can_cross(m.text(p, i - 1), init.split('=', 1)[-1]):
                        continue
                    new = m.lines[:]
                    new[i] = f'{ind}for (; {cond}; {step})'.replace('; )', ';)')
                    new.insert(p, indent(m.lines[p]) + init + ';')
                    out.append(('\n'.join(new), f'for-init {init} before the loop at slot -{i - p}', ()))
            if not has_continue:
                new = m.lines[:]
                c = m.close[op]
                if step:
                    new[c:c] = [inner + step + ';']
                new[i] = f'{ind}while ({cond or "true"})'
                if init:
                    new[i:i] = [ind + '{', ind + init + ';'] if False else [ind + init + ';']
                out.append(('\n'.join(new), 'for loop as while', ()))
                if step:
                    new = m.lines[:]
                    new[c:c] = [inner + step + ';']
                    new[i] = f'{ind}for ({init}; {cond};)'
                    out.append(('\n'.join(new), 'for-step at the end of the body', ()))
            continue
        w = re.match(r'^(?P<ind>\s*)while\s*\((?P<cond>.*)\)\s*$', code(l).rstrip())
        if w and m.next_sig(i) in m.close:
            new = m.lines[:]
            new[i] = f'{w.group("ind")}for (; {w.group("cond")};)'
            out.append(('\n'.join(new), 'while loop as for', ()))
    return out


STEP = re.compile(r'^\s*(?:\+\+\s*(?P<a>[A-Za-z_]\w*)|(?P<b>[A-Za-z_]\w*)\s*\+\+)\s*;\s*$')


def loop_cursor(body, cx):
    """Counter vs pointer: a pointer stepped beside the loop index (`p++; i++;`)
    becomes `T *p = &X[i];` at the top of each pass, and the reverse."""
    m, out = Model(body), []
    for i, l in enumerate(m.lines):
        if not LOOP.match(code(l)):
            continue
        op = m.next_sig(i)
        if op not in m.close:
            continue
        cl = m.close[op]
        f = FOR.match(code(l).rstrip())
        steps = [(j, STEP.match(code(m.lines[j]))) for j in range(op + 1, cl)]
        steps = [(j, g.group('a') or g.group('b')) for j, g in steps if g]
        names = collections.Counter(n for _, n in steps)
        idx = None
        if f:
            g = re.match(r'^(?:\w+\s+)?([A-Za-z_]\w*)\s*=', f.group('init').strip())
            idx = g.group(1) if g else None
            if not idx:
                g = re.match(r'^\s*([A-Za-z_]\w*)\s*[<>!]', f.group('cond'))
                idx = g.group(1) if g else None
        # forward: a pointer declared before the loop and stepped where the index is
        for p_ in range(i - 1, -1, -1):
            d = declaration(m.lines[p_])
            if not d or not code(m.lines[p_]).strip():
                continue
            if m.opener[p_] != m.opener[i]:
                break
            ptr = d.group('name')
            if '*' not in d.group('type') or not d.group('init') or ptr not in names or not idx:
                continue
            base = d.group('init').lstrip('=').strip()
            if re.search(r'\b%s\b' % ptr, code(m.text(p_ + 1, i - 1))):
                continue
            pj = [j for j, n in steps if n == ptr]
            if any(not any(n == idx and abs(j - k) <= 2 for k, n in steps) for j in pj):
                continue
            new = m.lines[:]
            for j in sorted(pj, reverse=True):
                del new[j]
            new.insert(op + 1, indent(m.lines[op]) + unit(body) + f'{decl_text(d.group("type"), ptr)} = &{base}[{idx}];')
            del new[p_]
            out.append(('\n'.join(new), f'{ptr} addressed from {idx} at the top of each pass', ()))
        # reverse: `T *p = &X[i];` at the top of the body
        first = m.next_sig(op)
        d = declaration(m.lines[first]) if first < cl else None
        if d and idx and d.group('init'):
            g = re.match(r'=\s*&\s*(?P<x>.+)\[\s*%s\s*\]\s*$' % idx, d.group('init').strip())
            if g:
                ptr = d.group('name')
                new = m.lines[:]
                for j, n in sorted(steps, reverse=True):
                    if n == idx:
                        new.insert(j + 1, indent(m.lines[j]) + f'{ptr}++;')
                del new[first]
                step = f.group('step').strip()
                if step:
                    new[i] = new[i].replace(f.group('step'), f' {step}, {ptr}++')
                new.insert(i, indent(l) + f'{decl_text(d.group("type"), ptr)} = {g.group("x")};')
                out.append(('\n'.join(new), f'{ptr} stepped beside {idx}', ()))
    return out


def t11_member_alias(body, cx):
    """T11/T12: a member used (read or written) two or more times bound once,
    as `T *name = &chain;` (uses become *name) or `T &name = chain;`, at each slot
    before its first use; reads/writes after are through the alias to the block end."""
    m, out = Model(body), []
    counts = collections.Counter()
    first = {}
    for i, l in enumerate(m.lines):
        if not m.starts_statement(i) and not code(l).strip():
            continue
        for g in permute.MEMBER_CHAIN.finditer(code(l)):
            k = permute._squash(g.group('chain'))
            counts[k] += 1
            first.setdefault(k, i)
    for key, n in counts.items():
        if n < 2:
            continue
        i = first[key]
        if not m.starts_statement(i):
            continue
        t = cx.etype(key, body)
        if not t or t.endswith('*'):
            continue
        name = fresh(body, field_name(key))
        op = m.opener[i]
        end = m.close[op]
        for form in ('pointer', 'reference', 'const reference'):
            new = m.lines[:]
            for j in range(i, min(end, len(new))):
                c = new[j]
                if any(permute._writes_name(code(c), r) for r in roots(key)) and j > i:
                    break
                for g in reversed(list(permute.MEMBER_CHAIN.finditer(c))):
                    if permute._squash(g.group('chain')) == key and '//' not in c[:g.start()]:
                        rep = f'*{name}' if form == 'pointer' else name
                        if form == 'pointer' and c[:g.start()].rstrip().endswith('&'):
                            k0 = c[:g.start()].rstrip()
                            new[j] = k0[:-1] + name + c[g.end():]
                            c = new[j]
                            continue
                        c = c[:g.start()] + rep + c[g.end():]
                        new[j] = c
            decl = {'pointer': f'{decl_text(t + " *", name)} = &{key};',
                    'reference': f'{decl_text(t + " &", name)} = {key};',
                    'const reference': f'{decl_text(t + " const &", name)} = {key};'}[form]
            for p in slots_before(m, i):
                if p < i and any(writes(m.text(p, i - 1), r) for r in roots(key)):
                    continue
                n2 = new[:]
                n2.insert(p, indent(m.lines[p]) + decl)
                out.append(('\n'.join(n2), f'{key} through {form} {name} at slot -{i - p}', ()))
    return out


SPLAT2 = re.compile(r'^(?P<ind>\s*)(?P<decl>(?:__m128\s+)?)(?P<v>\w+)\s*=\s*_mm_set_ss\((?P<x>[^;]+)\);\s*\n'
                    r'\s*(?P=v)\s*=\s*_mm_shuffle_ps\(\s*(?P=v)\s*,\s*(?P=v)\s*,\s*0\s*\);', re.M)
SPLAT1 = re.compile(r'^(?P<ind>\s*)(?P<decl>(?:__m128(?:\s+const)?\s+)?)(?P<v>\w+)\s*=\s*_mm_set1_ps\((?P<x>[^;]+)\);', re.M)


def sse_splat(body, cx):
    """T12 idiom: set_ss + shuffle(v, v, 0) <-> _mm_set1_ps."""
    out = []
    for g in SPLAT2.finditer(body):
        rep = f'{g.group("ind")}{g.group("decl")}{g.group("v")} = _mm_set1_ps({g.group("x")});'
        out.append((body[:g.start()] + rep + body[g.end():], f'{g.group("v")} broadcast with _mm_set1_ps', ()))
    for g in SPLAT1.finditer(body):
        d = g.group('decl').replace('const', '').strip()
        d = d + ' ' if d else ''
        rep = (f'{g.group("ind")}{d}{g.group("v")} = _mm_set_ss({g.group("x")});\n'
               f'{g.group("ind")}{g.group("v")} = _mm_shuffle_ps({g.group("v")}, {g.group("v")}, 0);')
        out.append((body[:g.start()] + rep + body[g.end():], f'{g.group("v")} broadcast with set_ss + shuffle', ()))
    return out


# ------------------------------------------------------------------ sampled upstream/local mutations

UPSTREAM = [f for f in permute.MUTATIONS + permute.TYPED_MUTATIONS + local_mutations.LOCAL_MUTATIONS
            if f.__name__ not in local_mutations.UNCLEAN]


BUNGIE = {'dword': 'unsigned long', 'word': 'unsigned short', 'byte': 'unsigned char', 'real': 'float'}
BROKEN = [re.compile(r'\(\s*[A-Za-z_][\w:]*(?:\s+const)?\s*\*?\s*[A-Za-z_]\w*\s*\)\s*=(?!=)'),   # (T *x) = e
          re.compile(r'[\w)\]]\s*[&*+]\s*\(\s*[A-Za-z_][\w:]*(?:\s+const)?\s*\*\s*\)\s*[),;]')]   # x & (T *)


def repair(old, new):
    """Upstream mutations write Bungie typedefs (dword, real) the Havok files lack, and
    sometimes swap a cast off its operand: fix the first, drop the second (None)."""
    keep = set(old.split('\n'))
    out = []
    for l in new.split('\n'):
        if l not in keep:
            if any(r.search(code(l)) for r in BROKEN):
                return None
            l = re.sub(r'^(\s*)(dword|word|byte|real)\b', lambda g: g.group(1) + BUNGIE[g.group(2)], l)
            l = re.sub(r'\((dword|word|byte|real)\)', lambda g: f'({BUNGIE[g.group(1)]})', l)
        out.append(l)
    return '\n'.join(out)


def sampled(fn, samples=6):
    typed = fn in permute.TYPED_MUTATIONS

    def run(body, cx):
        out, seen = [], set()
        for _ in range(samples):
            try:
                new = fn(body, cx.rng, cx.context) if typed else fn(body, cx.rng)
            except Exception:  # noqa: BLE001
                new = None
            new = repair(body, new) if new else None
            if new and new != body and new not in seen:
                seen.add(new)
                out.append((new, fn.__name__, ()))
        return out
    run.__name__ = 'up_' + fn.__name__.replace('mut_', '')
    return run


ENUMERATED = [t1_inplace, t2_byref, t4_decl_move, t4_arg_local, t4_member_local, t5_tail_dup, t7_base,
              t9_width, t11_constref, t11_member_alias, sse_splat, loop_forms, loop_cursor]
ALL = ENUMERATED + [sampled(f) for f in UPSTREAM]
NAMES = {f.__name__: f for f in ALL}

# prior weight per mutator for each diagnosed class (classify.diagnose); default 1
CLASS_WEIGHTS = {
    'regs': dict(t11_member_alias=2, sse_splat=1.5, t4_decl_move=3, t4_arg_local=3, t4_member_local=2, t11_constref=3, t7_base=2, t2_byref=1.5, loop_cursor=2,
                 up_reorder=1.5, up_move_statement=1.5, up_wrap_block=1.5),
    'schedule': dict(t11_member_alias=2, sse_splat=2, t4_decl_move=3, t4_arg_local=2, t4_member_local=2, t11_constref=2, t5_tail_dup=2,
                     up_reorder=2, up_move_statement=2, up_parenthesise=1.5),
    'semantic': dict(t1_inplace=3, t2_byref=2.5, t5_tail_dup=2.5, t4_member_local=2, t11_constref=2, t9_width=1.5,
                     loop_forms=2, t7_base=1.5, loop_cursor=2),
    'width': dict(t9_width=6, up_retype=0),
    'fold': dict(up_parenthesise=3, up_introduce_temp=2, up_inline_temp=2, t4_member_local=1.5),
    'stack': dict(up_wrap_block=3, up_hoist_decl=3, t4_decl_move=2, loop_forms=1.5),
    'convention': {},
}
HINT_WEIGHTS = [('updates memory in place', dict(t1_inplace=5)),
                ('BY REFERENCE', dict(t2_byref=4, t11_constref=3)),
                ('reciprocal', dict(up_introduce_temp=2, t4_member_local=1.5)),
                ('make the local `long`', dict(t9_width=5))]


# ------------------------------------------------------------------ policy

def policy(body, start_body):
    """(rejected reason or None, flags) of a candidate body."""
    c = '\n'.join(code(l) for l in body.split('\n'))
    if re.search(r'\bvolatile\b', c):
        return 'volatile', []
    if 'noinline' in c:
        return 'noinline', []
    if re.search(r'^\s*#\s*pragma', body, re.M):
        return '#pragma', []
    s0 = '\n'.join(code(l) for l in start_body.split('\n'))
    if len(re.findall(r'\bgoto\b', c)) > len(re.findall(r'\bgoto\b', s0)):
        return 'goto', []
    d1, d0 = declared(body), declared(start_body)
    twice = {n for n, k in d1.items() if k > 1 and d0.get(n, 0) < k}
    if twice:
        return 'local declared twice (shadowing) ' + ','.join(sorted(twice)), []
    unused = unused_locals(body) - unused_locals(start_body)
    if unused:
        return 'unused local ' + ','.join(sorted(unused)), []
    flags = []
    one = one_use_locals(body) - one_use_locals(start_body)
    if one:
        flags.append('one-use ' + ','.join(sorted(one)))
    return None, flags


def declared(body):
    c = collections.Counter()
    for l in body.split('\n'):
        d = declaration(l)
        if d:
            c[d.group('name')] += 1
    return c


def unused_locals(body):
    """Locals that are only written (declared, assigned, stepped), never read."""
    return {n for n, k in read_counts(body).items() if k == 0}


def read_counts(body):
    """local -> number of places that read it (declarations, plain assignments and steps are not reads)."""
    text = '\n'.join(code(l) for l in body.split('\n'))
    out = {}
    for n in declared(body):
        reads = 0
        for g in re.finditer(r'(?<!->)(?<!\.)\b%s\b' % re.escape(n), text):
            line_start = text.rfind('\n', 0, g.start()) + 1
            line_end = text.find('\n', g.end())
            line = text[line_start:line_end if line_end >= 0 else len(text)]
            after = text[g.end():].lstrip()
            before = text[line_start:g.start()].strip()
            if declaration(line) and declaration(line).group('name') == n:
                continue
            if re.match(r'(?:(?:<<|>>|[-+*/%&|^])?=(?!=)|\+\+|--)', after) and before in ('', '++', '--'):
                continue
            if before in ('++', '--') and after.startswith(';'):
                continue
            reads += 1
        out[n] = reads
    return out


def one_use_locals(body):
    """Locals read in exactly one place (named temporaries review rejects unless they match)."""
    return {n for n, k in read_counts(body).items() if k == 1}
