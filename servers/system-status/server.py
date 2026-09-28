#!/usr/bin/env python3
"""
System Status MCP Server

What an agent operating this machine needs to know about it: which OS and
package manager it is on, whether the user is at the keyboard, how much battery,
memory and disk are left, whether it is online, what is eating the CPU, whether
anything is overheating, and whether any service has failed -- plus one call
that checks all of it and reports only what is wrong.

Read-only and local. Every figure comes from /proc, /sys and /etc, or from the
system's own tools (`ip`, `systemctl`, `timedatectl`, `loginctl`, `nmcli`),
which are asked what they already know. This server opens no network
connection of any kind: the primary route is found by asking the kernel's
routing table, which sends nothing.

Python standard library only. Linux-only: the data sources are Linux kernel
interfaces, and macOS or Windows would each need their own backend rather than
a port of this one.

Every section degrades rather than fails. A desktop has no battery, a VM has no
thermal sensors, a minimal install may lack NetworkManager -- each of those
reports "not available" and the rest of the answer still arrives.
"""

import datetime
import json
import os
import pathlib
import pwd
import re
import shutil
import subprocess
import sys
import time
from typing import Any

# Kernel and config roots. Read at call time rather than captured at import, so
# the self-test can point them at a synthetic tree -- which is also the only
# way to exercise the battery code on a machine that has no battery.
PROC = pathlib.Path("/proc")
SYS = pathlib.Path("/sys")
ETC = pathlib.Path("/etc")
RUN = pathlib.Path("/run")
MODULE_DIRS = (pathlib.Path("/usr/lib/modules"), pathlib.Path("/lib/modules"))
PCI_IDS = (
    pathlib.Path("/usr/share/hwdata/pci.ids"),
    pathlib.Path("/usr/share/misc/pci.ids"),
    pathlib.Path("/usr/share/pci.ids"),
)

COMMAND_TIMEOUT_SECONDS = 5

# Filesystems that hold user data. Everything else in /proc/mounts -- proc,
# sysfs, cgroups, tmpfs, overlay, and squashfs (every snap and AppImage mounts
# one) -- is either not storage or not the user's, and listing it buries the
# drive they are asking about under thirty entries.
REAL_FILESYSTEMS = {
    "btrfs", "ext2", "ext3", "ext4", "xfs", "f2fs", "zfs", "bcachefs", "jfs",
    "reiserfs", "vfat", "exfat", "ntfs", "ntfs3", "fuseblk", "hfsplus", "apfs",
}

# Health thresholds. Deliberately conservative: a finding the user learns to
# ignore is worse than none, so these fire on conditions that warrant action.
DISK_WARN_PERCENT = 90.0
DISK_CRIT_PERCENT = 97.0
DISK_ABSOLUTE_FLOOR_BYTES = 20 * 1024**3       # absolute-free rules only apply above this size
DISK_WARN_FREE_BYTES = 5 * 1024**3
DISK_CRIT_FREE_BYTES = 1 * 1024**3
BATTERY_WARN_PERCENT = 20
BATTERY_CRIT_PERCENT = 10
BATTERY_WORN_HEALTH_PERCENT = 60
MEMORY_WARN_AVAILABLE_PERCENT = 10.0
MEMORY_CRIT_AVAILABLE_PERCENT = 5.0
PSI_FULL_WARN_AVG60 = 5.0      # % of time every task was stalled on memory
LOAD_WARN_PER_CORE = 1.5        # 5-minute load, per logical core
LOAD_CRIT_PER_CORE = 3.0
TEMP_MARGIN_TO_CRIT_C = 5.0
TEMP_WARN_NO_CRIT_C = 95.0      # for sensors that publish no critical point
TEMP_PLAUSIBLE_RANGE_C = (-40.0, 150.0)  # outside this a driver is reporting a sentinel, not a reading

# Package managers by the os-release ID that owns them, checked in order of
# ID then ID_LIKE. Picking by distro rather than by "first binary on PATH"
# matters: a Debian box can have `rpm` installed and an Arch box can have
# `apt` from the AUR, and suggesting the wrong one is the classic agent mistake.
DISTRO_PACKAGE_MANAGERS = {
    "arch": "pacman", "cachyos": "pacman", "manjaro": "pacman", "endeavouros": "pacman",
    "debian": "apt", "ubuntu": "apt", "linuxmint": "apt", "pop": "apt",
    "fedora": "dnf", "rhel": "dnf", "centos": "dnf", "rocky": "dnf", "almalinux": "dnf",
    "opensuse": "zypper", "opensuse-tumbleweed": "zypper", "opensuse-leap": "zypper", "suse": "zypper",
    "alpine": "apk", "void": "xbps-install", "gentoo": "emerge", "nixos": "nix",
}
EXTRA_PACKAGE_TOOLS = ("paru", "yay", "flatpak", "snap", "nix", "brew")

PCI_VENDORS = {"0x8086": "Intel", "0x10de": "NVIDIA", "0x1002": "AMD", "0x1af4": "Red Hat (virtio)", "0x15ad": "VMware"}


class StatusError(Exception):
    """Anything the caller could plausibly fix, or needs told plainly."""


# --- low-level readers ----------------------------------------------------


def _read(path: pathlib.Path) -> str | None:
    try:
        return path.read_text(errors="replace").strip()
    except (OSError, ValueError):
        return None


def _read_int(path: pathlib.Path) -> int | None:
    text = _read(path)
    if text is None:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _which(name: str) -> str | None:
    return shutil.which(name)


def _run(args: list[str], *, accept: tuple[int, ...] = (0,)) -> str | None:
    """Run a system tool and return stdout, or None if it is absent or fails.

    `accept` exists for tools that report through the exit code:
    systemd-detect-virt exits 1 to say "none", which is an answer, not a failure.
    """
    if _which(args[0]) is None:
        return None
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, timeout=COMMAND_TIMEOUT_SECONDS
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode not in accept:
        return None
    return proc.stdout


def _statvfs(path: str) -> os.statvfs_result:
    return os.statvfs(path)


def _kernel_release() -> str:
    return os.uname().release


def _human_bytes(value: float | None) -> str | None:
    if value is None:
        return None
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
    size = float(value)
    for unit in units:
        if abs(size) < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return None


def _human_duration(seconds: float | None) -> str | None:
    if seconds is None or seconds < 0:
        return None
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


def _parse_key_values(text: str, sep: str = "=") -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        if sep not in line:
            continue
        key, _, value = line.partition(sep)
        out[key.strip()] = value.strip().strip('"')
    return out


def _nmcli_fields(line: str) -> list[str]:
    """Split one `nmcli -t` line on unescaped colons.

    Terse mode escapes a literal ':' as '\\:', and SSIDs contain colons often
    enough (MAC-derived default names) that a plain split puts the signal
    strength into the security field.
    """
    fields, current, escaped = [], [], False
    for char in line:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ":":
            fields.append("".join(current))
            current = []
        else:
            current.append(char)
    fields.append("".join(current))
    return fields


