#!/usr/bin/env python3
"""Build a protected, distributable Sentinel-FX release -- VENDOR SIDE ONLY.

What this does, in order:

1. copies the tree into ``--out`` WITHOUT the vendor-only tools
   (``license_server.py``, this script) and without tests or build clutter;
2. embeds the vendor public key(s) in ``sentinel/licensing/vendor_key.py``,
   so the ``SENTINEL_LICENSE_PUBKEY`` environment variable can no longer turn
   licensing off or substitute a key;
3. with ``--compile``, compiles the licensing, risk, audit and agent modules to
   native extension modules with Cython and DELETES their Python source, so
   there is no readable ``.py`` to edit -- the check lives in machine code;
4. signs ``MANIFEST.sig`` over the protected files AS SHIPPED (the compiled
   binaries when compiled), so a modified or substituted module refuses live
   trading;
5. packs ``sentinel-fx-<version>[-<platform>].tar.gz``.

Compiled modules are platform-specific: build on the same OS, CPU
architecture and Python minor version as the customer (a Windows ``.pyd``
build on Windows, a Linux ``.so`` build on Linux).

The honest limit, stated once more because it is the whole point of the
licensing documentation: native compilation and a signed manifest raise the
cost of tampering from "edit one line" to "reverse-engineer and patch machine
code, then defeat the manifest" -- a real barrier, not an absolute one. The
control a licensee with root cannot remove is the online activation lease
(``--activation-url`` on ``licensegen.py issue``), because its answer comes
from the vendor's server.

Usage::

    python scripts/build_protected.py --version 1.5.0 \\
        --key ./vendor-keys/private.pem --pubkey ./vendor-keys/public.txt \\
        [--lease-pubkey ./lease-keys/public.txt] --compile --out ./release
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import sysconfig
import tarfile
from pathlib import Path
from typing import List

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

#: Never shipped to a customer.
VENDOR_ONLY = {"scripts/license_server.py", "scripts/build_protected.py"}

#: Copied into the release.
INCLUDE = ["sentinel", "scripts", "deploy", "docs", "dashboard/dist", "dashboard/src",
           "dashboard/package.json", "dashboard/package-lock.json",
           "dashboard/index.html", "dashboard/vite.config.ts", "dashboard/tsconfig.json",
           "dashboard/build-artifact.mjs", "requirements.txt", "requirements-dev.txt",
           "pyproject.toml", "README.md", "CHANGELOG.md", ".env.example", "Makefile",
           "Dockerfile", "docker-compose.yml", "var/.gitkeep"]

#: Compiled with --compile. The modules whose edit would change what the system
#: is allowed to do, plus the ones that wire the licence gate in.
COMPILE = [
    "sentinel/licensing/license.py",
    "sentinel/licensing/enforcement.py",
    "sentinel/licensing/integrity.py",
    "sentinel/licensing/fingerprint.py",
    "sentinel/licensing/clock_guard.py",
    "sentinel/licensing/activation.py",
    "sentinel/licensing/vendor_key.py",
    "sentinel/risk/engine.py",
    "sentinel/risk/sizing.py",
    "sentinel/risk/protect.py",
    "sentinel/core/audit.py",
    "sentinel/core/ids.py",
    "sentinel/research/verdicts.py",
    "sentinel/api/security.py",
]


def _ignore(directory: str, names: List[str]) -> List[str]:
    skip = {"__pycache__", ".pytest_cache", ".ruff_cache", "node_modules", ".venv"}
    rel_dir = Path(directory).resolve().relative_to(ROOT) if \
        Path(directory).resolve().is_relative_to(ROOT) else Path(directory)
    out = []
    for name in names:
        rel = (rel_dir / name).as_posix()
        if name in skip or rel in VENDOR_ONLY or name.endswith((".pyc", ".tmp")):
            out.append(name)
    return out


def copy_tree(dest: Path) -> None:
    for item in INCLUDE:
        src = ROOT / item
        if not src.exists():
            continue
        target = dest / item
        target.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, target, ignore=_ignore)
        else:
            shutil.copy2(src, target)


def embed_keys(dest: Path, pubkey: str, lease_pubkey: str) -> None:
    subprocess.run([sys.executable, str(ROOT / "scripts" / "licensegen.py"), "embed-key",
                    "--pubkey", pubkey, *(["--lease-pubkey", lease_pubkey]
                                          if lease_pubkey else []),
                    "--target", str(dest / "sentinel" / "licensing" / "vendor_key.py")],
                   check=True)


def compile_modules(dest: Path, modules: List[str]) -> List[str]:
    """Cythonize each module in place, delete its source, return shipped paths."""
    try:
        from Cython.Build import cythonize  # noqa: F401
    except ImportError as exc:
        raise SystemExit("--compile needs Cython: pip install 'cython>=3.0' "
                         "setuptools, and a C compiler") from exc
    setup_py = dest / "_build_protected_setup.py"
    setup_py.write_text(
        "from setuptools import setup\n"
        "from Cython.Build import cythonize\n"
        f"setup(ext_modules=cythonize({modules!r}, language_level=3,\n"
        "      compiler_directives={'embedsignature': False, 'binding': True,\n"
        "                           'emit_code_comments': False,\n"
        # Plain Python semantics. With annotation typing on, Cython enforces
        # `Optional[int]` on arguments whose default is a sentinel object and
        # the licence module fails to import.
        "                           'annotation_typing': False},\n"
        "      quiet=True), script_args=['build_ext', '--inplace'])\n",
        encoding="utf-8")
    try:
        subprocess.run([sys.executable, setup_py.name], cwd=dest, check=True)
    finally:
        setup_py.unlink(missing_ok=True)
        shutil.rmtree(dest / "build", ignore_errors=True)
    suffix = sysconfig.get_config_var("EXT_SUFFIX") or ".so"
    shipped: List[str] = []
    for rel in modules:
        src = dest / rel
        binary = src.with_name(src.stem + suffix)
        if not binary.is_file():
            raise SystemExit(f"compilation produced no {binary.name} for {rel}")
        src.unlink()
        src.with_suffix(".c").unlink(missing_ok=True)
        shipped.append(binary.relative_to(dest).as_posix())
    return shipped


def sign_manifest(dest: Path, key_pem: str, version: str, compiled: List[str]) -> None:
    from sentinel.licensing.integrity import PROTECTED_PATHS, build_manifest

    compiled_map = {Path(p).parent.as_posix() + "/" + Path(p).name.split(".")[0] + ".py": p
                    for p in compiled}
    paths = [compiled_map.get(p, p) for p in PROTECTED_PATHS]
    text = build_manifest(dest, key_pem, paths=paths, version=version)
    (dest / "MANIFEST.sig").write_text(text, encoding="utf-8")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", required=True)
    ap.add_argument("--key", required=True, help="licence PRIVATE key (signs the manifest)")
    ap.add_argument("--pubkey", required=True, help="licence public key (file or base64)")
    ap.add_argument("--lease-pubkey", default="", help="activation-lease public key")
    ap.add_argument("--compile", action="store_true", help="compile protected modules")
    ap.add_argument("--out", default="./release")
    args = ap.parse_args(argv)

    tag = f"{platform.system().lower()}-{platform.machine().lower()}" if args.compile else ""
    name = f"sentinel-fx-{args.version}" + (f"-{tag}" if tag else "")
    out_root = Path(args.out).resolve()
    dest = out_root / name
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    copy_tree(dest)
    embed_keys(dest, args.pubkey, args.lease_pubkey)
    compiled = compile_modules(dest, COMPILE) if args.compile else []
    sign_manifest(dest, Path(args.key).read_text(encoding="utf-8"), args.version, compiled)
    archive = out_root / f"{name}.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(dest, arcname=name)
    os.chmod(archive, 0o644)
    print(f"release  -> {archive}")
    print(f"compiled -> {len(compiled)} module(s)" if compiled else "compiled -> none "
          "(pure Python; pass --compile for native modules)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
