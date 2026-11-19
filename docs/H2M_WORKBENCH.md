# h2m: a matching workbench

`tools/h2m.py` is a command-line workbench for the last part of matching a
function: the source compiles, the score is small, and the question is which
spelling makes the compiler emit retail's bytes. It scores whole-file variants
of a source file against retail without touching the repository, explains the
difference, remembers every variant it has scored, and can search source
rewrites on its own.

It was written while matching the Havok functions described in
[MATCHING_WITH_LANES.md](MATCHING_WITH_LANES.md) and it adds to
[DECOMPILING.md](DECOMPILING.md); `tools/check.py` stays the judge of what
matches. This branch holds the source-level part: everything here works on C++
source text, on object files and images our own build produces, and on the
retail bytes of your own `default.xbe`.

Back-end tracing and forced-decision tools exist locally and are not part of
this branch pending the maintainers' decision.

## Requirements

- What `tools/check.py` needs: Python 3 with `pip install -r requirements-dev.txt`
  (capstone), your own retail `default.xbe` (`orig/default.xbe` or `RETAIL_XBE`),
  and a toolchain you obtained lawfully (`sdk/xbox` or `XDK_DIR`), on Windows or
  on Linux under Wine. The workbench calls the compiler only through
  `tools/build.py` (`build.run_tool`, `build.file_flags`) and adds no binary.
- Nothing else. The ledger is SQLite from the standard library.
- For game functions (image backend): a full build of the tree
  (`python tools/build.py`), because the reduced image reuses its objects and
  link map.

Environment variables, all optional:

| variable | default | meaning |
| --- | --- | --- |
| `H2M_ROOT` | the repository `tools/h2m.py` is in | the tree to work on (another checkout or worktree) |
| `H2M_OUT` | `<tree>/build/h2m` | work files, variants, caches, scratch roots (`build/` is ignored by git) |
| `H2M_DB` | `<H2M_OUT>/h2m.db` | the ledger; point several trees at one file to share it |
| `H2M_TU` | `1` | `0` compiles a Havok file alone instead of through the translation-unit tail `tools/build.py` adds |
| `H2M_SCRATCH` | `main` | image backend: scratch root id |
| `H2M_EXTRA` | remembered per function | image backend: callees kept in the reduced image |
| `HALO2_TOOL_TIMEOUT` | as `tools/build.py` | seconds per compiler call |

Nothing limits memory or the number of jobs for you. On a shared machine wrap
the command in whatever you use for that (a job limiter, `timeout 300`); one
object-backend compile takes about a second and little memory, one image-backend
variant is a whole-program link.

## Layout

| file | what |
| --- | --- |
| `tools/h2m.py` | the command dispatcher |
| `tools/h2m/project.py` | roots, `functions.csv` rows, retail bytes, body splicing, the file lock |
| `tools/h2m/objcmp.py` | compile one `/GL-` source, read the functions out of the COFF object with relocation fields masked, compare with retail |
| `tools/h2m/measure.py` | pick the object function that stands for a marker; aligned distance |
| `tools/h2m/asm.py` | one disassembler and one normaliser with masking levels |
| `tools/h2m/metrics.py` | the metric vector of a variant |
| `tools/h2m/ledger.py` | the SQLite record of every variant |
| `tools/h2m/scorer.py` | object backend: compile, pick, measure, record |
| `tools/h2m/image.py` | image backend for whole-program (LTCG) game functions |
| `tools/h2m/classify.py` | diagnosis: difference classes, register map, hints, side-by-side |
| `tools/h2m/corpus.py` | index of the retail code of every matched function |
| `tools/h2m/mutators.py`, `local_mutations.py` | the source techniques below as enumerations over a function body |
| `tools/h2m/search.py` | beam search over those mutators |
| `tools/h2m/probe.py` | diagnostic perturbations (never for submitted source) |
| `tools/h2m/phen.py` | find a retail instruction pattern in matched functions and reduce the source that shows it |
| `tools/h2m/webs.py` | per register and stack cell: where retail keeps a value and where ours does |
| `tools/h2m/run_check.py` | `tools/check.py` for Havok-only work when the toolchain folder has no `d3d8ltcg.lib` |
| `tools/h2m/gamecands.py` | list unmatched game functions by baseline score, outside people's claims |