# --- sections -------------------------------------------------------------


def _os_info() -> dict:
    release = _parse_key_values(_read(ETC / "os-release") or "")
    uname = os.uname()
    return {
        "name": release.get("PRETTY_NAME") or release.get("NAME"),
        "id": release.get("ID"),
        "id_like": release.get("ID_LIKE", "").split() or None,
        "version": release.get("VERSION_ID") or release.get("BUILD_ID"),
        "kernel": _kernel_release(),
        "architecture": uname.machine,
        "hostname": uname.nodename,
    }


def _package_managers(os_info: dict) -> dict:
    candidates = [os_info.get("id")] + (os_info.get("id_like") or [])
    primary = None
    for distro in candidates:
        manager = DISTRO_PACKAGE_MANAGERS.get(distro or "")
        if manager and _which(manager.split()[0]):
            primary = manager
            break
    if primary is None:
        # An unknown distro: fall back to whatever is actually installed.
        for manager in dict.fromkeys(DISTRO_PACKAGE_MANAGERS.values()):
            if _which(manager):
                primary = manager
                break
    extras = [tool for tool in EXTRA_PACKAGE_TOOLS if _which(tool)]
    return {"primary": primary, "also_available": extras}


def _init_system() -> str | None:
    if (RUN / "systemd" / "system").is_dir():
        return "systemd"
    return _read(PROC / "1" / "comm")


def _virtualization() -> dict:
    # systemd-detect-virt exits 1 when it detects nothing, and prints "none".
    detected = _run(["systemd-detect-virt"], accept=(0, 1))
    kind = detected.strip() if detected else None
    container = None
    if pathlib.Path("/.dockerenv").exists():
        container = "docker"
    elif (RUN / ".containerenv").exists():
        container = "podman"
    return {
        "virtualization": kind,
        "bare_metal": kind == "none" if kind else None,
        "container": container,
    }


def _session() -> dict:
    user = None
    try:
        user = pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        user = os.environ.get("USER")

    info = {
        "user": user,
        "shell": os.environ.get("SHELL"),
        "type": os.environ.get("XDG_SESSION_TYPE"),
        "desktop": os.environ.get("XDG_CURRENT_DESKTOP"),
        "locked": None,
        "idle": None,
    }

    # The graphical session, not whichever session spawned this process: a
    # server started by a daemon may not carry the desktop's environment at all.
    session_id = None
    if user:
        raw = _run(["loginctl", "show-user", user, "-p", "Display", "--value"])
        session_id = raw.strip() if raw and raw.strip() else None
    if session_id:
        raw = _run(["loginctl", "show-session", session_id,
                    "-p", "LockedHint", "-p", "IdleHint", "-p", "Type", "-p", "Desktop"])
        if raw:
            props = _parse_key_values(raw)
            info["locked"] = props.get("LockedHint") == "yes" if "LockedHint" in props else None
            info["idle"] = props.get("IdleHint") == "yes" if "IdleHint" in props else None
            info["type"] = info["type"] or props.get("Type") or None
            info["desktop"] = info["desktop"] or props.get("Desktop") or None
    return info


def _uptime() -> dict:
    raw = _read(PROC / "uptime")
    if not raw:
        return {"seconds": None, "human": None, "booted_at": None}
    seconds = float(raw.split()[0])
    booted = datetime.datetime.now().astimezone() - datetime.timedelta(seconds=seconds)
    return {
        "seconds": int(seconds),
        "human": _human_duration(seconds),
        "booted_at": booted.isoformat(timespec="seconds"),
    }


def _time_info() -> dict:
    now = datetime.datetime.now().astimezone()
    zone = None
    synced = None
    raw = _run(["timedatectl", "show", "-p", "Timezone", "-p", "NTPSynchronized"])
    if raw:
        props = _parse_key_values(raw)
        zone = props.get("Timezone") or None
        if "NTPSynchronized" in props:
            synced = props["NTPSynchronized"] == "yes"
    if zone is None:
        try:
            target = os.readlink(ETC / "localtime")
            zone = target.split("zoneinfo/", 1)[1] if "zoneinfo/" in target else None
        except OSError:
            zone = None
    return {
        "local": now.isoformat(timespec="seconds"),
        "utc": now.astimezone(datetime.timezone.utc).isoformat(timespec="seconds"),
        "timezone": zone,
        "utc_offset": now.strftime("%z"),
        "weekday": now.strftime("%A"),
        "clock_synchronized": synced,
    }


def _cpu_static() -> dict:
    text = _read(PROC / "cpuinfo") or ""
    model = None
    cores = set()
    physical = None
    core_id = None
    for line in text.splitlines():
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if key in ("model name", "Model", "Hardware") and model is None and value:
            model = value
        elif key == "physical id":
            physical = value
        elif key == "core id":
            core_id = value
        elif not line.strip():
            if physical is not None and core_id is not None:
                cores.add((physical, core_id))
            physical = core_id = None
    if physical is not None and core_id is not None:
        cores.add((physical, core_id))

    cpufreq = SYS / "devices" / "system" / "cpu" / "cpu0" / "cpufreq"
    max_khz = _read_int(cpufreq / "cpuinfo_max_freq")
    min_khz = _read_int(cpufreq / "cpuinfo_min_freq")
    base_khz = _read_int(cpufreq / "base_frequency")

    current = []
    cpu_root = SYS / "devices" / "system" / "cpu"
    if cpu_root.is_dir():
        for entry in cpu_root.glob("cpu[0-9]*"):
            value = _read_int(entry / "cpufreq" / "scaling_cur_freq")
            if value:
                current.append(value)

    # Boost is reported differently per driver: intel_pstate exposes the
    # inverse (no_turbo), acpi-cpufreq and amd-pstate a positive `boost`.
    # With boost off, cpuinfo_max_freq reads as the base clock, so the "max"
    # below silently understates what the chip can do unless this is said.
    boost = None
    no_turbo = _read_int(SYS / "devices" / "system" / "cpu" / "intel_pstate" / "no_turbo")
    if no_turbo is not None:
        boost = no_turbo == 0
    else:
        generic = _read_int(SYS / "devices" / "system" / "cpu" / "cpufreq" / "boost")
        if generic is not None:
            boost = generic == 1

    logical = os.cpu_count()
    return {
        "model": model,
        "boost_enabled": boost,
        "logical_cores": logical,
        "physical_cores": len(cores) or None,
        "frequency_mhz": {
            "current_average": round(sum(current) / len(current) / 1000) if current else None,
            "min": round(min_khz / 1000) if min_khz else None,
            "max": round(max_khz / 1000) if max_khz else None,
            "base": round(base_khz / 1000) if base_khz else None,
        },
        "governor": _read(cpufreq / "scaling_governor"),
        "energy_preference": _read(cpufreq / "energy_performance_preference"),
    }


