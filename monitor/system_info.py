from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

_REAL_FS_TYPES = {
    "ext2",
    "ext3",
    "ext4",
    "xfs",
    "btrfs",
    "f2fs",
    "vfat",
    "msdos",
    "exfat",
    "ntfs",
    "ntfs3",
    "fuseblk",
    "ubifs",
    "jffs2",
    "yaffs2",
    "overlay",
    "zfs",
    "hfsplus",
    "apfs",
}


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _read_meminfo() -> Dict[str, int]:
    """Read /proc/meminfo values in bytes on Linux/Entware/OpenWrt/RPi."""
    info: Dict[str, int] = {}
    path = Path("/proc/meminfo")
    if not path.exists():
        return info
    for line in _read_text(path).splitlines():
        parts = line.split(":")
        if len(parts) != 2:
            continue
        key = parts[0].strip()
        tokens = parts[1].strip().split()
        if tokens and tokens[0].isdigit():
            info[key] = int(tokens[0]) * 1024
    return info


def _read_swaps() -> List[Dict[str, Any]]:
    """Read active swap partitions and zRAM devices from /proc/swaps."""
    swaps: List[Dict[str, Any]] = []
    path = Path("/proc/swaps")
    if not path.exists():
        return swaps
    lines = _read_text(path).splitlines()
    for line in lines[1:]:
        parts = line.split()
        if len(parts) < 5:
            continue
        try:
            total = int(parts[2]) * 1024
            used = int(parts[3]) * 1024
            pct = (used / total * 100.0) if total > 0 else 0.0
            swaps.append(
                {
                    "name": parts[0],
                    "type": parts[1],
                    "total": total,
                    "used": used,
                    "used_pct": pct,
                }
            )
        except ValueError:
            continue
    return swaps


def _read_thermal_zones() -> List[Dict[str, Any]]:
    """Read SoC/board thermal sensors from /sys/class/thermal if available."""
    zones: List[Dict[str, Any]] = []
    base = Path("/sys/class/thermal")
    if not base.exists():
        return zones
    try:
        for entry in sorted(base.iterdir()):
            if not entry.name.startswith("thermal_zone"):
                continue
            raw_temp = _read_text(entry / "temp")
            if not raw_temp or not raw_temp.lstrip("-").isdigit():
                continue
            val = int(raw_temp)
            temp_c = val / 1000.0 if abs(val) >= 1000 else float(val)
            if 0.0 < temp_c < 150.0:
                ztype = _read_text(entry / "type") or entry.name
                zones.append({"name": ztype, "temp_c": round(temp_c, 1)})
    except OSError:
        pass
    return zones


def _read_uptime_and_load() -> Dict[str, Any]:
    uptime_sec = 0.0
    load_str = "—"
    uptime_text = _read_text(Path("/proc/uptime"))
    if uptime_text:
        try:
            uptime_sec = float(uptime_text.split()[0])
        except (ValueError, IndexError):
            pass

    load_text = _read_text(Path("/proc/loadavg"))
    if load_text:
        tokens = load_text.split()[:3]
        if tokens:
            load_str = " / ".join(tokens)

    if uptime_sec <= 0.0 and hasattr(os, "getloadavg"):
        try:
            l1, l5, l15 = os.getloadavg()
            load_str = f"{l1:.2f} / {l5:.2f} / {l15:.2f}"
        except OSError:
            pass

    return {"uptime_sec": int(uptime_sec), "load": load_str}


def _parent_block_device(dev_path: str) -> Optional[str]:
    if not dev_path or not dev_path.startswith("/dev/"):
        return None
    if re.search(r"(nvme\d+n\d+|mmcblk\d+)p\d+$", dev_path):
        return re.sub(r"p\d+$", "", dev_path)
    if re.match(r"^/dev/(sd[a-z]+|hd[a-z]+|vd[a-z]+)\d+$", dev_path):
        return re.sub(r"\d+$", "", dev_path)
    if re.match(r"^/dev/(sd[a-z]+|hd[a-z]+|vd[a-z]+|nvme\d+n\d+|mmcblk\d+)$", dev_path):
        return dev_path
    return None