## Variants

A variant is a whole-file copy of the function's `.cpp` with your change,
written anywhere outside `src/` (the convention is `<H2M_OUT>/<va>/name.cpp`).
The workbench compiles it from there against the tree's `include/` and `src/`,
so the repository is never edited while you experiment, and several people or
agents can work on one checkout. A header change cannot be tried from a variant
file: paste the changed header text at the top of the variant instead.

## The numbers

Every variant gets a vector, not one number:

- `lock`: the lockstep count `tools/check.py` prints plus the length
  difference. It is positional: one instruction inserted early counts
  everything after it.
- `dist`: aligned edit distance between the two instruction listings.
- `sem`: multiset difference of the instructions with registers, stack slots
  and labels masked: operations one side has and the other lacks, whatever
  their order.
- `frame`: our prologue's `sub esp` minus retail's.
- `first`: retail offset of the first aligned difference.
- `MATCH`: byte match with relocation fields masked.

Steer by `sem` and `dist`, confirm by `lock`, and let `tools/check.py --fast` on
the function and on every other marker of its file be the judge. `lock` is not
comparable with the project score for functions that end in jump tables.

## Commands

VA is hex, with or without `0x`. All examples run from the repository root.

| command | example | what it prints |
| --- | --- | --- |
| `score VA [FILE...]` | `python tools/h2m.py score 2daae0` | the vector of the tree's source, or of each whole-file variant given |
| `diff VA [FILE]` | `python tools/h2m.py diff 30ed40` | vector, difference classes, retail-only and ours-only instructions, the retail-to-ours register map, hints, then the aligned side-by-side (retail left, `!!` = differs) |
| `try VA VARIANTS.txt [--start BASE.cpp]` | `python tools/h2m.py try 30ed40 build/h2m/30ed40/v.txt` | one line per function variant; variants are separated by a line `=====`, each from the signature line to the closing `}` in column 0 |
| `history VA` | `python tools/h2m.py history 30ed40` | the best variants in the ledger, with their files |
| `triage [VA...] [--owner P] [--limit N]` | `python tools/h2m.py triage --limit 20` | the `diff` header of every open function of an owner (default `third:havok`), best first |
| `idiom VA [FILE]` | `python tools/h2m.py idiom 30ed40` | each instruction only retail has, looked up in matched functions of the same owner: read their source for the spelling |
| `exemplars VA [FILE]` | `python tools/h2m.py exemplars 2fe350` | matched functions whose retail code contains retail's sequence at each divergent region, and the nearest matched functions overall |
| `asm FILE... [--sym S]` | `python tools/h2m.py asm build/h2m/lab/t.cpp --sym probe` | compiles any standalone source (first line `// @flags /O2 /GL-`) and prints each function's code: the lab for small experiments |
| `retail VA [LO HI]` | `python tools/h2m.py retail 2daae0 0 40` | the raw retail listing of your XBE, nothing masked, float immediates and float constants decoded |
| `search VA [--start F] [--time S] [--beam N] [--only m1,m2]` | `python tools/h2m.py search 30ed40 --time 600` | beam search over the mutators; improvements are reduced to the hunks that matter and written to `<H2M_OUT>/<va>/search/` |
| `probe VA [FILE] --spec S` or `--auto` | `python tools/h2m.py probe 2fb150 --auto` | diagnostic perturbations, see below |
| `phen PATTERN` or `--at VA:START-END` `[--reduce N]` | `python tools/h2m.py phen 'mov eax, dword ptr [*]; dec eax; js *'` | matched functions whose retail code shows the pattern; `--reduce` shrinks their source to the minimum that still shows it |
| `webs VA [FILE] [--all]` | `python tools/h2m.py webs 30ed40` | per register web and stack cell: retail's location, ours, where they part; consistent renamings grouped (`PURE SWAP esi<->edi`, `ROTATION`, a constant slot shift) |
| `scratch [show\|rm]` | `python tools/h2m.py scratch` | where the image backend's scratch root is; `rm` deletes it |
| `fastcheck VA... [--tree DIR] [--dry]` | `python tools/h2m.py fastcheck 2daae0 --dry` | `tools/check.py --fast` for the functions, one run at a time per tree |