def _cpu_times() -> dict[str, list[int]]:
    out = {}
    for line in (_read(PROC / "stat") or "").splitlines():
        if line.startswith("cpu"):
            parts = line.split()
            out[parts[0]] = [int(x) for x in parts[1:9]]
    return out


def _cpu_usage(interval: float) -> dict:
    """Sample /proc/stat twice. A single read only gives totals since boot."""
    first = _cpu_times()
    time.sleep(interval)
    second = _cpu_times()

    def percent(name: str) -> tuple[float | None, float | None]:
        if name not in first or name not in second:
            return None, None
        a, b = first[name], second[name]
        # user nice system idle iowait irq softirq steal
        delta = [y - x for x, y in zip(a, b)]
        total = sum(delta)
        if total <= 0:
            return None, None
        idle = delta[3] + delta[4]
        return round(100.0 * (total - idle) / total, 1), round(100.0 * delta[4] / total, 1)

    overall, iowait = percent("cpu")
    per_core = []
    for name in sorted((n for n in second if n != "cpu"), key=lambda n: int(n[3:])):
        usage, _ = percent(name)
        per_core.append(usage)
    return {"percent": overall, "iowait_percent": iowait, "per_core_percent": per_core}


def _load() -> dict:
    try:
        one, five, fifteen = os.getloadavg()
    except OSError:
        return {"1m": None, "5m": None, "15m": None, "per_core_5m": None}
    cores = os.cpu_count() or 1
    return {
        "1m": round(one, 2),
        "5m": round(five, 2),
        "15m": round(fifteen, 2),
        # Load is only meaningful relative to cores: 6.0 is saturation on six
        # cores and a quiet afternoon on sixty-four.
        "per_core_5m": round(five / cores, 2),
    }


def _meminfo() -> dict[str, int]:
    out = {}
    for line in (_read(PROC / "meminfo") or "").splitlines():
        key, _, value = line.partition(":")
        parts = value.split()
        if parts:
            try:
                out[key.strip()] = int(parts[0]) * (1024 if len(parts) > 1 and parts[1] == "kB" else 1)
            except ValueError:
                continue
    return out


def _pressure(resource: str) -> dict | None:
    """Pressure stall information: the share of time tasks waited on a resource.

    Better than "percent used" for memory, because Linux fills free RAM with
    cache on purpose. `full` is the share of time *every* runnable task was
    stalled at once -- the number that corresponds to a machine feeling frozen.
    """
    text = _read(PROC / "pressure" / resource)
    if not text:
        return None
    out = {}
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        kind = parts[0]
        fields = dict(p.split("=", 1) for p in parts[1:] if "=" in p)
        try:
            out[kind] = {k: float(fields[k]) for k in ("avg10", "avg60", "avg300") if k in fields}
        except ValueError:
            continue
    return out or None


def _memory() -> dict:
    info = _meminfo()
    total = info.get("MemTotal")
    available = info.get("MemAvailable")
    used = total - available if total is not None and available is not None else None

    swaps = []
    for line in (_read(PROC / "swaps") or "").splitlines()[1:]:
        parts = line.split()
        if len(parts) < 4:
            continue
        name, kind, size_kb, used_kb = parts[0], parts[1], parts[2], parts[3]
        try:
            size, used_swap = int(size_kb) * 1024, int(used_kb) * 1024
        except ValueError:
            continue
        swaps.append({
            "device": name,
            # zram is compressed RAM, not disk: heavy use of it is cheap and
            # expected, heavy use of disk swap is what makes a machine crawl.
            "kind": "zram" if "/zram" in name else kind,
            "size": _human_bytes(size),
            "used": _human_bytes(used_swap),
            "percent_used": round(100.0 * used_swap / size, 1) if size else None,
            "size_bytes": size,
            "used_bytes": used_swap,
        })

    swap_total = info.get("SwapTotal")
    swap_free = info.get("SwapFree")
    return {
        "total": _human_bytes(total),
        "available": _human_bytes(available),
        "used": _human_bytes(used),
        "percent_used": round(100.0 * used / total, 1) if used is not None and total else None,
        "cached": _human_bytes(info.get("Cached")),
        "total_bytes": total,
        "available_bytes": available,
        "swap": {
            "total": _human_bytes(swap_total),
            "used": _human_bytes(swap_total - swap_free) if swap_total is not None and swap_free is not None else None,
            "devices": swaps,
        },
        "pressure": _pressure("memory"),
        "note": (
            "`available` is what matters. Linux fills idle RAM with cache on "
            "purpose, so high `used` alone is not a problem."
        ),
    }


def _power_supplies() -> list[tuple[pathlib.Path, dict]]:
    root = SYS / "class" / "power_supply"
    if not root.is_dir():
        return []
    out = []
    for entry in sorted(root.iterdir()):
        props = {}
        for name in ("type", "status", "capacity", "scope", "online", "technology",
                     "manufacturer", "model_name", "cycle_count",
                     "energy_now", "energy_full", "energy_full_design", "power_now",
                     "charge_now", "charge_full", "charge_full_design", "current_now",
                     "voltage_now"):
            value = _read(entry / name)
            if value is not None:
                props[name] = value
        out.append((entry, props))
    return out


def _as_int(props: dict, key: str) -> int | None:
    try:
        return int(props[key])
    except (KeyError, ValueError):
        return None


def _battery_detail(entry: pathlib.Path, props: dict) -> dict:
    status = props.get("status")
    capacity = _as_int(props, "capacity")

    # Batteries report either energy (µWh with power in µW) or charge (µAh with
    # current in µA). Both ratios come out in hours, and both are needed:
    # firmware picks one and the kernel passes it through.
    now = _as_int(props, "energy_now") or _as_int(props, "charge_now")
    full = _as_int(props, "energy_full") or _as_int(props, "charge_full")
    design = _as_int(props, "energy_full_design") or _as_int(props, "charge_full_design")
    rate = _as_int(props, "power_now") or _as_int(props, "current_now")
    rate = abs(rate) if rate else None  # some firmware reports discharge as negative

    time_to_empty = time_to_full = None
    if rate and now is not None:
        if status == "Discharging":
            time_to_empty = now / rate * 3600
        elif status == "Charging" and full is not None:
            time_to_full = max(full - now, 0) / rate * 3600

    health = round(100.0 * full / design, 1) if full and design else None
    watts = None
    if _as_int(props, "power_now"):
        watts = round(abs(_as_int(props, "power_now")) / 1e6, 1)

    return {
        "name": entry.name,
        "percent": capacity,
        "status": status,
        "time_to_empty": _human_duration(time_to_empty),
        "time_to_full": _human_duration(time_to_full),
        "draw_watts": watts,
        "health_percent": health,
        "cycle_count": _as_int(props, "cycle_count"),
        "technology": props.get("technology"),
        "model": " ".join(filter(None, (props.get("manufacturer"), props.get("model_name")))) or None,
    }


