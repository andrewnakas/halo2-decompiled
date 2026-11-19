"""One disassembler and one normaliser with levels.

level 'reloc': addresses -> ADDR, in-function branch targets -> L<off>.
level 'regs' : + registers -> R / X (xmm), 8/16-bit kept distinct as Rb / Rw.
level 'stack': + [esp+N] / [ebp-N] -> [S].
level 'loose': + every remaining number -> N.
Each instruction is an Ins(off, size, mnem, ops, text) where text is the
normalised spelling at level 'reloc'; mask(text, level) derives the others."""
import collections, re
import capstone

MD = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
Ins = collections.namedtuple('Ins', 'off size mnem ops text')
_HEX = re.compile(r'0x[0-9a-f]+')
R32 = re.compile(r'\b(eax|ebx|ecx|edx|esi|edi|ebp)\b')
R16 = re.compile(r'\b(ax|bx|cx|dx|si|di|bp)\b')
R8 = re.compile(r'\b([abcd][lh])\b')
XMM = re.compile(r'\bxmm\d\b')
STK = re.compile(r'\[esp(?: \+ [^\]]+)?\]|\[ebp [-+] [^\]]+\]')
LBL = re.compile(r'\bL[0-9a-f]+\b')
NUM = re.compile(r'\b(0x[0-9a-f]+|\d+)\b')


def strip_pad(data):
    end = len(data)
    while end and data[end - 1] in (0xCC, 0x90):
        end -= 1
    return bytes(data[:end])


def apply_mask(data, masked):
    b = bytearray(data)
    for o in masked:
        if o < len(b):
            b[o] = 0x7F
    return bytes(b)


def decode(data, base=0, size=None):
    """Normalised instructions of data[:size] loaded at base."""
    size = len(data) if size is None else size
    out = []
    for i in MD.disasm(bytes(data[:size]), base):
        ops = i.op_str
        if i.mnemonic.startswith(('j', 'call', 'loop')) and '[' not in ops and ops[:1].isdigit():
            v = int(ops, 0)
            ops = f'L{v - base:x}' if base <= v < base + size else 'ADDR'
        else:
            ops = _HEX.sub(lambda m: 'ADDR' if int(m.group(0), 16) >= 0x10000 else m.group(0), ops)
            ops = ops.replace('[0x0]', '[ADDR]').replace('ptr [0]', 'ptr [ADDR]')
            ops = re.sub(r'fs:\[[^\]]*\]', 'fs:[ADDR]', ops)
            # an unlinked reloc of ours is 7f7f7f7f -> ADDR; a zero immediate that
            # retail fills with an address is handled by masking before decode
        out.append(Ins(i.address - base, i.size, i.mnemonic, ops, (i.mnemonic + ' ' + ops).strip()))
    return out


def ours(data, masked):
    b = strip_pad(apply_mask(data, masked))
    return decode(b, 0, len(b))


def mask(text, level):
    if level == 'reloc':
        return text
    t = text
    if level in ('stack', 'loose'):
        t = STK.sub('[S]', t)
    t = XMM.sub('X', t)
    t = R32.sub('R', t)
    t = R16.sub('Rw', t)
    t = R8.sub('Rb', t)
    t = LBL.sub('L', t)
    if level == 'loose':
        t = NUM.sub('N', t)
    return t


_MOVE = re.compile(r'(mov|movaps|movss) R, R|movaps X, X|nop|lea R, \[R\]')


def bag(ins, level='stack'):
    """Multiset of masked instructions; register-to-register moves dropped."""
    c = collections.Counter()
    for i in ins:
        m = mask(i.text, level)
        if i.mnem in ('sub', 'add') and i.ops.startswith('esp, '):
            continue                                   # frame size is reported on its own
        if not _MOVE.fullmatch(m):
            c[m] += 1
    return c


def frame(ins):
    """Bytes reserved by the prologue's `sub esp, N` (0 if none in the first 12)."""
    for i in ins[:12]:
        if i.mnem == 'sub' and i.ops.startswith('esp, '):
            try:
                return int(i.ops[5:], 0)
            except ValueError:
                return 0
    return 0


def live_in(data, base=0):
    """Registers read before written on the straight-line entry path (up to the
    first branch/ret/call). A push that only saves a register is not a read."""
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_32)
    md.detail = True
    written, live = set(), set()
    fam = {'al': 'eax', 'ah': 'eax', 'ax': 'eax', 'bl': 'ebx', 'bh': 'ebx', 'bx': 'ebx', 'cl': 'ecx',
           'ch': 'ecx', 'cx': 'ecx', 'dl': 'edx', 'dh': 'edx', 'dx': 'edx', 'si': 'esi', 'di': 'edi', 'bp': 'ebp'}
    for i in md.disasm(bytes(data), base):
        if i.mnemonic.startswith(('j', 'call', 'ret', 'loop')):
            break
        if i.mnemonic == 'push' and i.op_str in ('ebx', 'esi', 'edi', 'ebp'):
            if i.op_str not in written:
                written.add('saved:' + i.op_str)
            continue
        if i.mnemonic == 'mov' and i.op_str.split(', ')[0] == i.op_str.split(', ')[-1]:
            continue                                   # mov edi, edi: a two-byte nop
        if i.mnemonic in ('xor', 'sub', 'pxor', 'xorps') and len(set(i.op_str.split(', '))) == 1:
            reads, writes = [], [i.op_str.split(', ')[0]]
        else:
            r, w = i.regs_access()
            reads, writes = [i.reg_name(x) for x in r], [i.reg_name(x) for x in w]
        for r in reads:
            r = fam.get(r, r)
            if r in ('eax', 'ebx', 'ecx', 'edx', 'esi', 'edi', 'ebp') and r not in written:
                live.add(r)
        for w in writes:
            written.add(fam.get(w, w))
    return live
