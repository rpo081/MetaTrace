#!/usr/bin/env bash
# Fix the macOS faiss+torch OpenMP conflict for local development.
#
# PyPI's faiss-cpu macOS arm64 wheel bundles its own libomp.dylib while torch
# bundles another; loading both in one process aborts/segfaults (OMP Error #15
# or silent SIGSEGV at CLIP instantiation). Relinking faiss onto torch's
# libomp copy leaves a single OpenMP runtime in the process.
#
# Linux is unaffected (both link system libgomp) — Docker deployments never
# need this. Idempotent: skips work when already relinked. Backups land next
# to the originals as *.pre-omp-fix.
#
# Usage:  scripts/fix_mac_omp.sh [path/to/venv]     (default: .venv)
set -euo pipefail

VENV="${1:-.venv}"
SITE="$(ls -d "$VENV"/lib/python*/site-packages 2>/dev/null | head -1)"
[[ -n "$SITE" ]] || { echo "no site-packages under $VENV" >&2; exit 1; }

FAISS="$SITE/faiss"
TORCH_OMP="$(realpath "$SITE/torch/lib/libomp.dylib")"
TARGETS=("$FAISS/_swigfaiss.abi3.so" "$FAISS/libfaiss.dylib")

for f in "${TARGETS[@]}"; do
  [[ -f "$f" ]] || { echo "missing $f — is faiss-cpu installed?" >&2; exit 1; }
done

if otool -L "$FAISS/libfaiss.dylib" | grep -q "dylibs/libomp.dylib"; then
  echo "backing up originals..."
  for f in "${TARGETS[@]}" "$FAISS/.dylibs/libomp.dylib"; do
    cp "$f" "$f.pre-omp-fix"
  done
  for f in "${TARGETS[@]}"; do
    install_name_tool -change @loader_path/.dylibs/libomp.dylib "$TORCH_OMP" "$f"
    codesign --force --sign - "$f"
  done
  echo "relinked faiss -> $TORCH_OMP"
else
  echo "already relinked; nothing to do"
fi

"$VENV/bin/python" - << 'EOF'
import faiss, numpy as np
idx = faiss.IndexIDMap2(faiss.IndexFlatIP(4))
idx.add_with_ids(np.random.rand(4, 4).astype("float32"), np.arange(4, dtype="int64"))
idx.search(np.random.rand(1, 4).astype("float32"), 2)
print("faiss sanity check OK")
EOF