def _battery() -> dict:
    batteries, peripherals, ac_online = [], [], None
    for entry, props in _power_supplies():
        kind = props.get("type")
        if kind == "Battery":
            # Wireless mice and keyboards publish batteries here too, marked
            # scope=Device. Counting them as the system battery would report
            # a laptop's charge from the mouse.
            if props.get("scope") == "Device":
                peripherals.append({
                    "name": entry.name,
                    "model": props.get("model_name"),
                    "percent": _as_int(props, "capacity"),
                    "status": props.get("status"),
                })
            else:
                batteries.append(_battery_detail(entry, props))
        elif kind in ("Mains", "USB") and "online" in props:
            online = props.get("online") == "1"
            ac_online = online if ac_online is None else (ac_online or online)

    return {
        "present": bool(batteries),
        "on_ac_power": ac_online,
        "batteries": batteries,
        "peripheral_batteries": peripherals,
    }


def _mounts() -> list[dict]:
    out = []
    for line in (_read(PROC / "self" / "mounts") or _read(PROC / "mounts") or "").splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        source, target, fstype, options = parts[0], parts[1], parts[2], parts[3]
        # /proc/mounts octal-escapes spaces and tabs in paths.
        target = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), target)
        out.append({"source": source, "mountpoint": target, "fstype": fstype,
                    "read_only": "ro" in options.split(",")})
    return out


def _disks() -> dict:
    # One entry per backing device. btrfs mounts the same partition once per
    # subvolume -- /, /home, /var/log, /var/cache... -- and statvfs reports the
    # whole filesystem at each, so listing mounts would show one drive seven
    # times, and summing them would claim seven times the real capacity.
    groups: dict[str, dict] = {}
    for mount in _mounts():
        if mount["fstype"] not in REAL_FILESYSTEMS:
            continue
        group = groups.setdefault(mount["source"], {
            "device": mount["source"],
            "fstype": mount["fstype"],
            "mountpoints": [],
            "read_only_mountpoints": [],
        })
        group["mountpoints"].append(mount["mountpoint"])
        if mount["read_only"]:
            group["read_only_mountpoints"].append(mount["mountpoint"])

    filesystems = []
    for group in groups.values():
        # Prefer "/" when present so the reading is the one users ask about.
        probe = "/" if "/" in group["mountpoints"] else group["mountpoints"][0]
        try:
            st = _statvfs(probe)
        except OSError:
            continue
        total = st.f_blocks * st.f_frsize
        free_root = st.f_bfree * st.f_frsize
        available = st.f_bavail * st.f_frsize
        used = total - free_root
        # df's definition: used against what an unprivileged user can reach,
        # so a disk that is "full" to the user reads 100% even while the
        # root-reserved blocks remain.
        denominator = used + available
        entry = {
            "device": group["device"],
            "fstype": group["fstype"],
            "mountpoints": sorted(group["mountpoints"], key=len),
            "total": _human_bytes(total),
            "used": _human_bytes(used),
            "free": _human_bytes(available),
            "percent_used": round(100.0 * used / denominator, 1) if denominator else None,
            "total_bytes": total,
            "free_bytes": available,
        }
        if group["read_only_mountpoints"]:
            entry["read_only_mountpoints"] = group["read_only_mountpoints"]
        if group["fstype"] == "btrfs":
            entry["note"] = (
                "btrfs free space is an estimate; it depends on how data and "
                "metadata are allocated. `btrfs filesystem usage /` is exact."
            )
        filesystems.append(entry)

    filesystems.sort(key=lambda f: (0 if "/" in f["mountpoints"] else 1, f["mountpoints"][0]))
    return {"filesystems": filesystems}


def _pci_names(wanted: set[tuple[str, str]]) -> dict[tuple[str, str], tuple[str | None, str | None]]:
    """Resolve (vendor, device) ids to names from pci.ids, scanning it once."""
    path = next((p for p in PCI_IDS if p.is_file()), None)
    if path is None or not wanted:
        return {}
    wanted_vendors = {v for v, _ in wanted}
    names: dict[tuple[str, str], tuple[str | None, str | None]] = {}
    vendor_id = vendor_name = None
    try:
        with path.open(errors="replace") as fh:
            for line in fh:
                if line.startswith("C "):
                    break  # device classes follow; vendors are done
                if not line.strip() or line.startswith("#"):
                    continue
                if not line.startswith("\t"):
                    vendor_id, _, vendor_name = line.strip().partition(" ")
                    vendor_name = vendor_name.strip()
                    continue
                if line.startswith("\t\t") or vendor_id not in wanted_vendors:
                    continue
                device_id, _, device_name = line.strip().partition(" ")
                if (vendor_id, device_id) in wanted:
                    names[(vendor_id, device_id)] = (vendor_name, device_name.strip())
    except OSError:
        return {}
    return names


def _gpus() -> list[dict]:
    # The PCI bus, not /sys/class/drm: a GPU with no working driver has no DRM
    # card at all, and "the NVIDIA card is present but nothing drives it" is
    # exactly the case worth being able to see.
    root = SYS / "bus" / "pci" / "devices"
    if not root.is_dir():
        return []
    found = []
    for dev in sorted(root.iterdir()):
        cls = _read(dev / "class") or ""
        if not cls.startswith("0x03"):
            continue
        vendor = (_read(dev / "vendor") or "").lower()
        device = (_read(dev / "device") or "").lower()
        driver_link = dev / "driver"
        driver = os.path.basename(os.readlink(driver_link)) if driver_link.is_symlink() else None
        found.append({
            "slot": dev.name,
            "vendor_id": vendor,
            "device_id": device,
            "driver": driver,
            "power_state": _read(dev / "power_state"),
            "primary": _read(dev / "boot_vga") == "1",
        })

    names = _pci_names({(g["vendor_id"][2:], g["device_id"][2:]) for g in found})
    for gpu in found:
        vendor_name, device_name = names.get((gpu["vendor_id"][2:], gpu["device_id"][2:]), (None, None))
        gpu["vendor"] = vendor_name or PCI_VENDORS.get(gpu["vendor_id"])
        gpu["model"] = device_name
    return found


