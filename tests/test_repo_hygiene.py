"""Repository hygiene that is really runtime correctness.

A shell script with CRLF line endings does not run on Linux at all: the
kernel reads the shebang as ``bash\\r`` and the installer fails before its
first line. That is exactly how 1.3.0 and 1.4.0 shipped
``deploy/scripts/install.sh``. These tests make the failure impossible to
reintroduce from a Windows checkout.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_LF_ONLY = ("*.sh", "*.py", "*.service", "*.timer", "*.conf")


def _files(patterns):
    for pattern in patterns:
        for path in ROOT.rglob(pattern):
            parts = set(path.relative_to(ROOT).parts)
            if parts & {"node_modules", ".venv", ".git", "dist", "__pycache__"}:
                continue
            yield path


def test_no_crlf_in_files_executed_on_linux():
    offenders = [str(p.relative_to(ROOT)) for p in _files(_LF_ONLY)
                 if b"\r\n" in p.read_bytes()]
    assert not offenders, (
        "these files have CRLF line endings and will not run on Linux: "
        + ", ".join(sorted(offenders)))


def test_shell_scripts_have_a_shebang():
    missing = [str(p.relative_to(ROOT)) for p in _files(("*.sh",))
               if not p.read_bytes().startswith(b"#!")]
    assert not missing, "shell scripts without a shebang: " + ", ".join(sorted(missing))


def test_shell_scripts_parse():
    import shutil
    import subprocess

    bash = shutil.which("bash")
    if bash is None:  # pragma: no cover - Windows without Git Bash
        import pytest
        pytest.skip("bash is not available")
    for path in _files(("*.sh",)):
        result = subprocess.run([bash, "-n", str(path)], capture_output=True, text=True)
        assert result.returncode == 0, f"{path.relative_to(ROOT)}: {result.stderr}"
