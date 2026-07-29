"""Make the src/ layout importable when running pytest from anywhere.

Each of these packages is published as its OWN repository, so this file is deliberately
self-contained: it must work in a bare clone with no parent checkout and no editable install.
It only puts `src/` on the path -- it does NOT stub, mock, or shim anything, so a genuine
packaging break still fails the cold-install check in oss/tools/check_cold_install.py.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))