def _network() -> dict:
    interfaces = []
    raw = _run(["ip", "-j", "addr"])
    if raw:
        try:
            for iface in json.loads(raw):
                if iface.get("link_type") == "loopback":
                    continue
                addresses = [
                    {
                        "address": a.get("local"),
                        "prefix": a.get("prefixlen"),
                        "family": "IPv4" if a.get("family") == "inet" else "IPv6",
                        "scope": a.get("scope"),
                    }
                    for a in iface.get("addr_info", [])
                    if a.get("local")
                ]
                # MAC addresses are deliberately omitted: they are a stable
                # hardware fingerprint and no question a user asks needs one.
                name = iface.get("ifname")
                interfaces.append({
                    "name": name,
                    # Physical NICs have a backing device in sysfs; docker
                    # bridges, veths, VPN tunnels and the like do not. Labelled
                    # rather than filtered: a VPN interface is sometimes exactly
                    # the one being asked about.
                    "kind": "physical" if name and (SYS / "class" / "net" / name / "device").exists() else "virtual",
                    "state": (iface.get("operstate") or "").lower() or None,
                    "addresses": addresses,
                })
        except (json.JSONDecodeError, TypeError):
            interfaces = []

    # Asking the routing table which way traffic *would* go sends no packet.
    primary = None
    raw = _run(["ip", "-j", "route", "get", "1.1.1.1"])
    if raw:
        try:
            route = (json.loads(raw) or [{}])[0]
            primary = {
                "interface": route.get("dev"),
                "local_ip": route.get("prefsrc"),
                "gateway": route.get("gateway"),
            }
        except (json.JSONDecodeError, TypeError, IndexError):
            primary = None

    resolv = _read(ETC / "resolv.conf") or ""
    nameservers = [line.split()[1] for line in resolv.splitlines()
                   if line.startswith("nameserver") and len(line.split()) > 1]
    dns = {"nameservers": nameservers}
    if nameservers == ["127.0.0.53"]:
        dns["note"] = "127.0.0.53 is systemd-resolved's local stub; `resolvectl dns` shows the real upstreams."

    wifi = None
    raw = _run(["nmcli", "-t", "-f", "ACTIVE,SSID,SIGNAL,SECURITY,CHAN", "dev", "wifi"])
    if raw:
        for line in raw.splitlines():
            fields = _nmcli_fields(line)
            if len(fields) >= 5 and fields[0] == "yes":
                wifi = {
                    "ssid": fields[1] or None,
                    "signal_percent": int(fields[2]) if fields[2].isdigit() else None,
                    "security": fields[3] or None,
                    "channel": fields[4] or None,
                }
                break

    connectivity = None
    raw = _run(["nmcli", "-t", "-f", "CONNECTIVITY", "general"])
    if raw and raw.strip():
        # NetworkManager's own periodic check; "portal" means a captive portal
        # is intercepting traffic, which is otherwise indistinguishable from
        # being online until a page fails to load.
        connectivity = raw.strip().splitlines()[0]

    interfaces.sort(key=lambda i: (i["kind"] != "physical", i["state"] != "up", i["name"] or ""))
    return {
        "online": None if connectivity is None else connectivity == "full",
        "connectivity": connectivity,
        "primary_route": primary,
        "interfaces": interfaces,
        "wifi": wifi,
        "dns": dns,
    }


def _process_sample() -> dict[int, dict]:
    out = {}
    for entry in PROC.iterdir() if PROC.is_dir() else []:
        if not entry.name.isdigit():
            continue
        stat = _read(entry / "stat")
        if not stat:
            continue
        # comm is parenthesised and may itself contain spaces and ')', so split
        # on the LAST ')' -- splitting on whitespace misreads every process
        # with a space in its name and shifts every field after it.
        open_paren, close_paren = stat.find("("), stat.rfind(")")
        if open_paren < 0 or close_paren < 0:
            continue
        name = stat[open_paren + 1:close_paren]
        fields = stat[close_paren + 2:].split()
        try:
            # Field N of stat(5) is fields[N-3] once pid and comm are removed.
            ticks = int(fields[11]) + int(fields[12])   # utime + stime
            rss_pages = int(fields[21])
        except (IndexError, ValueError):
            continue
        uid = None
        for line in (_read(entry / "status") or "").splitlines():
            if line.startswith("Uid:"):
                parts = line.split()
                uid = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
                break
        out[int(entry.name)] = {"name": name, "ticks": ticks, "rss_pages": rss_pages, "uid": uid}
    return out


def _username(uid: int | None, cache: dict) -> str | None:
    if uid is None:
        return None
    if uid not in cache:
        try:
            cache[uid] = pwd.getpwuid(uid).pw_name
        except KeyError:
            cache[uid] = str(uid)
    return cache[uid]


def _top_processes(sort_by: str, limit: int, include_command_line: bool, interval: float) -> dict:
    clock = os.sysconf("SC_CLK_TCK")
    page = os.sysconf("SC_PAGE_SIZE")
    total_memory = _meminfo().get("MemTotal")

    start = time.monotonic()
    first = _process_sample()
    time.sleep(interval)
    second = _process_sample()
    elapsed = max(time.monotonic() - start, 1e-6)

    users: dict = {}
    rows = []
    own = os.getpid()
    for pid, now in second.items():
        before = first.get(pid)
        if before is None or pid == own:
            # Started mid-sample, so its share cannot be measured fairly -- or
            # it is this server, whose CPU use during the sample is the
            # sampling itself.
            continue
        rss = now["rss_pages"] * page
        row = {
            "pid": pid,
            "name": now["name"],
            "user": _username(now["uid"], users),
            # Per core, as top reports it: a busy four-thread process reads 400%.
            "cpu_percent": round(100.0 * max(now["ticks"] - before["ticks"], 0) / (elapsed * clock), 1),
            "memory": _human_bytes(rss),
            "memory_percent": round(100.0 * rss / total_memory, 1) if total_memory else None,
            "memory_bytes": rss,
        }
        rows.append(row)

    key = "memory_bytes" if sort_by == "memory" else "cpu_percent"
    rows.sort(key=lambda r: (r[key], r["memory_bytes"]), reverse=True)
    rows = rows[:limit]

    if include_command_line:
        for row in rows:
            raw = _read(PROC / str(row["pid"]) / "cmdline")
            if raw is not None:
                command = raw.replace("\0", " ").strip()
                row["command"] = (command[:200] + "…") if len(command) > 200 else (command or None)

    return {
        "sorted_by": "memory" if key == "memory_bytes" else "cpu",
        "sample_seconds": round(elapsed, 2),
        "process_count": len(second),
        "processes": rows,
    }


def _temperatures() -> dict:
    sensors = []
    root = SYS / "class" / "hwmon"
    if root.is_dir():
        for hw in sorted(root.iterdir()):
            chip = _read(hw / "name") or hw.name
            for input_file in sorted(hw.glob("temp*_input")):
                prefix = input_file.name[: -len("_input")]
                raw = _read_int(input_file)
                if raw is None:
                    continue
                celsius = raw / 1000.0
                low, high = TEMP_PLAUSIBLE_RANGE_C
                if not low <= celsius <= high:
                    continue
                crit = _read_int(hw / f"{prefix}_crit")
                high_mark = _read_int(hw / f"{prefix}_max")
                crit_c = crit / 1000.0 if crit and low <= crit / 1000.0 <= 200 else None
                sensors.append({
                    "chip": chip,
                    "label": _read(hw / f"{prefix}_label") or prefix,
                    "celsius": round(celsius, 1),
                    "critical_celsius": round(crit_c, 1) if crit_c else None,
                    "high_celsius": round(high_mark / 1000.0, 1) if high_mark and high_mark < 200_000 else None,
                })

    zones = []
    zroot = SYS / "class" / "thermal"
    if zroot.is_dir():
        for zone in sorted(zroot.glob("thermal_zone*")):
            raw = _read_int(zone / "temp")
            if raw is None:
                continue
            celsius = raw / 1000.0
            low, high = TEMP_PLAUSIBLE_RANGE_C
            if low <= celsius <= high:
                zones.append({"zone": _read(zone / "type") or zone.name, "celsius": round(celsius, 1)})

    readings = [s["celsius"] for s in sensors] or [z["celsius"] for z in zones]
    hottest = None
    if sensors:
        top = max(sensors, key=lambda s: s["celsius"])
        hottest = {"chip": top["chip"], "label": top["label"], "celsius": top["celsius"]}
    elif zones:
        top = max(zones, key=lambda z: z["celsius"])
        hottest = {"zone": top["zone"], "celsius": top["celsius"]}

    return {
        "available": bool(readings),
        "hottest": hottest,
        "sensors": sensors,
        "thermal_zones": zones,
    }