`score`, `diff`, `try`, `triage`, `exemplars` and `idiom` take
`--backend auto|object|image`; `auto` picks by the function's owner in
`config/functions.csv`, as `webs` always does. `--sym NAME` chooses the object function by a substring
of its decorated name: check it when a helper's name contains the address of
the function you are scoring.

The fastest way to use `score` is to generate a family of variants with a
few lines of Python (`itertools.product` over independent choices) and score
them in one call. The ledger answers a repeated text at once.

### probe

A probe adds code retail does not have (a store of a variable into an external
array, a dummy scratch value, `volatile`, an escaped address) to measure what a
register or slot assignment depends on. Probe variants are never candidates for
the repository. The probe's own instructions are found through their
relocations and removed before comparing, so every variant has a raw vector and
a net one; `NET-MATCH` means retail's code plus only the probe instruction.

Spec lines (`&` joins several into one variant):

- `use VAR xK @WHERE [as store|add|touch]`: K counted uses of VAR. WHERE is a
  file line, `+N` (body line), `top`, `end` or `dD` (next to VAR's first use at
  loop depth D).
- `unuse VAR`: forms that lower VAR's use count.
- `scratch xK @WHERE`: K dummy scratch values live across the statement.
- `decl-order A B C`: every order of those declarations (legal source: a hit is
  a real candidate).
- `volatile VAR`, `slot VAR`, `raw @WHERE: text`, `replace @WHERE: text`,
  `delete @WHERE`.

`--auto` takes rival register pairs from the diff's register map, locates each
variable with a one-use probe, then titrates one to four extra uses at each
loop depth. A `NET-MATCH` from `use X x1` reads: X has one more use at that
depth in whatever source retail was built from. Look for a spelling that
mentions X once more without emitting code (an inline helper taking X by value,
a comparison that folds away). `0x2fb150` was matched from that reading: passing
the two shape kinds through one `__forceinline` helper gave the missing use.

## Game functions: the image backend

Game functions are built with `/GL`. An object file holds no code for them; the
function exists only after the whole-program link, and its shape depends on
callers and callees. For them the workbench scores on the reduced image that
`tools/check.py --fast` builds:

1. A private scratch copy of the tree is made once under
   `<H2M_OUT>/scratch/<id>/` with `permute.prepare_scratch` (sources, headers,
   config, the full build's objects and map), and brought back in line with the
   tree at the start of every command.
2. The variant text is written over the function's source in the scratch, and
   `check.fast_check` builds and checks the reduced image there. The tree's own
   `build/` is never touched.
3. `lock` and `MATCH` are that check's own numbers (its `report-fast.json`
   entry), so they equal what `python tools/check.py --fast VA` would print for
   the same sources. `dist`, `sem`, `frame` and `first` are computed from the
   function's bytes read back from the reduced image, with the image's address
   fields as the mask.
4. The pristine source is written back, whatever happened.

One build at a time per scratch (a file lock); use one scratch id per person or
agent. A variant costs one reduced link, 10 to 30 seconds. Ledger entries of
the image backend are keyed by the baseline build and a digest of the headers,
so a new full build or a header edit starts a new cache.

`--extra VA,VA` keeps more callees in the reduced image. It is needed when the
reduced-image score is far from the score of the full build: the full build
shapes the function by callees the reduced image leaves out (`0x243c00`: 198
without, 2 with five callees kept). Candidates are the callees named in the
`CAVEAT` lines that `diff` prints and the function's `calls` column. The list
is remembered per function.

A `CAVEAT` line means the reduced image may build the function differently
from the full build. The full check stays the judge. `search`, `probe`, `phen
--reduce` and `asm` are object-backend tools.

## The multiplexed sweep

A full build and check of the game takes minutes, and it is the only honest
gate for whole-program code: a change to one function can move others through
inlining and the link. Spending one full build on one variant of one function
is therefore slow. The sweep spends it on many functions at once.

- Pick K open functions for a round such that each sits in a different source
  file and none is in another's call neighbourhood (callers and callees, the
  `calls` column of `config/functions.csv`). Their scores then do not interact.
