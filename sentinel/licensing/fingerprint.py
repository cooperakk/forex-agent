"""Machine fingerprinting.

A fingerprint identifies the machine a licence is bound to. Two requirements
pull against each other and the balance chosen here matters:

* It must be STABLE. A fingerprint that changes when a container restarts, a
  network interface is renamed, or a disk is resized turns a paying customer
  into a support ticket at 3am -- and the natural fix, "just ignore the
  mismatch", removes the whole control.
* It must be SPECIFIC. A fingerprint that is the same on every cloud VM of the
  same image binds nothing at all.

So this reads several weak signals and combines them with a threshold: a
licence matches if ENOUGH components agree, not if all of them do. Replacing a
network card does not invalidate the licence; copying the install to a
different machine does.
"""

from __future__ import annotations

import hashlib
import os
import platform
import re
import subprocess
import uuid
from pathlib import Path
from typing import Dict, List, Optional

# How many components must match for a machine to be considered the same one.
# Three of the five typical signals is deliberately forgiving: hardware gets
# replaced, and a false refusal is worse for a legitimate user than a false
# accept is for the vendor.
MATCH_THRESHOLD = 3


def _read_first(paths: List[str]) -> Optional[str]:
    for p in paths:
        try:
            value = Path(p).read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if value:
            return value
    return None


def _machine_id() -> Optional[str]:
    """The OS's own stable machine identity, where one exists.

    Linux: /etc/machine-id (systemd) or /var/lib/dbus/machine-id.
    macOS: IOPlatformUUID.
    Windows: MachineGuid from the registry.
    """
    value = _read_first(["/etc/machine-id", "/var/lib/dbus/machine-id"])
    if value:
        return value
    system = platform.system()
    if system == "Darwin":
        try:
            out = subprocess.run(
                ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                capture_output=True, text=True, timeout=5).stdout
            match = re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', out)
            if match:
                return match.group(1)
        except (OSError, subprocess.SubprocessError):
            pass
    elif system == "Windows":  # pragma: no cover - not exercised on Linux CI
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r"SOFTWARE\Microsoft\Cryptography") as key:
                return str(winreg.QueryValueEx(key, "MachineGuid")[0])
        except OSError:
            pass
    return None


def _primary_mac() -> Optional[str]:
    """The first non-loopback, non-virtual MAC address.

    Deliberately skips docker/veth/virbr interfaces: they are regenerated on
    every container start, and including one would make the fingerprint
    unstable for exactly the deployment this software recommends.
    """
    sys_net = Path("/sys/class/net")
    if sys_net.is_dir():
        skip = ("lo", "docker", "veth", "br-", "virbr", "tun", "tap", "wg")
        for iface in sorted(p.name for p in sys_net.iterdir()):
            if iface.startswith(skip):
                continue
            try:
                mac = (sys_net / iface / "address").read_text().strip()
            except OSError:
                continue
            if mac and mac != "00:00:00:00:00:00":
                return mac
    node = uuid.getnode()
    # getnode() sets the multicast bit when it had to invent a random address;
    # a random value is worse than nothing here.
    if (node >> 40) % 2:
        return None
    return ":".join(f"{(node >> shift) & 0xFF:02x}" for shift in range(40, -8, -8))


def _cpu_signature() -> Optional[str]:
    """Model name and core count. Coarse on purpose: it distinguishes machine
    classes, and combined with the others it adds specificity without adding
    fragility."""
    try:
        text = Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="replace")
        model = re.search(r"model name\s*:\s*(.+)", text)
        cores = len(re.findall(r"^processor\s*:", text, re.MULTILINE))
        if model:
            return f"{model.group(1).strip()}|{cores}"
    except OSError:
        pass
    machine = platform.machine()
    cores = os.cpu_count() or 0
    return f"{machine}|{cores}" if machine else None


def _root_fs_id() -> Optional[str]:
    """Filesystem UUID of the root device -- survives a reboot, changes on a
    reinstall or a copy to different storage."""
    try:
        by_uuid = Path("/dev/disk/by-uuid")
        if by_uuid.is_dir():
            root_dev = os.stat("/").st_dev
            for entry in sorted(by_uuid.iterdir()):
                try:
                    if os.stat(entry.resolve()).st_rdev == root_dev:
                        return entry.name
                except OSError:
                    continue
    except OSError:
        pass
    return None


def _hostname() -> Optional[str]:
    name = platform.node().strip()
    return name or None


def fingerprint_components() -> Dict[str, Optional[str]]:
    """Every signal, unhashed, for diagnosis.

    Exposed so an operator whose licence stopped matching can see WHICH
    component changed, rather than being told only that it did.
    """
    return {
        "machine_id": _machine_id(),
        "mac": _primary_mac(),
        "cpu": _cpu_signature(),
        "root_fs": _root_fs_id(),
        "hostname": _hostname(),
    }


def _hash_component(name: str, value: str) -> str:
    return hashlib.sha256(f"sentinel-fx:{name}:{value}".encode("utf-8")).hexdigest()[:16]


def machine_fingerprint() -> Dict[str, str]:
    """Hashed components. Only these are written into a licence.

    Hashing means a licence file does not disclose the customer's hostname, MAC
    address or disk identifiers -- it is a document that may be emailed around.
    """
    return {name: _hash_component(name, value)
            for name, value in fingerprint_components().items() if value}


def fingerprint_matches(bound: Dict[str, str],
                        current: Optional[Dict[str, str]] = None,
                        *, threshold: int = MATCH_THRESHOLD) -> tuple:
    """(matches, matched_count, detail).

    Returns the count and a per-component breakdown as well as the verdict, so
    a refusal can explain itself. A licence bound to fewer components than the
    threshold requires ALL of them to match -- otherwise a licence recorded on a
    minimal container would bind almost nothing.
    """
    now = current if current is not None else machine_fingerprint()
    shared = [k for k in bound if k in now]
    matched = [k for k in shared if bound[k] == now[k]]
    detail = {k: (k in matched) for k in sorted(set(bound) | set(now))}
    required = min(threshold, len(bound)) if bound else 0
    if not bound:
        return True, 0, detail          # an unbound licence runs anywhere
    return len(matched) >= required and required > 0, len(matched), detail