def _failed_units() -> dict:
    result = {"available": _which("systemctl") is not None, "system": [], "user": []}
    if not result["available"]:
        return result
    for scope, args in (("system", []), ("user", ["--user"])):
        raw = _run(["systemctl", *args, "list-units", "--failed", "--output=json", "--no-pager"])
        units = []
        if raw:
            try:
                for unit in json.loads(raw):
                    name = unit.get("unit")
                    if not name:
                        continue
                    flag = "--user " if scope == "user" else ""
                    units.append({
                        "unit": name,
                        "description": unit.get("description"),
                        "state": unit.get("sub"),
                        # The next thing anyone investigating would run.
                        "investigate": f"systemctl {flag}status {name}; journalctl {flag}-u {name} -b",
                    })
            except (json.JSONDecodeError, TypeError):
                units = []
        result[scope] = units
    return result


def _reboot_required() -> dict:
    reasons = []
    flag = RUN / "reboot-required"
    if flag.exists():
        packages = _read(RUN / "reboot-required.pkgs")
        reasons.append({
            "reason": "the package manager flagged a reboot",
            "packages": packages.split() if packages else None,
        })
    release = _kernel_release()
    if not any((d / release).is_dir() for d in MODULE_DIRS):
        # The running kernel's modules were removed by an upgrade. Anything
        # that needs a new module from now on -- a USB device, a filesystem, a
        # VPN -- fails until the machine boots the kernel that is installed.
        installed = sorted(p.name for d in MODULE_DIRS if d.is_dir() for p in d.iterdir())
        reasons.append({
            "reason": "the running kernel was replaced by an upgrade; its modules are gone",
            "running_kernel": release,
            "installed_kernels": installed or None,
        })
    return {"required": bool(reasons), "reasons": reasons}


# --- health ---------------------------------------------------------------


def _finding(findings: list, severity: str, area: str, message: str, **detail: Any) -> None:
    findings.append({"severity": severity, "area": area, "message": message,
                     **({"detail": detail} if detail else {})})


def _check_disks(findings: list) -> None:
    for fs in _disks()["filesystems"]:
        pct, free, total = fs["percent_used"], fs["free_bytes"], fs["total_bytes"]
        where = ", ".join(fs["mountpoints"][:3]) + ("…" if len(fs["mountpoints"]) > 3 else "")
        big = total >= DISK_ABSOLUTE_FLOOR_BYTES
        if pct is not None and (pct >= DISK_CRIT_PERCENT or (big and free < DISK_CRIT_FREE_BYTES)):
            _finding(findings, "critical", "disk", f"{where} is almost full: {fs['free']} free ({pct}% used)")
        elif pct is not None and (pct >= DISK_WARN_PERCENT or (big and free < DISK_WARN_FREE_BYTES)):
            _finding(findings, "warning", "disk", f"{where} is getting full: {fs['free']} free ({pct}% used)")
        ro = [m for m in fs.get("read_only_mountpoints", []) if m in ("/", "/home")]
        if ro:
            _finding(findings, "warning", "disk",
                     f"{', '.join(ro)} is mounted read-only. If that is not intentional, "
                     f"the filesystem may have hit errors and remounted itself read-only; check `dmesg`.")


def _check_memory(findings: list) -> None:
    mem = _memory()
    total, available = mem["total_bytes"], mem["available_bytes"]
    if total and available is not None:
        free_pct = 100.0 * available / total
        if free_pct < MEMORY_CRIT_AVAILABLE_PERCENT:
            _finding(findings, "critical", "memory", f"Only {mem['available']} of {mem['total']} memory available")
        elif free_pct < MEMORY_WARN_AVAILABLE_PERCENT:
            _finding(findings, "warning", "memory", f"Memory is low: {mem['available']} of {mem['total']} available")
    full = ((mem.get("pressure") or {}).get("full") or {}).get("avg60")
    if full is not None and full >= PSI_FULL_WARN_AVG60:
        _finding(findings, "warning", "memory",
                 f"The system spent {full}% of the last minute with every task stalled waiting on memory",
                 psi_full_avg60=full)
    for dev in mem["swap"]["devices"]:
        if dev["kind"] != "zram" and (dev["percent_used"] or 0) >= 50:
            _finding(findings, "warning", "memory",
                     f"Disk swap {dev['device']} is {dev['percent_used']}% used; heavy disk swapping makes everything slow")


def _check_load(findings: list) -> None:
    load = _load()
    per_core = load["per_core_5m"]
    if per_core is None:
        return
    if per_core >= LOAD_CRIT_PER_CORE:
        _finding(findings, "critical", "cpu", f"CPU is heavily oversubscribed: 5-minute load {load['5m']} on {os.cpu_count()} cores")
    elif per_core >= LOAD_WARN_PER_CORE:
        _finding(findings, "warning", "cpu", f"CPU is busy: 5-minute load {load['5m']} on {os.cpu_count()} cores")


def _check_boost(findings: list) -> None:
    cpu = _cpu_static()
    if cpu["boost_enabled"] is False:
        cap = cpu["frequency_mhz"]["max"]
        _finding(findings, "info", "cpu",
                 "CPU boost (turbo) is disabled" + (f", so clocks are capped at {cap} MHz" if cap else "") +
                 ". That can be deliberate -- battery life, thermals, a power-management tool, "
                 "or firmware limiting a laptop running without its battery -- but if not, it "
                 "costs a large share of peak performance.")


def _check_temperatures(findings: list) -> None:
    for sensor in _temperatures()["sensors"]:
        c, crit = sensor["celsius"], sensor["critical_celsius"]
        name = f"{sensor['chip']} {sensor['label']}"
        if crit is not None and c >= crit:
            _finding(findings, "critical", "temperature", f"{name} is at {c}°C, at or above its critical point {crit}°C")
        elif crit is not None and c >= crit - TEMP_MARGIN_TO_CRIT_C:
            _finding(findings, "warning", "temperature", f"{name} is at {c}°C, close to its critical point {crit}°C")
        elif crit is None and c >= TEMP_WARN_NO_CRIT_C:
            _finding(findings, "warning", "temperature", f"{name} is running hot at {c}°C")


