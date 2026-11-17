"""System metrics read without special permissions: /proc, /sys/fs/cgroup and
statvfs() on mounted volumes.  Anything unavailable is returned as None and
shown as "n/a" by the UI - never an error."""
from __future__ import annotations

import os
import platform
import socket
import threading
import time

from . import VERSION
from .fsutil import disk_usage, mount_for, mounts

_START = time.time()
_cpu_lock = threading.Lock()
_cpu_prev: dict = {}


def _read(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except OSError:
        return None


def os_name() -> str | None:
    txt = _read("/etc/os-release")
    if not txt:
        return None
    for line in txt.splitlines():
        if line.startswith("PRETTY_NAME="):
            return line.split("=", 1)[1].strip('"')
    return None


def cpu_count() -> int | None:
    n = None
    try:
        n = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        n = os.cpu_count()
    quota = _read("/sys/fs/cgroup/cpu.max")
    if quota and n:
        q, _, period = quota.partition(" ")
        if q != "max" and period:
            try:
                n = max(1, min(n, int(-(-int(q) // int(period)))))
            except ValueError:
                pass
    return n


def cpu_percent() -> float | None:
    """Container CPU usage from cgroup v2 (fallback: host /proc/stat)."""
    now = time.monotonic()
    usage = None
    stat = _read("/sys/fs/cgroup/cpu.stat")
    if stat:
        for line in stat.splitlines():
            if line.startswith("usage_usec "):
                usage = int(line.split()[1]) / 1e6
    with _cpu_lock:
        if usage is not None:
            prev = _cpu_prev.get("cg")
            _cpu_prev["cg"] = (now, usage)
            if prev and now > prev[0]:
                n = cpu_count() or 1
                return round(max(0.0, min(100.0, 100.0 * (usage - prev[1]) / (now - prev[0]) / n)), 1)
            return None
        line = (_read("/proc/stat") or "").splitlines()
        if not line or not line[0].startswith("cpu "):
            return None
        vals = [int(x) for x in line[0].split()[1:]]
        idle, total = vals[3] + (vals[4] if len(vals) > 4 else 0), sum(vals)
        prev = _cpu_prev.get("proc")
        _cpu_prev["proc"] = (idle, total)
        if prev and total > prev[1]:
            return round(100.0 * (1 - (idle - prev[0]) / (total - prev[1])), 1)
        return None


def memory() -> dict:
    total = used = None
    meminfo = _read("/proc/meminfo") or ""
    info = {}
    for line in meminfo.splitlines():
        k, _, v = line.partition(":")
        try:
            info[k] = int(v.split()[0]) * 1024
        except (ValueError, IndexError):
            pass
    if info:
        total = info.get("MemTotal")
        avail = info.get("MemAvailable")
        used = total - avail if total is not None and avail is not None else None
    limited = False
    cg_max = _read("/sys/fs/cgroup/memory.max") or _read("/sys/fs/cgroup/memory/memory.limit_in_bytes")
    cg_cur = _read("/sys/fs/cgroup/memory.current") or _read("/sys/fs/cgroup/memory/memory.usage_in_bytes")
    if cg_max and cg_max != "max":
        try:
            lim = int(cg_max)
            if total is None or lim < total:
                total, limited = lim, True
        except ValueError:
            pass
    if limited and cg_cur:
        try:
            cur = int(cg_cur)
            stat = _read("/sys/fs/cgroup/memory.stat") or ""
            inactive = 0
            for line in stat.splitlines():
                if line.startswith("inactive_file "):
                    inactive = int(line.split()[1])
            used = max(0, cur - inactive)
        except ValueError:
            pass
    pct = round(100.0 * used / total, 1) if total and used is not None else None
    return {"total": total, "used": used, "percent": pct, "cgroup_limited": limited}


def uptime_system() -> float | None:
    txt = _read("/proc/uptime")
    try:
        return float(txt.split()[0]) if txt else None
    except ValueError:
        return None


def uptime_container() -> float | None:
    """Age of PID 1 of the container (falls back to this process)."""
    up = uptime_system()
    stat = _read("/proc/1/stat")
    try:
        ticks = os.sysconf("SC_CLK_TCK")
    except (ValueError, OSError):
        ticks = 100
    if up is not None and stat:
        try:
            start = int(stat.rsplit(")", 1)[1].split()[19]) / ticks
            return max(0.0, up - start)
        except (IndexError, ValueError):
            pass
    return time.time() - _START


def level(free_pct: float | None, warn: float, crit: float) -> str:
    if free_pct is None:
        return "unknown"
    if free_pct < crit:
        return "critical"
    if free_pct < warn:
        return "warning"
    return "ok"


def volume(path: str, warn: float, crit: float, table=None) -> dict:
    du = disk_usage(path) if os.path.exists(path) else None
    m = mount_for(path, table) if os.path.exists(path) else None
    out = {"mount_point": m["mount_point"] if m else None, "fstype": m["fstype"] if m else None,
           "read_only": (("ro" in m["options"]) if m else None) if du is None else (du["read_only"] or ("ro" in (m or {}).get("options", [])))}
    if du:
        out.update(total=du["total"], used=du["used"], free=du["free"], used_pct=du["used_pct"], free_pct=du["free_pct"])
    else:
        out.update(total=None, used=None, free=None, used_pct=None, free_pct=None)
    out["level"] = level(out["free_pct"], warn, crit)
    return out


def system(settings, db_status: dict, running_jobs: int) -> dict:
    load = None
    try:
        load = [round(x, 2) for x in os.getloadavg()]
    except OSError:
        pass
    uname = platform.uname()
    table = mounts()
    return {
        "host": settings.host_label or socket.gethostname() or None,
        "os": os_name(), "kernel": uname.release or None, "arch": uname.machine or None,
        "cpu": {"cores": cpu_count(), "percent": cpu_percent(), "load": load},
        "memory": memory(),
        "volumes": {"data": volume(str(settings.data_dir), settings.disk_warn, settings.disk_crit, table),
                    "state": volume(str(settings.state_dir), settings.disk_warn, settings.disk_crit, table)},
        "uptime": {"system": uptime_system(), "container": uptime_container()},
        "version": VERSION, "database": db_status, "jobs_running": running_jobs,
        "thresholds": {"warn": settings.disk_warn, "crit": settings.disk_crit},
    }


def source_disks(settings, areas: list[dict], indexed_sizes: dict, display) -> list[dict]:
    table = mounts()
    out = []
    for a in areas:
        src = volume(a["path"], settings.disk_warn, settings.disk_crit, table)
        dst = volume(a["dest"], settings.disk_warn, settings.disk_crit, table) if os.path.exists(a["dest"]) else \
            volume(os.path.dirname(a["dest"]), settings.disk_warn, settings.disk_crit, table)
        same = None
        try:
            ref = a["dest"] if os.path.exists(a["dest"]) else os.path.dirname(a["dest"])
            same = os.stat(a["path"]).st_dev == os.stat(ref).st_dev
        except OSError:
            pass
        out.append({"source_id": a["id"], "label": a["label"], "source_path": display(a["path"]), "dest_path": display(a["dest"]),
                    "source": src, "dest": dst, "same_volume": same, "indexed_bytes": indexed_sizes.get(a["id"])})
    return out
