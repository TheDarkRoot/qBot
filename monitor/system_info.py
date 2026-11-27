from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional


def _read_meminfo() -> Dict[str, int]:
    """Read /proc/meminfo values in bytes on Linux/Entware/OpenWrt/RPi."""
    info: Dict[str, int] = {}
    path = Path("/proc/meminfo")
    if not path.exists():
        return info
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            parts = line.split(":")
            if len(parts) != 2:
                continue
            key = parts[0].strip()
            tokens = parts[1].strip().split()
            if tokens and tokens[0].isdigit():
                info[key] = int(tokens[0]) * 1024
    except OSError:
        pass
    return info


def _read_uptime_and_load() -> Dict[str, Any]:
    uptime_sec = 0.0
    load_str = "—"
    uptime_path = Path("/proc/uptime")
    if uptime_path.exists():
        try:
            first = uptime_path.read_text(encoding="utf-8").strip().split()[0]
            uptime_sec = float(first)
        except (OSError, ValueError, IndexError):
            pass

    load_path = Path("/proc/loadavg")
    if load_path.exists():
        try:
            tokens = load_path.read_text(encoding="utf-8").strip().split()[:3]
            if tokens:
                load_str = " / ".join(tokens)
        except OSError:
            pass

    if uptime_sec <= 0.0 and hasattr(os, "getloadavg"):
        try:
            l1, l5, l15 = os.getloadavg()
            load_str = f"{l1:.2f} / {l5:.2f} / {l15:.2f}"
        except OSError:
            pass

    return {"uptime_sec": int(uptime_sec), "load": load_str}


def _resolve_block_device(path_str: str) -> Optional[str]:
    """Resolve a mounted filesystem path to its parent block device (e.g. /dev/sdb)."""
    mounts_path = Path("/proc/mounts")
    if not mounts_path.exists() or not path_str:
        return None

    target = os.path.realpath(path_str)
    best_dev: Optional[str] = None
    best_len = -1

    try:
        for line in mounts_path.read_text(encoding="utf-8", errors="replace").splitlines():
            parts = line.split()
            if len(parts) < 2:
                continue
            dev, mnt = parts[0], parts[1]
            if not dev.startswith("/dev/"):
                continue
            mnt_clean = mnt.rstrip("/") or "/"
            matched = (
                target == mnt_clean
                or (mnt_clean == "/")
                or target.startswith(mnt_clean + "/")
            )
            if matched and len(mnt_clean) > best_len:
                best_len = len(mnt_clean)
                best_dev = dev
    except OSError:
        return None

    if not best_dev:
        return None

    if re.search(r"(nvme\d+n\d+|mmcblk\d+)p\d+$", best_dev):
        return re.sub(r"p\d+$", "", best_dev)
    return re.sub(r"\d+$", "", best_dev)


def collect_disk_usage(paths: List[str]) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    seen_targets = set()

    for raw_path in paths:
        cleaned = str(raw_path or "").strip()
        if not cleaned:
            continue
        p = Path(cleaned)
        if not p.exists():
            results.append({"path": cleaned, "exists": False})
            continue
        try:
            st = os.statvfs(str(p))
            total = int(st.f_blocks * st.f_frsize)
            avail = int(st.f_bavail * st.f_frsize)
            used = max(0, total - avail)
            used_pct = (used / total * 100.0) if total > 0 else 0.0
            key = (total, avail)
            if key in seen_targets:
                continue
            seen_targets.add(key)
            results.append(
                {
                    "path": cleaned,
                    "exists": True,
                    "total": total,
                    "used": used,
                    "free": avail,
                    "used_pct": used_pct,
                }
            )
        except (OSError, AttributeError):
            results.append({"path": cleaned, "exists": False})

    return results


def _run_smartctl_json(smartctl_bin: str, device: str, sat_mode: bool = False) -> Dict[str, Any]:
    cmd = [smartctl_bin]
    if sat_mode:
        cmd.extend(["-d", "sat"])
    cmd.extend(["-i", "-H", "-A", "-j", device])
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=8,
        check=False,
    )
    return json.loads(proc.stdout) if proc.stdout else {}