def _check_battery(findings: list, unavailable: list) -> None:
    info = _battery()
    if not info["present"]:
        unavailable.append("battery: none present")
    for bat in info["batteries"]:
        pct, status = bat["percent"], bat["status"]
        if status == "Discharging" and pct is not None:
            if pct <= BATTERY_CRIT_PERCENT:
                _finding(findings, "critical", "battery", f"Battery at {pct}% and discharging" +
                         (f" ({bat['time_to_empty']} left)" if bat["time_to_empty"] else ""))
            elif pct <= BATTERY_WARN_PERCENT:
                _finding(findings, "warning", "battery", f"Battery low: {pct}% and discharging" +
                         (f" ({bat['time_to_empty']} left)" if bat["time_to_empty"] else ""))
        if bat["health_percent"] is not None and bat["health_percent"] < BATTERY_WORN_HEALTH_PERCENT:
            _finding(findings, "info", "battery", f"Battery is worn: holds {bat['health_percent']}% of its design capacity")
    for dev in info["peripheral_batteries"]:
        if dev["percent"] is not None and dev["percent"] <= BATTERY_WARN_PERCENT:
            _finding(findings, "info", "battery", f"{dev['model'] or dev['name']} battery is at {dev['percent']}%")


def _check_services(findings: list, unavailable: list) -> None:
    units = _failed_units()
    if not units["available"]:
        unavailable.append("services: systemctl not found")
        return
    for scope in ("system", "user"):
        for unit in units[scope]:
            _finding(findings, "warning", "services",
                     f"{unit['unit']} has failed ({unit['description'] or 'no description'})",
                     scope=scope, investigate=unit["investigate"])


def _check_reboot(findings: list) -> None:
    for reason in _reboot_required()["reasons"]:
        _finding(findings, "warning", "reboot", f"Reboot needed: {reason['reason']}",
                 **{k: v for k, v in reason.items() if k != "reason"})


def _check_time(findings: list) -> None:
    if _time_info()["clock_synchronized"] is False:
        _finding(findings, "info", "time", "The system clock is not synchronized; TLS and logins can fail when it drifts")


def _check_network(findings: list) -> None:
    net = _network()
    state = net["connectivity"]
    if state == "portal":
        _finding(findings, "warning", "network", "A captive portal is intercepting traffic: sign in through a browser")
    elif state in ("none", "limited"):
        _finding(findings, "warning", "network", f"Network connectivity is {state}")


# --- tools ------------------------------------------------------------------


def _clamp(value: Any, low: float, high: float, default: float, name: str) -> float:
    if value is None:
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise StatusError(f"`{name}` must be a number")
    return max(low, min(high, number))


def tool_get_system_overview(arguments: dict) -> dict:
    os_info = _os_info()
    mem = _memory()
    battery = _battery()
    disks = _disks()["filesystems"]
    root_fs = next((f for f in disks if "/" in f["mountpoints"]), None)
    cpu = _cpu_static()
    profile = _run(["powerprofilesctl", "get"])
    return {
        "os": os_info,
        "package_manager": _package_managers(os_info),
        "init_system": _init_system(),
        "environment": _virtualization(),
        "session": _session(),
        "time": _time_info(),
        "uptime": _uptime(),
        "cpu": {"model": cpu["model"], "physical_cores": cpu["physical_cores"],
                "logical_cores": cpu["logical_cores"]},
        "gpus": [{"vendor": g["vendor"], "model": g["model"], "driver": g["driver"]} for g in _gpus()],
        "memory": {"total": mem["total"], "available": mem["available"], "percent_used": mem["percent_used"]},
        "battery": (
            {"percent": battery["batteries"][0]["percent"], "status": battery["batteries"][0]["status"],
             "on_ac_power": battery["on_ac_power"]}
            if battery["present"] else {"present": False, "on_ac_power": battery["on_ac_power"]}
        ),
        "root_filesystem": (
            {"total": root_fs["total"], "free": root_fs["free"], "percent_used": root_fs["percent_used"]}
            if root_fs else None
        ),
        "power_profile": profile.strip() if profile else None,
    }


def tool_get_system_health(arguments: dict) -> dict:
    findings: list = []
    unavailable: list = []
    checks = [
        ("disks", lambda: _check_disks(findings)),
        ("memory", lambda: _check_memory(findings)),
        ("cpu_load", lambda: _check_load(findings)),
        ("cpu_boost", lambda: _check_boost(findings)),
        ("temperatures", lambda: _check_temperatures(findings)),
        ("battery", lambda: _check_battery(findings, unavailable)),
        ("failed_services", lambda: _check_services(findings, unavailable)),
        ("reboot_required", lambda: _check_reboot(findings)),
        ("clock_sync", lambda: _check_time(findings)),
        ("connectivity", lambda: _check_network(findings)),
    ]
    checked = []
    for name, check in checks:
        # One broken check must not hide the other eight.
        try:
            check()
            checked.append(name)
        except Exception as exc:
            unavailable.append(f"{name}: {type(exc).__name__}: {exc}")

    order = {"critical": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda f: order.get(f["severity"], 3))
    worst = findings[0]["severity"] if findings else "ok"
    status = worst if worst in ("critical", "warning") else "ok"
    counts = {s: sum(1 for f in findings if f["severity"] == s) for s in ("critical", "warning", "info")}
    summary = "Nothing needs attention." if not findings else "; ".join(
        f"{n} {s}" for s, n in counts.items() if n
    ) + ". Most urgent: " + findings[0]["message"]

    return {
        "status": status,
        "summary": summary,
        "findings": findings,
        # So "no findings" can be told apart from "never looked".
        "checked": checked,
        "unavailable": unavailable,
    }


def tool_get_battery(arguments: dict) -> dict:
    return _battery()


def tool_get_cpu(arguments: dict) -> dict:
    interval = _clamp(arguments.get("sample_seconds"), 0.1, 3.0, 0.5, "sample_seconds")
    info = _cpu_static()
    info["load"] = _load()
    info["usage"] = _cpu_usage(interval)
    temps = _temperatures()
    package = next((s for s in temps["sensors"]
                    if s["chip"] in ("coretemp", "k10temp", "zenpower", "cpu_thermal")
                    and ("Package" in s["label"] or "Tctl" in s["label"] or "Tdie" in s["label"])), None)
    info["temperature_celsius"] = package["celsius"] if package else None
    return info


def tool_get_memory(arguments: dict) -> dict:
    return _memory()


def tool_get_disks(arguments: dict) -> dict:
    return _disks()


def tool_get_network(arguments: dict) -> dict:
    return _network()