def _mount_priority(mount_path: str, preferred_path: str = "") -> int:
    if preferred_path:
        try:
            pref_real = os.path.realpath(preferred_path)
            if pref_real == mount_path or pref_real.startswith(mount_path.rstrip("/") + "/"):
                return 0
        except OSError:
            pass
    if mount_path == "/opt":
        return 1
    if mount_path == "/storage":
        return 2
    if mount_path.startswith("/tmp/mnt/"):
        return 3
    if mount_path == "/":
        return 4
    return 5


def _friendly_mount_label(mount_path: str, fstype: str, device: str, preferred_path: str = "") -> str:
    if mount_path == "/opt":
        return "Entware"
    if mount_path == "/storage" or fstype in ("ubifs", "jffs2", "yaffs2") or "ubi" in device or "mtd" in device:
        return "Dahili Depolama"
    if preferred_path:
        try:
            pref_real = os.path.realpath(preferred_path)
            if pref_real == mount_path or pref_real.startswith(mount_path.rstrip("/") + "/"):
                leaf = Path(pref_real).name
                return f"Depolama ({leaf})" if leaf else "Torrent Diski"
        except OSError:
            pass
    if mount_path == "/":
        return "Sistem Kökü"
    if mount_path.startswith("/tmp/mnt/"):
        return "Harici Depolama"
    return Path(mount_path).name or mount_path


def _stat_path_usage(path_str: str) -> Optional[Dict[str, Any]]:
    p = Path(path_str)
    if not p.exists():
        return None
    try:
        if hasattr(os, "statvfs"):
            st = os.statvfs(str(p))
            total = int(st.f_blocks * st.f_frsize)
            free = int(st.f_bavail * st.f_frsize)
        else:
            du = shutil.disk_usage(str(p))
            total = int(du.total)
            free = int(du.free)
        if total <= 0:
            return None
        used = max(0, total - free)
        used_pct = used / total * 100.0
        free_pct = max(0.0, 100.0 - used_pct)
        return {
            "total": total,
            "used": used,
            "free": free,
            "used_pct": used_pct,
            "free_pct": free_pct,
        }
    except OSError:
        return None