- Write one mutant of each into its file, run one full build and one full
  check, and compare the report with a saved baseline function by function.
- Each target's gain or loss is read from its own row. Every other function
  whose score moved is recorded and attributed to the target in whose
  neighbourhood it lies, so the same report is the regression gate.
- A mutant that does not compile is put back with its pristine object before
  the link, and the round goes on without it.
- A mutant that lowers its function's score with no loss elsewhere becomes the
  base the next mutants of that function are made from. Mutation kinds are
  drawn in proportion to their recorded success rate.
- Every match and every large gain is confirmed alone (one function, one
  build) before anyone proposes it.

`tools/h2m/gamecands.py` makes the candidate list a sweep starts from. The
sweep driver itself is not on this branch: the current one is tied to a local
build setup and is still changing.

## Source techniques

Each line is a source shape that produced a match or a large drop, with a
function where it can be checked. The first group is also enumerated by
`tools/h2m/mutators.py`, which is what `search` runs.

Shapes that point at their source:

1. Retail updates memory in place (`dec dword ptr [reg+N]`) where ours loads,
   changes and stores: `long &size = points.size; --size;` (`0x31bc90`).
2. Retail forms a member's address and reads through it (`add reg, N; mov reg,
   [reg]`): the member is passed by reference to an inline helper (`0x2e3370`,
   `0x2daae0`). An index evaluated before the object is the argument evaluation
   order of such a helper.
3. Reciprocal then multiply (`fdivr [1.0]; fmulp`) where ours divides once: a
   named reciprocal.
4. A call argument read into a local declared at the right place took
   `0x2daae0` from 31 to a match. Sweep which reads become locals and where
   they are declared.
5. Retail puts the shared loop step right after the first branch and later
   branches jump back to it: write the step in each branch and the compiler
   merges them into the first (`0x3072f0`, 97 to 20).
6. A loop helper called with a count of one whose retail code has a different
   operand order is a separate single-element helper (`0x306400`).
7. A base pointer read once per pass (`T *base = array; base[i] = base[i +
   1];`) fixes `lea [eax+ecx]` against `[ecx+eax]` (`0x31bc90`).
8. A struct with a zeroing constructor gives one zero register for several
   zero stores.
9. Retail without `movsx`: the local is `long`, not `short`.
10. Temporaries and the call using them in a bare `{}` block; a member read
    into a named const local right before its use; `_mm_store_ps` and
    `_mm_load_ps` to force a store or a reload; parenthesised float products; a
    result variable declared before a loop (a dead stack store); a count-first
    index search helper.
11. Retail forms `base + K` early in its own register (`lea esi, [eax+0x30]`)
    where ours folds the offset late: bind the member with a constant reference
    to the pointer, `unsigned char const *const &nodes = tree->nodes;`
    (`0x3008d0`, `0x3028c0`). A by-value local is folded back. Its position
    among the opening statements matters.
12. When only registers or order differ, run `exemplars` and rewrite the body
    statement by statement in the style of a matched function with the same
    retail shape: which values are named, which variables are reused for later
    terms, pointer locals for outputs, operand order of the final adds
    (`0x2fe350` from `0x1ce210`: 90 statement orders of the old style compiled
    the same, the exemplar's style matched).
13. A guard that repeats a loop's own entry test, `if (0 < count) { for (i = 0;
    i < count; i++) ... }`, emits no code and can change which values get esi,
    edi and ebx in the whole function (`0x2e5590`). The spelling matters: `0 <
    count` worked; `count > 0`, `count != 0` and `count` added code.

The reference barrier (a local with a reference or pointer bound to it is not
substituted back into the expressions that use it):

14. It keeps two sub-expressions apart that would fold into one tree
    (`0x2ff210`, `0x2ff570`), sets the destination of an SSE operation
    (`0x306b90`), keeps a pointer chain in one register (`0x316110`), and keeps
    a value's register occupied past its last use so the next value goes
    elsewhere (`0x319230`). Helpers taking `long &` fold back.
15. A barrier that is not needed can be what breaks retail's assignment: try
    removing barriers as well as adding them (`0x2ddbd0`).
16. A float store scheduled before an integer operation that retail does
    first: read the float's bits through a reference bound to the float local
    (`0x307840`).

Helpers, accessors and constructors:

17. A trivial accessor or by-value setter shifts the scratch rotation without
    emitting code (`0x3173f0`). Sweep accessor and setter combinations when
    only a scratch register differs.
18. A loop bound or start taken through a trivial file-local accessor that
    takes a const reference fixed scratch registers swapped inside a loop
    where a hundred local spellings did not (`0x2e3b00`).
19. An accessor used for the loop bound gives `mov eax, [mem]; cmp eax, ecx`
    instead of `cmp [mem], eax`.
20. A call through a table entry scheduled wrongly: fetch the entry through an
    inline accessor returning the function pointer (`0x316dd0`).
21. A file-local static helper's parameter order sets the order in which the
    caller sets up its register arguments (`0x30d480`).
22. A constructor body, not an initialiser list, in store order; a field read
    through the stored member is pinned after that store, one read through the
    parameter is free to move (`0x2fe830`).
23. Retail loads several values into registers before a sum that ours folds:
    a derived struct whose constructor takes them as parameters; the parameter
    order matters.
24. A member address in the wrong register with stores in the wrong order can
    be a derived-to-base conversion: make the member a base class and pass
    `this`.
25. A fresh `xor reg, reg` on a failure path with the result pointer in eax:
    the body is a `__forceinline` member returning a literal, and the outer
    function returns the pointer it was given.
26. Naming a copy-constructor source as a reference (`T const &source =
    *p->q; T copy(source);`) moves the argument load by one scratch register.
27. A global read through a file-local inline accessor is loaded earlier than
    a direct read (`0x2ff8b0`, 197 to 132).
28. `mov rB, rA` right after `lea rB, [..]; mov rA, [rB+N]` is a second source
    read of the same element: write it through the original array expression,
    not through the reference or a local.

Statements, loops and blocks:

29. Moving a loop's final `return` inside an endless loop (`for (;;) { ...; if
    (--tries <= 0) return 0; }`) moves the shared return block (`0x307c40`).
30. `do { ...; goto outside; } while (--n >= 0)` rewritten as `if (n >= 0) {
    for (; n >= 0; n--) { ...; return 0; } }` gave retail's loop bottom and
    result-block order in every inlined copy.
31. Failures leaving a loop by `break` with one `if (failed != 0) { restart }`
    after the loop place the shared failure block after the restart code;
    `goto restart` from inside does not (`0x309b50`, 441 to 104).
32. `x = (float)a - b;` split into `x = (float)a; x -= b;` moves where an
    integer operation or a cursor step lands (`0x301a60`, 21 to 9).
33. A float sum written as one expression is put in the compiler's own order,
    so permuting its terms does nothing; written as `x = p1; x += p2; ...` it
    keeps the source order (`0x306580`, nine of nine x87 sums).
34. Operand order of commutative intrinsics and float operators is the only
    spelling inside a block that changes code (`0x2de1b0`, 150 to 12 from
    `_mm_mul_ps(b, scale)` and `_mm_add_ps(second, first)`). Count the
    instructions of each block against retail first (padding can hide a
    different count), then enumerate the commutations of that block's products
    and sums. Integer `+ & |` and pointer-add commutation never changed code;
    flipping a comparison did.
35. Replacing an `_mm_store_ps`/`_mm_load_ps` round trip with a plain
    assignment plus `T const &ref = local;` references (`0x306400`).
36. Field-wise fill of a local struct followed by a whole-struct assignment
    gives `lea` and `[reg+4]` reads (`0x3124c0`).
37. The emission order of an OR chain of byte loads follows the base the
    offsets are relative to, not source order or tree shape; an `x ^= key;` in
    its own statement turns it into a Horner chain (`0x31c7d0`).
38. Shift count `mov ecx, eax` rather than `mov cl, al`: keep the byte as a
    byte variable and widen both the switch value and the count from it.
39. Per-group block scopes with their own locals move stack slots (`0x306580`,
    149 to 101).
40. A commutative x87 product or compare whose two operands come out swapped:
    the order depended on how many initialised locals were defined before the
    local's own definition. Folding three one-use locals into their uses
    matched `0x306f80`; moving the declaration alone did nothing.
41. Two words passed to an inline helper both by reference, assigned in the
    same order in both arms of an `if`/`else`, decided which one took a dead
    argument's home and which a frame cell (`0x315340`).

Translation-unit effects (these need a maintainer, because they change files):

42. `tools/build.py` compiles a Havok file through a generated unit that ends
    with `#pragma auto_inline(off)` and `#pragma inline_depth(0)`. Implicit
    template specialisations are instantiated at the end of the unit, after
    those pragmas, so a template helper is not inlined there even when it is
    `__forceinline`, while it is when the file is compiled alone. The object
    backend compiles through the same tail for that reason (`H2M_TU`).