def tool_get_top_processes(arguments: dict) -> dict:
    sort_by = (arguments.get("sort_by") or "cpu").lower()
    if sort_by not in ("cpu", "memory"):
        raise StatusError("`sort_by` must be 'cpu' or 'memory'")
    limit = int(_clamp(arguments.get("limit"), 1, 50, 10, "limit"))
    interval = _clamp(arguments.get("sample_seconds"), 0.1, 3.0, 0.5, "sample_seconds")
    include = arguments.get("include_command_line", False)
    if not isinstance(include, bool):
        raise StatusError("`include_command_line` must be true or false")
    return _top_processes(sort_by, limit, include, interval)


def tool_get_temperatures(arguments: dict) -> dict:
    return _temperatures()


def tool_get_failed_services(arguments: dict) -> dict:
    return _failed_units()


TOOLS = [
    {
        "name": "get_system_overview",
        "description": (
            "Orient yourself on this machine in one call, like neofetch: OS and version, "
            "kernel, hostname, the distro's package manager (use it -- never assume apt), "
            "init system, whether this is a VM or container, the logged-in user and "
            "whether their screen is locked or idle, the local time and timezone, uptime, "
            "CPU, GPUs, memory, battery, root disk space and power profile. Call this "
            "before running commands that depend on the distro, and to answer 'what "
            "time is it' -- you do not otherwise know the user's local time."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_system_health",
        "description": (
            "Check everything that commonly goes wrong and report only what needs "
            "attention: nearly full or read-only disks, low memory or memory stalls, "
            "heavy disk swapping, CPU overload or disabled turbo boost, temperatures near their critical point, "
            "low or worn battery, failed systemd services, a kernel upgraded since boot "
            "(reboot needed), an unsynchronized clock, and captive portals or lost "
            "connectivity. Returns an overall status (ok, warning, critical), findings "
            "most-urgent first, and which checks ran. Call this for 'is anything wrong', "
            "'why is my computer acting up', or before a long or heavy task."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_battery",
        "description": (
            "Battery charge, whether it is charging, time to empty or to full, power "
            "draw, battery health (capacity versus new), cycle count, and whether the "
            "machine is on AC power. Also reports wireless peripherals such as mice and "
            "keyboards separately. Reports present: false on machines with no battery."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_cpu",
        "description": (
            "CPU model, physical and logical cores, whether turbo boost is enabled, current usage overall and per core "
            "(sampled over a short interval), I/O wait, load averages including load "
            "per core, clock speeds, the frequency governor and energy preference, and "
            "package temperature."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "sample_seconds": {
                    "type": "number",
                    "description": "How long to sample usage, 0.1-3.0 seconds. Default 0.5.",
                }
            },
        },
    },
    {
        "name": "get_memory",
        "description": (
            "RAM total, available and used; swap devices, distinguishing zram "
            "(compressed RAM, cheap) from disk swap (slow); and memory pressure -- the "
            "share of time tasks stalled waiting for memory, which is the real measure "
            "of whether memory is a problem. Linux fills idle RAM with cache on purpose, "
            "so judge by `available` and pressure, not `used`."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_disks",
        "description": (
            "Space on every real filesystem: total, used, free and percent used, one "
            "entry per physical device with all its mount points listed together -- so "
            "btrfs subvolumes of the same drive are not counted several times. Excludes "
            "virtual filesystems, snaps and AppImages. Flags read-only mounts. Use for "
            "'how much space do I have' or before downloading or installing something large."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_network",
        "description": (
            "Network state: whether the machine is online, behind a captive portal or "
            "offline; which interface and local IP carry traffic and the gateway; each "
            "interface's state and addresses; the connected Wi-Fi network, its signal "
            "strength and security; and DNS servers. Local information only: this does "
            "not look up the public IP address, because that needs a request to an "
            "outside service."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_top_processes",
        "description": (
            "The processes using the most CPU or memory right now, with PID, name, user, "
            "CPU percent (per core, as top shows it, so a multi-threaded process can "
            "exceed 100%) and memory. Use for 'what is slowing my computer down' or "
            "'what is using all my memory'. Full command lines are omitted unless "
            "include_command_line is true: they can contain passwords or tokens passed as "
            "arguments, so request them only when the process name alone is not enough "
            "(for example, several 'python3' processes)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "sort_by": {"type": "string", "enum": ["cpu", "memory"], "description": "Default 'cpu'."},
                "limit": {"type": "integer", "description": "How many processes, 1-50. Default 10."},
                "include_command_line": {
                    "type": "boolean",
                    "description": "Include each process's full command line. Default false.",
                },
                "sample_seconds": {
                    "type": "number",
                    "description": "CPU sampling interval, 0.1-3.0 seconds. Default 0.5.",
                },
            },
        },
    },
    {
        "name": "get_temperatures",
        "description": (
            "Every temperature sensor the kernel exposes -- CPU package and cores, NVMe "
            "drives, chipset, Wi-Fi -- with each sensor's critical point where it "
            "publishes one, and the hottest reading. Implausible values that some "
            "drivers report as placeholders are filtered out."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_failed_services",
        "description": (
            "systemd services and other units that have failed, both system-wide and "
            "for the user, each with its description and the commands to investigate it. "
            "Use for 'is anything broken' or when a feature stopped working after boot."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
]

_HANDLERS = {
    "get_system_overview": tool_get_system_overview,
    "get_system_health": tool_get_system_health,
    "get_battery": tool_get_battery,
    "get_cpu": tool_get_cpu,
    "get_memory": tool_get_memory,
    "get_disks": tool_get_disks,
    "get_network": tool_get_network,
    "get_top_processes": tool_get_top_processes,
    "get_temperatures": tool_get_temperatures,
    "get_failed_services": tool_get_failed_services,
}


def _result(payload: dict) -> dict:
    return {
        "content": [{"type": "text", "text": json.dumps(payload, indent=2)}],
        "isError": False,
    }


def _error(message: str) -> dict:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def _call_tool(name: str, arguments: dict) -> dict:
    handler = _HANDLERS[name]
    try:
        return _result(handler(arguments))
    except StatusError as exc:
        return _error(str(exc))
    except Exception as exc:
        # Same reasoning as the other first-party servers: a bare traceback on
        # stdout is neither legible nor safe, and everything the caller can act
        # on is raised as StatusError above.
        return _error(f"{name} failed: {type(exc).__name__}: {exc}")


def _handle(request: dict) -> dict | None:
    method = request.get("method", "")
    req_id = request.get("id")
    params = request.get("params") or {}

    # Notifications have no id and require no response.
    if req_id is None:
        return None

    def ok(result: Any) -> dict:
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    def err(code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}

    if method == "initialize":
        return ok({
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "system-status-mcp", "version": "1.0.0"},
        })

    if method == "ping":
        return ok({})

    if method == "tools/list":
        return ok({"tools": TOOLS})

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if name not in _HANDLERS:
            return err(-32601, f"Unknown tool: {name}")
        return ok(_call_tool(name, arguments))

    return err(-32601, f"Method not found: {method}")


def main() -> None:
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            request = json.loads(raw)
        except json.JSONDecodeError:
            continue
        response = _handle(request)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