def discover_storage_mounts(
    explicit_paths: Optional[List[str]] = None,
    preferred_path: str = "",
) -> List[Dict[str, Any]]:
    """Discover all real mounted storage filesystems (internal flash, Entware, external HDD/USB)."""
    mounts_file = Path("/proc/mounts")
    if not mounts_file.exists():
        # Windows / non-Linux fallback
        fallback_paths = list(explicit_paths or [])
        if preferred_path and preferred_path not in fallback_paths:
            fallback_paths.append(preferred_path)
        if not fallback_paths:
            fallback_paths = [str(Path.cwd())]
        results: List[Dict[str, Any]] = []
        for raw in fallback_paths:
            usage = _stat_path_usage(raw)
            if usage:
                results.append(
                    {
                        "label": Path(raw).name or raw,
                        "path": raw,
                        "device": "",
                        "parent_device": None,
                        "fstype": "",
                        "exists": True,
                        **usage,
                    }
                )
            else:
                results.append({"label": raw, "path": raw, "exists": False})
        return results

    raw_mounts: List[Dict[str, str]] = []
    for line in _read_text(mounts_file).splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        dev = parts[0]
        mnt = parts[1].replace("\\040", " ")
        fstype = parts[2].lower()
        raw_mounts.append({"device": dev, "mount": mnt, "fstype": fstype})

    by_st_dev: Dict[int, Dict[str, Any]] = {}

    def _consider_mount(dev: str, mnt: str, fstype: str) -> None:
        usage = _stat_path_usage(mnt)
        if not usage:
            return
        try:
            st_dev = os.stat(mnt).st_dev
        except OSError:
            return

        prio = _mount_priority(mnt, preferred_path)
        candidate = {
            "label": _friendly_mount_label(mnt, fstype, dev, preferred_path),
            "path": mnt,
            "device": dev,
            "parent_device": _parent_block_device(dev),
            "fstype": fstype,
            "exists": True,
            "_prio": prio,
            **usage,
        }
        existing = by_st_dev.get(st_dev)
        if existing is None or (prio, len(mnt)) < (existing["_prio"], len(existing["path"])):
            by_st_dev[st_dev] = candidate

    # 1. Otomatik gercek depolama baglamalarini tara
    for entry in raw_mounts:
        fstype = entry["fstype"]
        mnt = entry["mount"]
        dev = entry["device"]
        is_special = mnt in ("/opt", "/storage")
        if fstype in _REAL_FS_TYPES or is_special:
            # Sadece salt-okunur sistem ROM (squashfs) baglamalarini atla
            if fstype == "squashfs":
                continue
            _consider_mount(dev, mnt, fstype)

    # 2. Kullanici tarafindan ozel olarak belirtilen yollar varsa ekle
    for raw in list(explicit_paths or []):
        cleaned = str(raw or "").strip()
        if not cleaned or not Path(cleaned).exists():
            continue
        target = os.path.realpath(cleaned)
        best = None
        best_len = -1
        for entry in raw_mounts:
            m = entry["mount"].rstrip("/") or "/"
            if target == m or m == "/" or target.startswith(m + "/"):
                if len(m) > best_len:
                    best_len = len(m)
                    best = entry
        if best:
            _consider_mount(best["device"], best["mount"], best["fstype"])

    items = sorted(by_st_dev.values(), key=lambda x: (x["_prio"], x["path"]))
    for item in items:
        item.pop("_prio", None)
    return items


def _format_usb_speed(raw_speed: str) -> str:
    cleaned = raw_speed.strip()
    if not cleaned:
        return ""
    mapping = {
        "1.5": "USB 1.0 (1.5 Mbps)",
        "12": "USB 1.1 (12 Mbps)",
        "480": "USB 2.0 (480 Mbps)",
        "5000": "USB 3.0 (5 Gbps)",
        "10000": "USB 3.1 (10 Gbps)",
        "20000": "USB 3.2 (20 Gbps)",
    }
    return mapping.get(cleaned, f"USB ({cleaned} Mbps)")


def _read_sysfs_block_info(device_path: str) -> Dict[str, Any]:
    """Read hardware vendor, model, USB serial, link speed, and physical size from /sys/block."""
    dev_name = Path(device_path).name
    block_dir = Path("/sys/block") / dev_name
    info: Dict[str, Any] = {
        "device": device_path,
        "vendor": "",
        "model": "",
        "serial": "",
        "firmware": "",
        "usb_speed": "",
        "media_type": "",
        "physical_bytes": 0,
    }
    if not block_dir.exists():
        return info

    size_sectors = _read_text(block_dir / "size")
    if size_sectors.isdigit():
        info["physical_bytes"] = int(size_sectors) * 512

    rot = _read_text(block_dir / "queue" / "rotational")
    if rot == "1":
        info["media_type"] = "HDD"
    elif rot == "0":
        info["media_type"] = "Flash/SSD"

    dev_dir = block_dir / "device"
    info["vendor"] = _read_text(dev_dir / "vendor")
    info["model"] = _read_text(dev_dir / "model")
    info["firmware"] = _read_text(dev_dir / "rev")

    # USB cihaz kokune (idVendor + idProduct) kadar yukari cik
    try:
        cur = dev_dir.resolve()
        for _ in range(8):
            if (cur / "idVendor").exists() and (cur / "idProduct").exists():
                usb_mfg = _read_text(cur / "manufacturer")
                usb_prod = _read_text(cur / "product")
                usb_ser = _read_text(cur / "serial")
                usb_spd = _read_text(cur / "speed")
                if usb_ser and not info["serial"]:
                    info["serial"] = usb_ser
                if usb_spd:
                    info["usb_speed"] = _format_usb_speed(usb_spd)
                if not info["vendor"] and usb_mfg:
                    info["vendor"] = usb_mfg
                if not info["model"] and usb_prod:
                    info["model"] = usb_prod
                break
            if cur.parent == cur:
                break
            cur = cur.parent
    except OSError:
        pass

    return info