def _extract_temperature(data: Dict[str, Any]) -> Optional[int]:
    temp_obj = data.get("temperature") or {}
    if isinstance(temp_obj, dict) and isinstance(temp_obj.get("current"), int):
        return temp_obj["current"]

    attrs = (data.get("ata_smart_attributes") or {}).get("table") or []
    for attr in attrs:
        if attr.get("id") in (194, 190):
            raw = attr.get("raw") or {}
            raw_str = str(raw.get("string") or "").strip()
            if raw_str:
                first = raw_str.split()[0]
                if first.lstrip("-").isdigit():
                    return int(first)
            raw_val = raw.get("value")
            if isinstance(raw_val, int):
                return raw_val & 0xFF
    return None


def collect_smart_info(devices: List[str]) -> List[Dict[str, Any]]:
    """Query S.M.A.R.T. health & temperature via smartctl (with USB -d sat fallback)."""
    smartctl_bin = shutil.which("smartctl") or next(
        (
            p
            for p in ("/opt/sbin/smartctl", "/usr/sbin/smartctl", "/sbin/smartctl")
            if Path(p).exists()
        ),
        None,
    )
    if not smartctl_bin:
        return []

    output: List[Dict[str, Any]] = []
    seen = set()

    for dev in devices:
        cleaned = str(dev or "").strip()
        if not cleaned or cleaned in seen or not Path(cleaned).exists():
            continue
        seen.add(cleaned)
        try:
            data = _run_smartctl_json(smartctl_bin, cleaned, sat_mode=False)
            has_status = (
                isinstance(data.get("smart_status"), dict)
                and "passed" in data["smart_status"]
            )
            temp_c = _extract_temperature(data)

            if not has_status and temp_c is None:
                data = _run_smartctl_json(smartctl_bin, cleaned, sat_mode=True)
                has_status = (
                    isinstance(data.get("smart_status"), dict)
                    and "passed" in data["smart_status"]
                )
                temp_c = _extract_temperature(data)

            passed: Optional[bool] = (
                bool(data["smart_status"]["passed"]) if has_status else None
            )
            model = str(data.get("model_name") or "").strip()
            output.append(
                {
                    "device": cleaned,
                    "model": model,
                    "passed": passed,
                    "temp_c": temp_c,
                }
            )
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, ValueError):
            output.append(
                {
                    "device": cleaned,
                    "model": "",
                    "passed": None,
                    "temp_c": None,
                }
            )

    return output


def collect_system_snapshot(
    disk_paths: Optional[List[str]] = None,
    smart_devices: Optional[List[str]] = None,
) -> Dict[str, Any]:
    mem = _read_meminfo()
    ul = _read_uptime_and_load()

    mem_total = mem.get("MemTotal", 0)
    mem_avail = mem.get("MemAvailable", 0)
    if mem_total > 0 and mem_avail <= 0:
        mem_avail = mem.get("MemFree", 0) + mem.get("Buffers", 0) + mem.get("Cached", 0)
    mem_used = max(0, mem_total - mem_avail) if mem_total > 0 else 0

    swap_total = mem.get("SwapTotal", 0)
    swap_free = mem.get("SwapFree", 0)
    swap_used = max(0, swap_total - swap_free) if swap_total > 0 else 0

    try:
        bot_rss = int(os.sysconf("SC_PAGE_SIZE")) * int(
            Path("/proc/self/statm").read_text(encoding="utf-8").split()[1]
        )
    except (OSError, ValueError, IndexError, AttributeError):
        bot_rss = 0

    target_disks = list(disk_paths or [])
    if not target_disks:
        target_disks = ["/opt", "/tmp"] if Path("/opt").exists() else [str(Path.cwd())]

    resolved_smart: List[str] = []
    for item in list(smart_devices or []):
        if str(item).strip().lower() == "auto":
            for p in target_disks:
                dev = _resolve_block_device(p)
                if dev and dev not in resolved_smart:
                    resolved_smart.append(dev)
        else:
            cleaned = str(item).strip()
            if cleaned and cleaned not in resolved_smart:
                resolved_smart.append(cleaned)

    return {
        "os_name": f"{os.name} ({os.uname().sysname} {os.uname().machine})"
        if hasattr(os, "uname")
        else os.name,
        "uptime_sec": ul["uptime_sec"],
        "load": ul["load"],
        "mem_total": mem_total,
        "mem_used": mem_used,
        "mem_avail": mem_avail,
        "swap_total": swap_total,
        "swap_used": swap_used,
        "bot_rss": bot_rss,
        "disks": collect_disk_usage(target_disks),
        "smart": collect_smart_info(resolved_smart),
    }