43. Retail reloading pointers after every store where ours caches them: the
    callee was in another translation unit in retail (`0x318c10`, `0x31be80`).
44. Arguments arriving in eax/ebx with caller cleanup: a `static` function
    whose callers are all in the same unit (`0x303540`). `__fastcall` is not
    it.
45. A store to an address-taken local stays ahead of later loads through
    pointers read from memory, unless every callee that receives the local's
    address is defined earlier in the same file (`0x3072f0` matched with
    unchanged source once the callees' files were concatenated ahead of it).
46. A user-written destructor, even an empty one, stores the class vtable; an
    implicit one does not, and it is emitted only in the unit that constructs
    the object (`0x2d8510`).
47. In `/GX` files, `throw()` on a callee's declaration tells the caller it
    cannot throw without merging files (`0x2de1b0`: a stack home fixed, first
    divergence from +0x2c to +0xd8).
48. Compiler flags are settled: about fifty flag sets on 24 open Havok
    functions gave identical or far worse code (exceptions: `0x315620` `/Op`,
    the `/GX` files). Sibling order inside a file, helper linkage and the
    presence of siblings were neutral.

Reading a diff:

- Find the first divergence and the retail-only instructions, and ask what
  source would make the compiler emit exactly that: an extra load is a
  separate read; an address formed is a reference; a different block order is
  a different statement structure; a different frame slot is a different local
  or scope.
- Register-only differences usually follow from which values are named locals
  and from helper shapes. Reordering statements rarely helps.
- `das`, `add byte ptr [eax], al` and similar at the end of a listing are
  jump tables decoded as code.

## Compiler behaviour seen in the generated code

These are observations of MSVC 7.1 (13.10.3077) output under this project's
flags. Most were made by compiling small sources and many variants and
comparing the code. The rules about the number of register candidates in a
block, about a shared constant's uses and reloads, and about reload order were
first seen with the local back-end tracing tools mentioned above, and then
confirmed by the matches they led to (`0x30e720`, `0x31be80`). None is taken
from any documentation, and a rule that held many times can still fail: check
it against the listing. Where this section differs
from the "weighted use counts" paragraph of
[MATCHING_WITH_LANES.md](MATCHING_WITH_LANES.md), this is the later
measurement.

Callee-saved registers (which long-lived value gets esi, edi, ebx, ebp):

- A value's priority grows with its uses, each weighted by 2 to the power of
  its loop depth. Conditionals do not change the weight of a use, and neither
  does the way a loop is spelled.
- The term a block contributes also scales with the number of register
  candidates referenced in that block. A named local that holds a load result
  and is used across instructions is a candidate. A named sum or size that
  folds into its uses is not. The return value of an inline helper is a
  candidate; parameters that fold are not.
- An `if` placed inside a value's live range, with no use of the value in it,
  lowered that value's priority; the same `if` outside the live range had no
  effect (`0x2e5590`).
- Equal priorities go to the value whose last definition comes first; for
  single-definition values, the one declared first. Prologue push order says
  nothing about it.
- Extra uses do not move a value steadily up through ebx, edi, esi
  (`0x2fb150`: one extra use of one parameter gave ebx, two edi, three esi,
  while one to four extra uses of `this` left it in ebx).
- So to move a value from one callee-saved register to another: give it one
  more weighted use than its rival, take one from the rival (often a temporary
  at loop depth two), or change how many candidates its blocks reference.

A shared constant kept in a register (typically zero):

- A store of the constant to memory counts as a use. Compares against it and
  register copies of it do not.
- When uses minus reloads is not positive, the compiler reloads the constant
  at each use instead of keeping it in a register.
- An unfused compare against zero is one more use and leaves the instructions
  the same. A loop start read through a size accessor that takes a const
  reference produces one.

Values split between a register and memory:

- When a value is reloaded on a control-flow edge together with others, the
  reloads appear in the order in which the pieces came into being. A named
  local declared between two others can flip that order.
- A value retail keeps in a scratch register with a stack home is not reached
  by adding uses (`0x31be80`, up to eight): that difference is a split value,
  not a priority contest.

Scratch registers:

- eax, ecx, edx are handed out in turn, skipping busy ones. A value that needs
  a fixed register (a call receiver in ecx) takes it without advancing the
  turn. A scratch swap at one call therefore often depends on how many scratch
  values were allocated earlier in the function, not on code near the call.
- Of two independent values, the one used last is allocated first; load order
  in the source does not decide.
- XMM: the lowest free register, the right operand of an intrinsic first, the
  result in the left operand's register. A `_mm_set_ss(constant)` named as a
  local is a real operation and its declaration order sets the registers;
  plain loads and named copies move nothing.
- A global 16-byte constant is loaded at the top of its block in every
  spelling; a local in memory is loaded late, at its use.

Stack layout:

- Four-byte cells (address-taken scalars, spills, argument homes) are at the
  bottom, aggregates above them in ascending size. With equal sizes, more
  references in the source text (no loop weighting) means a lower address,
  then the earlier last reference.
- Declaration order, names and first use of uninitialised locals move no slot.
- Locals whose address escapes share a slot only across disjoint `{}` scopes;
  arrays and spills that do not escape share by liveness. Spills reuse dead
  parameters' argument homes first, lowest address first.
- A one-byte struct result used through an inline member is laid out as an
  aggregate; read as a field it is a narrow scalar at the bottom.
- A copy into a `const` local passed by address in two places lets the
  compiler reuse a computed vector across the stores in between (`0x306580`,
  250 to 149).

Folding:

- A test against a sentinel that an inlined helper stored is folded away.
  Retail not folding it means the value was not trackable in that caller. A
  float to double to float round trip inside a bool helper hides a stored
  float constant from that folding (`0x2f63d0`).
- Store operand order `[cursor+offset]` against `[offset+cursor]` in
  strength-reduced loops followed function-wide structure (code after the
  loop, grouping of frame locals), never the loop's own spelling (`0x2f3030`
  family, open).