def _run_smartctl_json(smartctl_bin: str, device: str, sat_mode: bool = False) -> Dict[str, Any]:
    cmd = [smartctl_bin]
    if sat_mode:
        cmd.extend(["-d", "sat"])
    cmd.extend(["-i", "-H", "-A", "-j", device])
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=6,
        check=False,
    )
    return json.loads(proc.stdout) if proc.stdout else {}


def _extract_attr_raw(attrs_by_id: Dict[int, Dict[str, Any]], attr_id: int) -> Optional[int]:
    attr = attrs_by_id.get(attr_id)
    if not attr:
        return None
    raw = attr.get("raw") or {}
    val = raw.get("value")
    if isinstance(val, int):
        return val
    raw_str = str(raw.get("string") or "").strip()
    if raw_str:
        first = raw_str.split()[0]
        if first.lstrip("-").isdigit():
            return int(first)
    return None


def _extract_temperature(data: Dict[str, Any], attrs_by_id: Dict[int, Dict[str, Any]]) -> Optional[int]:
    temp_obj = data.get("temperature") or {}
    if isinstance(temp_obj, dict) and isinstance(temp_obj.get("current"), int):
        return temp_obj["current"]

    for attr_id in (194, 190):
        attr = attrs_by_id.get(attr_id)
        if not attr:
            continue
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


def collect_block_devices_info(devices: List[str]) -> List[Dict[str, Any]]:
    """Collect /sys hardware details + full S.M.A.R.T. metrics for all physical disks."""
    smartctl_bin = shutil.which("smartctl") or next(
        (
            p
            for p in ("/opt/sbin/smartctl", "/usr/sbin/smartctl", "/sbin/smartctl")
            if Path(p).exists()
        ),
        None,
    )

    output: List[Dict[str, Any]] = []
    seen = set()

    for dev in devices:
        cleaned = str(dev or "").strip()
        if not cleaned or cleaned in seen or not Path(cleaned).exists():
            continue
        seen.add(cleaned)

        sys_info = _read_sysfs_block_info(cleaned)
        record: Dict[str, Any] = {
            "device": cleaned,
            "vendor": sys_info["vendor"],
            "model": sys_info["model"],
            "serial": sys_info["serial"],
            "firmware": sys_info["firmware"],
            "usb_speed": sys_info["usb_speed"],
            "media_type": sys_info["media_type"],
            "physical_bytes": sys_info["physical_bytes"],
            "rpm": None,
            "smart_supported": False,
            "passed": None,
            "temp_c": None,
            "power_on_hours": None,
            "power_cycles": None,
            "reallocated": None,
            "pending": None,
            "uncorrectable": None,
            "crc_errors": None,
        }

        if smartctl_bin:
            try:
                data = _run_smartctl_json(smartctl_bin, cleaned, sat_mode=False)
                attrs_list = (data.get("ata_smart_attributes") or {}).get("table") or []
                attrs_by_id = {int(a["id"]): a for a in attrs_list if "id" in a}
                has_status = (
                    isinstance(data.get("smart_status"), dict)
                    and "passed" in data["smart_status"]
                )
                temp_c = _extract_temperature(data, attrs_by_id)

                if not has_status and temp_c is None:
                    data = _run_smartctl_json(smartctl_bin, cleaned, sat_mode=True)
                    attrs_list = (data.get("ata_smart_attributes") or {}).get("table") or []
                    attrs_by_id = {int(a["id"]): a for a in attrs_list if "id" in a}
                    has_status = (
                        isinstance(data.get("smart_status"), dict)
                        and "passed" in data["smart_status"]
                    )
                    temp_c = _extract_temperature(data, attrs_by_id)

                if has_status or temp_c is not None or data.get("serial_number"):
                    record["smart_supported"] = True
                    if data.get("model_name"):
                        record["model"] = str(data["model_name"]).strip()
                    if data.get("serial_number"):
                        record["serial"] = str(data["serial_number"]).strip()
                    if data.get("firmware_version"):
                        record["firmware"] = str(data["firmware_version"]).strip()

                    cap = (data.get("user_capacity") or {}).get("bytes")
                    if isinstance(cap, int) and cap > 0:
                        record["physical_bytes"] = cap

                    rpm = data.get("rotation_rate")
                    if isinstance(rpm, int) and rpm > 0:
                        record["rpm"] = rpm
                        record["media_type"] = "HDD"
                    elif rpm == 0:
                        record["media_type"] = "SSD"

                    if has_status:
                        record["passed"] = bool(data["smart_status"]["passed"])
                    record["temp_c"] = temp_c

                    poh = (data.get("power_on_time") or {}).get("hours")
                    record["power_on_hours"] = (
                        int(poh)
                        if isinstance(poh, int)
                        else _extract_attr_raw(attrs_by_id, 9)
                    )
                    pcc = data.get("power_cycle_count")
                    record["power_cycles"] = (
                        int(pcc)
                        if isinstance(pcc, int)
                        else _extract_attr_raw(attrs_by_id, 12)
                    )
                    record["reallocated"] = _extract_attr_raw(attrs_by_id, 5)
                    record["pending"] = _extract_attr_raw(attrs_by_id, 197)
                    record["uncorrectable"] = _extract_attr_raw(attrs_by_id, 198)
                    record["crc_errors"] = _extract_attr_raw(attrs_by_id, 199)
            except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, ValueError):
                pass

        output.append(record)

    return output


