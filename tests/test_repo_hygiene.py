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


def test_powershell_scripts_are_ascii():
    """Windows PowerShell 5.1 reads a BOM-less .ps1 in the ANSI code page, so
    one non-ASCII character is mis-decoded on a Persian or Chinese Windows."""
    offenders = [str(p.relative_to(ROOT)) for p in _files(("*.ps1",))
                 if any(b > 127 for b in p.read_bytes())]
    assert not offenders, "non-ASCII PowerShell: " + ", ".join(sorted(offenders))


def test_powershell_scripts_parse(tmp_path):
    import os
    import shutil
    import subprocess

    pwsh = os.environ.get("SENTINEL_PWSH") or shutil.which("pwsh")
    if not pwsh:
        import pytest
        pytest.skip("PowerShell (pwsh) is not installed; set SENTINEL_PWSH to run this")
    script = (
        "$bad = 0; foreach ($f in Get-ChildItem -Path $args[0] -Filter *.ps1 -Recurse) {"
        " $t = $null; $e = $null;"
        " [System.Management.Automation.Language.Parser]::ParseFile($f.FullName, [ref]$t, [ref]$e)"
        " | Out-Null; foreach ($x in $e) { Write-Output ($f.Name + ': ' + $x.Message); $bad++ } };"
        " exit $bad")
    checker = tmp_path / "parse.ps1"
    checker.write_text(script, encoding="ascii")
    result = subprocess.run([pwsh, "-NoProfile", "-File", str(checker), str(ROOT / "deploy")],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_windows_cmd_wrappers_point_at_existing_scripts():
    for cmd in (ROOT / "deploy" / "windows").glob("*.cmd"):
        target = cmd.with_suffix(".ps1")
        assert target.is_file(), f"{cmd.name} wraps a missing {target.name}"
        assert f"%~dp0{target.name}" in cmd.read_text(), cmd.name