Transplanting:

- Copying the helper shape of a matched sibling word for word, redundant
  tests included, transplants its code generation into the function that
  inlines the copy. When a sibling with the same retail shape is matched,
  start from its helpers before trying anything else.

## Patterns found with `phen`

| retail pattern | source that produces it |
| --- | --- |
| `mov r, [m]; dec r; js` | `for (int k = array->size - 1; k >= 0; k--)` with the member read inside the loop's initialiser (`0x2dddb0`, `0x3158d0`) |
| `dec r; jns` | the close of the same loop |
| `add r, x; jns` | a sum tested as one expression, `if ((steps[k] + value) < 0)` (`0x2e1640`) |
| `sub r, x; jns` | game code only: `PIN(time - update_time, 0, max)` (`0x124950`) |
| `mov r1, r2; cmp r1, [m]` | game code only: a call result compared and then consumed by a `switch` (`0x2c6170`) |

## Source rules

The workbench will happily score source the project does not accept.
"What not to do" in [DECOMPILING.md](DECOMPILING.md) and "Real source or a
score steer?" in [MATCHING_WITH_LANES.md](MATCHING_WITH_LANES.md) still hold
for anything proposed. The matches made with it kept to: no `volatile`, no
`__declspec(noinline)`, no pragmas in bodies, no argument swaps at calls, no
dead locals, names that describe behaviour. `search` leaves out the permuter
mutations that break those rules, and flags a result that adds a one-use local
without reaching a match. After a win, score every other `// @retail`
marker of the file against the variant and against the tree's file: nothing
else may get worse.