def _discover_physical_block_devices(mounts: List[Dict[str, Any]]) -> List[str]:
    found: List[str] = []

    # 1. /sys/block uzerindeki tum fiziksel diskler (sd*, nvme*, mmcblk*)
    sys_block = Path("/sys/block")
    if sys_block.exists():
        try:
            for entry in sorted(sys_block.iterdir()):
                name = entry.name
                if re.match(r"^(sd[a-z]+|hd[a-z]+|vd[a-z]+|nvme\d+n\d+|mmcblk\d+)$", name):
                    dev_path = f"/dev/{name}"
                    if dev_path not in found:
                        found.append(dev_path)
        except OSError:
            pass

    # 2. Bagli dosya sistemlerinden cozulen ust diskler
    for m in mounts:
        parent = m.get("parent_device")
        if parent and parent not in found:
            found.append(parent)

    return found


def collect_system_snapshot(
    disk_paths: Optional[List[str]] = None,
    smart_devices: Optional[List[str]] = None,
    preferred_save_path: str = "",
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

    mounts = discover_storage_mounts(
        explicit_paths=disk_paths,
        preferred_path=preferred_save_path,
    )

    resolved_devices: List[str] = []
    req_devices = list(smart_devices) if smart_devices is not None else ["auto"]
    for item in req_devices:
        if str(item).strip().lower() == "auto":
            for dev in _discover_physical_block_devices(mounts):
                if dev not in resolved_devices:
                    resolved_devices.append(dev)
        else:
            cleaned = str(item).strip()
            if cleaned and cleaned not in resolved_devices:
                resolved_devices.append(cleaned)

    return {
        "os_name": f"{os.name} ({os.uname().sysname} {os.uname().machine})"
        if hasattr(os, "uname")
        else os.name,
        "uptime_sec": ul["uptime_sec"],
        "load": ul["load"],
        "thermals": _read_thermal_zones(),
        "mem_total": mem_total,
        "mem_used": mem_used,
        "mem_avail": mem_avail,
        "swap_total": swap_total,
        "swap_used": swap_used,
        "swaps": _read_swaps(),
        "bot_rss": bot_rss,
        "disks": mounts,
        "smart": collect_block_devices_info(resolved_devices),
    }