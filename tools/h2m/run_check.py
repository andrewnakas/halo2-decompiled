"""Runs tools/check.py for code that needs no Xbox SDK library (Havok: /GL-,
standard calling conventions) when XDK_DIR holds a compiler but no
d3d8ltcg.lib: the D3D member objects tools/build.py extracts from that library
are then skipped. With the library present this is tools/check.py unchanged.

usage (from the repository root): python tools/h2m/run_check.py [check.py arguments]
H2REPO names another tree to check (default: this repository)."""
import os, sys

repo = os.path.abspath(os.environ.get('H2REPO')
                       or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(repo, 'tools'))
os.chdir(repo)
import build, xbe  # noqa: E402

if not os.path.isfile(os.path.join(xbe.xdk_dir(), 'lib', build.D3D_LIBRARY)):
    build.d3d_objects = lambda root, xdk: []
import check  # noqa: E402

sys.argv[0] = 'check.py'
sys.exit(check.main())
