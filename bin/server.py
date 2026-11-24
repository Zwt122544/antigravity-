#!/usr/bin/env python3
"""
Linux Server Resource Monitor - Antigravity MCP App Server.

Pure Python 3 standard-library stdio MCP server that continuously samples
Linux system telemetry (CPU, per-core load, Memory/Swap, NVIDIA GPUs & VRAM,
Network RX/TX speed, Disk I/O & mounts, Temperatures, Top processes) on a 1Hz
background thread, serves an interactive bilingual MCP App dashboard (`ui://`),
and supports automatic GitHub version checking & one-click hot updates.
"""

import argparse
import collections
import hashlib
import io
import json
import os
import pwd
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

APP_URI = "ui://linux-monitor/app.html"
PLUGIN_ROOT = Path(__file__).resolve().parent.parent
UI_HTML_PATH = PLUGIN_ROOT / "ui" / "app.html"
UI_SDK_PATH = PLUGIN_ROOT / "ui" / "ext-apps.mjs"
MANIFEST_PATH = PLUGIN_ROOT / "plugin.json"
SERVER_NAME = "linux-monitor"

HISTORY_LEN = 120  # 2 minutes at 1 Hz
UPDATE_CHECK_TTL = 1800  # 30 min auto-check cache
CLK_TCK = os.sysconf(os.sysconf_names["SC_CLK_TCK"]) if hasattr(os, "sysconf") else 100
PAGE_SIZE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096

PHYS_DISK_RE = re.compile(r"^(sd[a-z]+|hd[a-z]+|vd[a-z]+|xvd[a-z]+|nvme\d+n\d+|mmcblk\d+)$")
VIRTUAL_NET_PREFIXES = ("lo", "docker", "br-", "veth", "virbr", "flannel", "cali", "cni", "kube")
REAL_FS_TYPES = {
    "ext2", "ext3", "ext4", "xfs", "btrfs", "zfs", "ntfs", "vfat", "exfat",
    "nfs", "nfs4", "cifs", "fuse.sshfs", "gpfs", "lustre", "ceph", "glusterfs",
}
GITHUB_REPO_RE = re.compile(r"github\.com[:/]([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$")
OWNER_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

# Files/directories that hot-update is allowed to replace inside PLUGIN_ROOT
UPDATABLE_FILES = ("plugin.json", "mcp_config.json", "README.md", "LICENSE")
UPDATABLE_DIRS = ("bin", "ui", "assets", "skills")


def safe_float(val, default=0.0):
    try:
        s = str(val).strip()
        if not s or s in ("[N/A]", "N/A", "[Not Supported]", "None"):
            return default
        return float(s)
    except Exception:
        return default


def safe_int(val, default=0):
    try:
        s = str(val).strip()
        if not s or s in ("[N/A]", "N/A", "[Not Supported]", "None"):
            return default
        return int(float(s))
    except Exception:
        return default


def read_text(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception:
        return ""


def file_sha256(path):
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return ""


def parse_semver(v):
    s = str(v or "").strip().lstrip("vV")
    nums = [int(x) for x in re.findall(r"\d+", s)[:3]]
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums)


def normalize_github_repo(val):
    s = str(val or "").strip()
    if not s:
        return ""
    if OWNER_REPO_RE.match(s):
        return s[:-4] if s.endswith(".git") else s
    m = GITHUB_REPO_RE.search(s)
    if m:
        return m.group(1)
    return ""


def read_manifest():
    try:
        return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"name": SERVER_NAME, "version": "1.1.0"}


def get_local_version():
    m = read_manifest()
    return str(m.get("version") or "1.1.0").strip()


def get_manifest_repo():
    m = read_manifest()
    repo = m.get("repository")
    if isinstance(repo, dict):
        repo = repo.get("url", "")
    return normalize_github_repo(repo) or normalize_github_repo(m.get("homepage", ""))


def http_get_bytes(url, timeout=12, headers=None):
    """Fetch URL with direct-first fallback so broken local proxies don't block GitHub."""
    req_headers = {
        "User-Agent": f"antigravity-linux-monitor/{get_local_version()}",
        "Accept": "application/vnd.github+json",
    }
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, headers=req_headers)

    # Try default opener first, then direct (no-proxy) if proxy fails, and vice versa
    openers = [
        urllib.request.build_opener(urllib.request.ProxyHandler({})),
        urllib.request.build_opener(),
    ]
    last_err = None
    for opener in openers:
        try:
            with opener.open(req, timeout=timeout) as resp:
                return resp.read(), resp.headers
        except urllib.error.HTTPError:
            raise
        except Exception as exc:
            last_err = exc
    raise last_err or RuntimeError("Network request failed")


class SettingsStore:
    """Persists user preferences (language, optional custom GitHub repo) in PLUGIN_DATA."""

    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.data_dir / "settings.json"
        self._lock = threading.Lock()
        self._data = self._load()

    def _load(self):
        defaults = {
            "lang": "",           # "" = auto from host locale, "zh", or "en"
            "github_repo": "",    # optional override "owner/repo"
        }
        try:
            if self.path.exists():
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    defaults.update(loaded)
        except Exception:
            pass
        return defaults

    def get(self):
        with self._lock:
            out = dict(self._data)
        out["effective_repo"] = normalize_github_repo(out.get("github_repo")) or get_manifest_repo()
        out["current_version"] = get_local_version()
        return out

    def update(self, patch):
        with self._lock:
            if "lang" in patch:
                lang = str(patch["lang"] or "").strip().lower()
                if lang in ("", "zh", "en"):
                    self._data["lang"] = lang
            if "github_repo" in patch:
                raw = str(patch["github_repo"] or "").strip()
                self._data["github_repo"] = normalize_github_repo(raw) if raw else ""
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)
        return self.get()


class PluginUpdater:
    """Checks GitHub Releases (with main-branch plugin.json fallback) and performs atomic hot updates."""

    def __init__(self, settings_store):
        self.settings = settings_store
        self._lock = threading.Lock()
        self._cache = None
        self._cache_ts = 0.0
        self._cache_repo = ""

        # Trigger a non-blocking background update check 3 seconds after boot
        threading.Thread(target=self._bg_initial_check, name="update-check", daemon=True).start()

    def _bg_initial_check(self):
        time.sleep(3.0)
        try:
            self.check_update(force=False)
        except Exception:
            pass

    def get_cached_status(self):
        repo = self.settings.get()["effective_repo"]
        cur = get_local_version()
        with self._lock:
            if self._cache and self._cache_repo == repo:
                res = dict(self._cache)
                res["current_version"] = cur
                return res
        return {
            "status": "unconfigured" if not repo else "idle",
            "repo": repo,
            "current_version": cur,
            "latest_version": "",
            "has_update": False,
            "release_name": "",
            "release_notes": "",
            "release_url": f"https://github.com/{repo}" if repo else "",
            "tarball_url": "",
            "published_at": "",
            "checked_at": 0,
            "error": "",
        }

    def check_update(self, force=False, repo_override=""):
        repo = normalize_github_repo(repo_override) or self.settings.get()["effective_repo"]
        cur = get_local_version()

        if not repo:
            res = {
                "status": "unconfigured",
                "repo": "",
                "current_version": cur,
                "latest_version": "",
                "has_update": False,
                "release_name": "",
                "release_notes": "",
                "release_url": "",
                "tarball_url": "",
                "published_at": "",
                "checked_at": time.time(),
                "error": "",
            }
            with self._lock:
                self._cache = res
                self._cache_ts = time.time()
                self._cache_repo = ""
            return res

        with self._lock:
            if (
                not force
                and self._cache
                and self._cache_repo == repo
                and (time.time() - self._cache_ts) < UPDATE_CHECK_TTL
            ):
                return dict(self._cache)

        latest_ver = ""
        release_name = ""
        release_notes = ""
        release_url = f"https://github.com/{repo}/releases"
        tarball_url = ""
        published_at = ""
        source = "release"

        try:
            # 1) Try latest GitHub Release first
            api_url = f"https://api.github.com/repos/{repo}/releases/latest"
            try:
                raw, _ = http_get_bytes(api_url, timeout=10)
                rel = json.loads(raw.decode("utf-8"))
                tag = str(rel.get("tag_name") or "").strip()
                latest_ver = tag.lstrip("vV") or str(rel.get("name") or "").strip()
                release_name = str(rel.get("name") or tag or latest_ver)
                release_notes = str(rel.get("body") or "").strip()[:2000]
                release_url = str(rel.get("html_url") or release_url)
                tarball_url = str(rel.get("tarball_url") or f"https://api.github.com/repos/{repo}/tarball/{tag}")
                published_at = str(rel.get("published_at") or "")
            except urllib.error.HTTPError as he:
                if he.code != 404:
                    raise
                # 2) No Release yet -> fallback to main branch plugin.json
                source = "branch"
                manifest_url = f"https://raw.githubusercontent.com/{repo}/main/plugin.json"
                raw, _ = http_get_bytes(manifest_url, timeout=10)
                remote_m = json.loads(raw.decode("utf-8"))
                latest_ver = str(remote_m.get("version") or "").strip()
                release_name = f"main (v{latest_ver})" if latest_ver else "main"
                release_notes = "Latest commit on main branch (no GitHub Release tag found yet)."
                release_url = f"https://github.com/{repo}"
                tarball_url = f"https://api.github.com/repos/{repo}/tarball/main"

            has_update = bool(latest_ver and parse_semver(latest_ver) > parse_semver(cur))
            res = {
                "status": "update_available" if has_update else "up_to_date",
                "source": source,
                "repo": repo,
                "current_version": cur,
                "latest_version": latest_ver,
                "has_update": has_update,
                "release_name": release_name,
                "release_notes": release_notes,
                "release_url": release_url,
                "tarball_url": tarball_url,
                "published_at": published_at,
                "checked_at": time.time(),
                "error": "",
            }
        except Exception as exc:
            res = {
                "status": "error",
                "repo": repo,
                "current_version": cur,
                "latest_version": "",
                "has_update": False,
                "release_name": "",
                "release_notes": "",
                "release_url": f"https://github.com/{repo}",
                "tarball_url": "",
                "published_at": "",
                "checked_at": time.time(),
                "error": str(exc),
            }

        with self._lock:
            self._cache = res
            self._cache_ts = time.time()
            self._cache_repo = repo
        return dict(res)

    def apply_update(self, repo_override="", force=False):
        check = self.check_update(force=True, repo_override=repo_override)
        if check["status"] == "unconfigured":
            return {"ok": False, "error": "GitHub repository is not configured."}
        if check["status"] == "error":
            return {"ok": False, "error": f"Failed to check GitHub: {check['error']}"}
        if not check["has_update"] and not force:
            return {
                "ok": True,
                "updated": False,
                "message": "Already on the latest version.",
                "from_version": check["current_version"],
                "to_version": check["latest_version"] or check["current_version"],
                "server_restarted": False,
            }

        tarball_url = check["tarball_url"]
        if not tarball_url:
            return {"ok": False, "error": "No downloadable archive URL found."}

        old_version = check["current_version"]
        old_server_sha = file_sha256(Path(__file__).resolve())
        old_ui_sha = file_sha256(UI_HTML_PATH)

        try:
            raw_tar, _ = http_get_bytes(tarball_url, timeout=30)
        except Exception as exc:
            return {"ok": False, "error": f"Failed to download update archive: {exc}"}

        stage_dir = Path(tempfile.mkdtemp(prefix="lm_update_", dir=str(self.settings.data_dir)))
        try:
            with tarfile.open(fileobj=io.BytesIO(raw_tar), mode="r:gz") as tf:
                # Safe extraction: reject path traversal or symlinks escaping stage_dir
                for member in tf.getmembers():
                    if member.issym() or member.islnk():
                        continue
                    target = (stage_dir / member.name).resolve()
                    if not str(target).startswith(str(stage_dir.resolve())):
                        raise RuntimeError(f"Unsafe archive path: {member.name}")
                    tf.extract(member, path=stage_dir)

            # Find extracted top-level directory containing plugin.json
            src_root = None
            for cand in [stage_dir] + list(stage_dir.iterdir()):
                if cand.is_dir() and (cand / "plugin.json").exists():
                    src_root = cand
                    break
            if not src_root:
                return {"ok": False, "error": "Downloaded archive does not contain plugin.json."}

            # Validate mandatory plugin files exist before touching live installation
            required = ("plugin.json", "mcp_config.json", "bin/server.py", "ui/app.html")
            for rel in required:
                if not (src_root / rel).exists():
                    return {"ok": False, "error": f"Update archive missing required file: {rel}"}

            # Validate JSON integrity
            new_manifest = json.loads((src_root / "plugin.json").read_text(encoding="utf-8"))
            json.loads((src_root / "mcp_config.json").read_text(encoding="utf-8"))

            # Preserve ext-apps.mjs if the remote repo omits the vendored SDK bundle
            sdk_backup = None
            if not (src_root / "ui" / "ext-apps.mjs").exists() and UI_SDK_PATH.exists():
                sdk_backup = UI_SDK_PATH.read_bytes()

            # Replace allowed files
            for fname in UPDATABLE_FILES:
                src_f = src_root / fname
                if src_f.is_file():
                    dst_f = PLUGIN_ROOT / fname
                    tmp_f = dst_f.with_suffix(dst_f.suffix + ".upd")
                    shutil.copy2(src_f, tmp_f)
                    os.replace(tmp_f, dst_f)

            # Replace allowed directories safely
            for dname in UPDATABLE_DIRS:
                src_d = src_root / dname
                if src_d.is_dir():
                    dst_d = PLUGIN_ROOT / dname
                    dst_d.mkdir(parents=True, exist_ok=True)
                    for root, _, files in os.walk(src_d):
                        rel_dir = Path(root).relative_to(src_d)
                        out_dir = dst_d / rel_dir
                        out_dir.mkdir(parents=True, exist_ok=True)
                        for f in files:
                            sf = Path(root) / f
                            df = out_dir / f
                            tf_tmp = df.with_suffix(df.suffix + ".upd")
                            shutil.copy2(sf, tf_tmp)
                            os.replace(tf_tmp, df)

            if sdk_backup is not None and not UI_SDK_PATH.exists():
                UI_SDK_PATH.write_bytes(sdk_backup)

            # Ensure server.py stays executable
            try:
                os.chmod(PLUGIN_ROOT / "bin" / "server.py", 0o755)
            except Exception:
                pass

            new_version = str(new_manifest.get("version") or check["latest_version"] or old_version)
            server_changed = file_sha256(Path(__file__).resolve()) != old_server_sha
            ui_changed = file_sha256(UI_HTML_PATH) != old_ui_sha

            with self._lock:
                self._cache = {
                    **check,
                    "status": "up_to_date",
                    "current_version": new_version,
                    "latest_version": new_version,
                    "has_update": False,
                    "checked_at": time.time(),
                }

            return {
                "ok": True,
                "updated": True,
                "from_version": old_version,
                "to_version": new_version,
                "server_changed": server_changed,
                "ui_changed": ui_changed,
                "server_restarted": server_changed,
            }
        except Exception as exc:
            return {"ok": False, "error": f"Failed to apply update: {exc}"}
        finally:
            shutil.rmtree(stage_dir, ignore_errors=True)


def get_static_host_info():
    uname = os.uname()
    cpu_model = "Unknown CPU"
    physical_ids = set()
    core_pairs = set()
    logical_cores = os.cpu_count() or 1
    cpuinfo = read_text("/proc/cpuinfo")
    cur_phys = None
    for line in cpuinfo.splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        k = k.strip()
        v = v.strip()
        if k == "model name" and cpu_model == "Unknown CPU":
            cpu_model = v
        elif k == "physical id":
            cur_phys = v
            physical_ids.add(v)
        elif k == "core id" and cur_phys is not None:
            core_pairs.add((cur_phys, v))

    return {
        "hostname": socket.gethostname(),
        "os": f"{uname.sysname} {uname.release}",
        "arch": uname.machine,
        "cpu_model": cpu_model,
        "sockets": max(len(physical_ids), 1),
        "physical_cores": len(core_pairs) if core_pairs else logical_cores,
        "logical_cores": logical_cores,
    }


def read_proc_stat():
    text = read_text("/proc/stat")
    total = None
    cores = {}
    for line in text.splitlines():
        if line.startswith("cpu "):
            total = [int(x) for x in line.split()[1:]]
        elif line.startswith("cpu"):
            parts = line.split()
            idx_str = parts[0][3:]
            if idx_str.isdigit():
                cores[int(idx_str)] = [int(x) for x in parts[1:]]
    return total, [cores[i] for i in sorted(cores.keys())]


def calc_cpu_pct(prev, curr):
    if not prev or not curr:
        return {"total": 0.0, "user": 0.0, "system": 0.0, "iowait": 0.0, "irq": 0.0, "steal": 0.0, "idle": 100.0}
    n = min(len(prev), len(curr))
    deltas = [max(0, curr[i] - prev[i]) for i in range(n)]
    total_d = sum(deltas)
    if total_d <= 0:
        return {"total": 0.0, "user": 0.0, "system": 0.0, "iowait": 0.0, "irq": 0.0, "steal": 0.0, "idle": 100.0}

    user = (deltas[0] + (deltas[1] if n > 1 else 0)) / total_d * 100.0
    system = (deltas[2] if n > 2 else 0) / total_d * 100.0
    idle = (deltas[3] if n > 3 else 0) / total_d * 100.0
    iowait = (deltas[4] if n > 4 else 0) / total_d * 100.0
    irq = ((deltas[5] if n > 5 else 0) + (deltas[6] if n > 6 else 0)) / total_d * 100.0
    steal = (deltas[7] if n > 7 else 0) / total_d * 100.0
    busy = max(0.0, min(100.0, 100.0 - idle - iowait))
    return {
        "total": round(busy, 1),
        "user": round(user, 1),
        "system": round(system, 1),
        "iowait": round(iowait, 1),
        "irq": round(irq, 1),
        "steal": round(steal, 1),
        "idle": round(idle, 1),
    }


def read_cpu_freq():
    freqs = []
    cpu_base = Path("/sys/devices/system/cpu")
    try:
        for p in cpu_base.glob("cpu[0-9]*/cpufreq/scaling_cur_freq"):
            v = read_text(p).strip()
            if v.isdigit():
                freqs.append(int(v) / 1000.0)
    except Exception:
        pass
    if not freqs:
        for line in read_text("/proc/cpuinfo").splitlines():
            if line.startswith("cpu MHz"):
                try:
                    freqs.append(float(line.split(":", 1)[1].strip()))
                except Exception:
                    pass
    if not freqs:
        return {"avg_mhz": 0.0, "min_mhz": 0.0, "max_mhz": 0.0}
    return {
        "avg_mhz": round(sum(freqs) / len(freqs), 0),
        "min_mhz": round(min(freqs), 0),
        "max_mhz": round(max(freqs), 0),
    }


def read_temperatures():
    temps = []
    hwmon_root = Path("/sys/class/hwmon")
    if hwmon_root.exists():
        for hw in sorted(hwmon_root.iterdir()):
            name = read_text(hw / "name").strip()
            for inp in sorted(hw.glob("temp*_input")):
                raw = read_text(inp).strip()
                if not raw:
                    continue
                val = safe_float(raw, -1) / 1000.0
                if val <= 0 or val > 150:
                    continue
                lbl_file = inp.with_name(inp.name.replace("_input", "_label"))
                lbl = read_text(lbl_file).strip() or name or inp.stem
                temps.append({"sensor": name or "hwmon", "label": lbl, "temp_c": round(val, 1)})
    if not temps:
        tz_root = Path("/sys/class/thermal")
        if tz_root.exists():
            for tz in sorted(tz_root.glob("thermal_zone*")):
                raw = read_text(tz / "temp").strip()
                t_type = read_text(tz / "type").strip() or tz.name
                val = safe_float(raw, -1) / 1000.0
                if 0 < val <= 150:
                    temps.append({"sensor": tz.name, "label": t_type, "temp_c": round(val, 1)})
    return temps[:12]


def read_memory():
    info = {}
    for line in read_text("/proc/meminfo").splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        parts = v.strip().split()
        if parts and parts[0].isdigit():
            info[k.strip()] = int(parts[0]) * 1024

    total = info.get("MemTotal", 0)
    free = info.get("MemFree", 0)
    avail = info.get("MemAvailable", free)
    buffers = info.get("Buffers", 0)
    cached = info.get("Cached", 0) + info.get("SReclaimable", 0)
    used = max(0, total - avail)
    swap_total = info.get("SwapTotal", 0)
    swap_free = info.get("SwapFree", 0)
    swap_used = max(0, swap_total - swap_free)
    return {
        "total_bytes": total,
        "used_bytes": used,
        "available_bytes": avail,
        "free_bytes": free,
        "buffers_bytes": buffers,
        "cached_bytes": cached,
        "percent": round((used / total * 100.0) if total > 0 else 0.0, 1),
        "swap_total_bytes": swap_total,
        "swap_used_bytes": swap_used,
        "swap_free_bytes": swap_free,
        "swap_percent": round((swap_used / swap_total * 100.0) if swap_total > 0 else 0.0, 1),
    }


def read_load_and_uptime():
    load_parts = read_text("/proc/loadavg").strip().split()
    l1 = safe_float(load_parts[0]) if len(load_parts) > 0 else 0.0
    l5 = safe_float(load_parts[1]) if len(load_parts) > 1 else 0.0
    l15 = safe_float(load_parts[2]) if len(load_parts) > 2 else 0.0
    running, total_procs = 0, 0
    if len(load_parts) > 3 and "/" in load_parts[3]:
        r_s, t_s = load_parts[3].split("/", 1)
        running = safe_int(r_s)
        total_procs = safe_int(t_s)
    up_parts = read_text("/proc/uptime").strip().split()
    uptime_sec = int(safe_float(up_parts[0])) if up_parts else 0
    return {
        "load_1m": round(l1, 2),
        "load_5m": round(l5, 2),
        "load_15m": round(l15, 2),
        "running_procs": running,
        "total_procs": total_procs,
        "uptime_sec": uptime_sec,
    }


def read_net_counters():
    result = {}
    lines = read_text("/proc/net/dev").splitlines()
    for line in lines[2:]:
        if ":" not in line:
            continue
        iface, data = line.split(":", 1)
        iface = iface.strip()
        vals = data.split()
        if len(vals) < 16:
            continue
        result[iface] = (
            int(vals[0]), int(vals[1]), int(vals[2]) + int(vals[3]),
            int(vals[8]), int(vals[9]), int(vals[10]) + int(vals[11]),
        )
    return result


def get_iface_meta(iface):
    base = Path("/sys/class/net") / iface
    state = read_text(base / "operstate").strip() or "unknown"
    speed = safe_int(read_text(base / "speed").strip(), 0)
    return state, max(0, speed)


def read_disk_counters():
    result = {}
    for line in read_text("/proc/diskstats").splitlines():
        parts = line.split()
        if len(parts) < 14:
            continue
        name = parts[2]
        if not PHYS_DISK_RE.match(name):
            continue
        result[name] = (int(parts[3]), int(parts[5]), int(parts[7]), int(parts[9]))
    return result


def read_disk_mounts():
    mounts = []
    seen_devs = set()
    seen_mounts = set()
    for line in read_text("/proc/mounts").splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        dev, mnt, fstype = parts[0], parts[1], parts[2]
        if mnt in seen_mounts:
            continue
        is_root = (mnt == "/")
        if not is_root and fstype not in REAL_FS_TYPES:
            continue
        if any(mnt.startswith(p) for p in ("/snap", "/boot/efi", "/run", "/sys", "/proc", "/dev")):
            continue
        if dev.startswith("/dev/") and dev in seen_devs and not is_root:
            continue
        try:
            st = os.statvfs(mnt)
            total = st.f_blocks * st.f_frsize
            free = st.f_bfree * st.f_frsize
            avail = st.f_bavail * st.f_frsize
            used = max(0, total - free)
            if total <= 0:
                continue
            seen_mounts.add(mnt)
            if dev.startswith("/dev/"):
                seen_devs.add(dev)
            mounts.append({
                "device": dev,
                "mount": mnt,
                "fstype": fstype,
                "total_bytes": total,
                "used_bytes": used,
                "avail_bytes": avail,
                "percent": round(used / total * 100.0, 1),
            })
        except Exception:
            continue
    mounts.sort(key=lambda x: (x["mount"] != "/", -x["total_bytes"]))
    return mounts[:8]


def lookup_proc_info(pid):
    pdir = Path(f"/proc/{pid}")
    name = read_text(pdir / "comm").strip()
    cmdline = read_text(pdir / "cmdline").replace("\x00", " ").strip()
    user = ""
    for line in read_text(pdir / "status").splitlines():
        if line.startswith("Uid:"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                uid = int(parts[1])
                try:
                    user = pwd.getpwuid(uid).pw_name
                except Exception:
                    user = str(uid)
            break
    if not name and cmdline:
        name = os.path.basename(cmdline.split()[0])
    return name or f"pid:{pid}", cmdline[:160], user


def read_nvidia_gpus():
    gpu_fields = [
        "index", "name", "uuid", "utilization.gpu", "utilization.memory",
        "memory.used", "memory.total", "temperature.gpu",
        "power.draw", "power.limit", "fan.speed",
        "clocks.current.graphics", "clocks.max.graphics",
        "clocks.current.memory", "clocks.max.memory",
        "pstate", "pcie.link.gen.current", "pcie.link.width.current",
    ]
    try:
        proc = subprocess.run(
            ["nvidia-smi", f"--query-gpu={','.join(gpu_fields)}", "--format=csv,noheader,nounits"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=2.5,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return [], {}
    except Exception:
        return [], {}

    gpus = []
    uuid_to_gpu = {}
    for line in proc.stdout.strip().splitlines():
        cols = [c.strip() for c in line.split(",")]
        if len(cols) < 18:
            continue
        mem_used = safe_float(cols[5])
        mem_total = safe_float(cols[6], 1.0)
        pwr = safe_float(cols[8])
        pwr_lim = safe_float(cols[9], 1.0)
        g = {
            "index": safe_int(cols[0]),
            "name": cols[1],
            "uuid": cols[2],
            "util_gpu": round(safe_float(cols[3]), 1),
            "util_mem": round(safe_float(cols[4]), 1),
            "mem_used_mb": round(mem_used, 0),
            "mem_total_mb": round(mem_total, 0),
            "mem_percent": round((mem_used / mem_total * 100.0) if mem_total > 0 else 0.0, 1),
            "temp_c": safe_int(cols[7]),
            "power_w": round(pwr, 1),
            "power_limit_w": round(pwr_lim, 1),
            "power_percent": round((pwr / pwr_lim * 100.0) if pwr_lim > 0 else 0.0, 1),
            "fan_percent": safe_int(cols[10]),
            "clock_gfx_mhz": safe_int(cols[11]),
            "clock_gfx_max_mhz": safe_int(cols[12]),
            "clock_mem_mhz": safe_int(cols[13]),
            "clock_mem_max_mhz": safe_int(cols[14]),
            "pstate": cols[15],
            "pcie_gen": safe_int(cols[16]),
            "pcie_width": safe_int(cols[17]),
            "processes": [],
        }
        gpus.append(g)
        uuid_to_gpu[g["uuid"]] = g

    pid_vram = collections.defaultdict(float)
    try:
        pproc = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=gpu_uuid,pid,process_name,used_gpu_memory", "--format=csv,noheader,nounits"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=2.0,
        )
        if pproc.returncode == 0 and pproc.stdout.strip():
            for line in pproc.stdout.strip().splitlines():
                cols = [c.strip() for c in line.split(",")]
                if len(cols) < 4:
                    continue
                guuid, pid_s, pname, vram_s = cols[0], cols[1], cols[2], cols[3]
                pid = safe_int(pid_s)
                vram = round(safe_float(vram_s), 0)
                if pid > 0:
                    pid_vram[pid] += vram
                    proc_name, cmdline, user = lookup_proc_info(pid)
                    display_name = os.path.basename(pname) if (pname and pname != "[Not Found]") else proc_name
                    if guuid in uuid_to_gpu:
                        uuid_to_gpu[guuid]["processes"].append({
                            "pid": pid, "name": display_name, "user": user, "cmdline": cmdline, "vram_mb": vram,
                        })
    except Exception:
        pass

    return gpus, dict(pid_vram)


class TelemetryCollector:
    """Continuously samples Linux server telemetry at 1 Hz in a background thread."""

    def __init__(self):
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.static_info = get_static_host_info()
        self._prev_cpu_total = None
        self._prev_cpu_cores = None
        self._prev_net = None
        self._prev_disk = None
        self._prev_ts = None
        self._prev_proc_ticks = {}
        self._prev_proc_ts = None
        self._uid_cache = {}
        self._tick = 0

        self.latest = None
        self.top_processes = []
        self.mounts = []
        self.history = collections.deque(maxlen=HISTORY_LEN)

        self._prime()
        self._thread = threading.Thread(target=self._run_loop, name="telemetry-1hz", daemon=True)
        self._thread.start()

    def _uid_to_name(self, uid):
        if uid not in self._uid_cache:
            try:
                self._uid_cache[uid] = pwd.getpwuid(uid).pw_name
            except Exception:
                self._uid_cache[uid] = str(uid)
        return self._uid_cache[uid]

    def _sample_processes(self, mem_total_bytes, pid_vram):
        now = time.monotonic()
        dt = (now - self._prev_proc_ts) if self._prev_proc_ts else 0.0
        new_ticks = {}
        procs = []
        try:
            entries = os.listdir("/proc")
        except Exception:
            return self.top_processes

        for entry in entries:
            if not entry.isdigit():
                continue
            pid = int(entry)
            stat_s = read_text(f"/proc/{pid}/stat")
            if not stat_s:
                continue
            rp = stat_s.rfind(")")
            lp = stat_s.find("(")
            if lp < 0 or rp < 0:
                continue
            comm = stat_s[lp + 1:rp]
            rest = stat_s[rp + 2:].split()
            if len(rest) < 22:
                continue
            state = rest[0]
            total_ticks = int(rest[11]) + int(rest[12])
            rss_pages = int(rest[21])
            new_ticks[pid] = total_ticks

            cpu_pct = 0.0
            if dt > 0.05 and pid in self._prev_proc_ticks:
                d_ticks = max(0, total_ticks - self._prev_proc_ticks[pid])
                cpu_pct = (d_ticks / CLK_TCK) / dt * 100.0

            rss_bytes = max(0, rss_pages) * PAGE_SIZE
            mem_pct = (rss_bytes / mem_total_bytes * 100.0) if mem_total_bytes > 0 else 0.0
            vram_mb = pid_vram.get(pid, 0.0)

            if cpu_pct >= 0.1 or mem_pct >= 0.1 or vram_mb > 0:
                procs.append((cpu_pct, mem_pct, vram_mb, pid, comm, state, rss_bytes))

        self._prev_proc_ticks = new_ticks
        self._prev_proc_ts = now

        by_cpu = sorted(procs, key=lambda x: x[0], reverse=True)[:12]
        by_mem = sorted(procs, key=lambda x: x[1], reverse=True)[:10]
        by_gpu = sorted([p for p in procs if p[2] > 0], key=lambda x: x[2], reverse=True)[:10]
        merged = {item[3]: item for item in by_cpu + by_mem + by_gpu}

        enriched = []
        for cpu_pct, mem_pct, vram_mb, pid, comm, state, rss_bytes in merged.values():
            user = ""
            try:
                user = self._uid_to_name(os.stat(f"/proc/{pid}").st_uid)
            except Exception:
                pass
            cmdline = read_text(f"/proc/{pid}/cmdline").replace("\x00", " ").strip()
            enriched.append({
                "pid": pid,
                "name": comm,
                "user": user,
                "state": state,
                "cpu_percent": round(cpu_pct, 1),
                "mem_percent": round(mem_pct, 2),
                "rss_bytes": rss_bytes,
                "vram_mb": round(vram_mb, 0),
                "cmdline": cmdline[:140] if cmdline else comm,
            })

        enriched.sort(key=lambda x: (x["cpu_percent"], x["vram_mb"], x["mem_percent"]), reverse=True)
        return enriched[:18]

    def _prime(self):
        self._prev_cpu_total, self._prev_cpu_cores = read_proc_stat()
        self._prev_net = read_net_counters()
        self._prev_disk = read_disk_counters()
        self._prev_ts = time.monotonic()
        self.mounts = read_disk_mounts()
        time.sleep(0.15)
        self._sample_once()

    def _sample_once(self):
        now_mono = time.monotonic()
        now_wall = time.time()
        dt = max(0.05, now_mono - self._prev_ts) if self._prev_ts else 1.0

        cpu_total_raw, cpu_cores_raw = read_proc_stat()
        cpu_summary = calc_cpu_pct(self._prev_cpu_total, cpu_total_raw)
        if self._prev_cpu_cores and len(self._prev_cpu_cores) == len(cpu_cores_raw):
            per_core = [calc_cpu_pct(p, c)["total"] for p, c in zip(self._prev_cpu_cores, cpu_cores_raw)]
        else:
            per_core = [0.0] * len(cpu_cores_raw)
        self._prev_cpu_total = cpu_total_raw
        self._prev_cpu_cores = cpu_cores_raw

        cpu_freq = read_cpu_freq()
        mem = read_memory()
        load = read_load_and_uptime()

        net_raw = read_net_counters()
        interfaces = []
        total_rx_bps = total_tx_bps = 0.0
        total_rx_bytes = total_tx_bytes = 0
        for iface, curr in sorted(net_raw.items()):
            prev = self._prev_net.get(iface) if self._prev_net else None
            if prev:
                rx_bps = max(0, curr[0] - prev[0]) / dt
                rx_pps = max(0, curr[1] - prev[1]) / dt
                tx_bps = max(0, curr[3] - prev[3]) / dt
                tx_pps = max(0, curr[4] - prev[4]) / dt
            else:
                rx_bps = tx_bps = rx_pps = tx_pps = 0.0
            state, speed_mbps = get_iface_meta(iface)
            is_virtual = iface == "lo" or iface.startswith(VIRTUAL_NET_PREFIXES)
            if not is_virtual:
                total_rx_bps += rx_bps
                total_tx_bps += tx_bps
                total_rx_bytes += curr[0]
                total_tx_bytes += curr[3]
            interfaces.append({
                "name": iface, "state": state, "speed_mbps": speed_mbps, "is_virtual": is_virtual,
                "rx_bps": round(rx_bps, 1), "tx_bps": round(tx_bps, 1),
                "rx_pps": round(rx_pps, 1), "tx_pps": round(tx_pps, 1),
                "rx_total_bytes": curr[0], "tx_total_bytes": curr[3], "errs": curr[2] + curr[5],
            })
        interfaces.sort(key=lambda x: (x["is_virtual"], x["state"] != "up", -(x["rx_bps"] + x["tx_bps"]), x["name"]))
        self._prev_net = net_raw

        disk_raw = read_disk_counters()
        disks = []
        total_read_bps = total_write_bps = 0.0
        for dev, curr in sorted(disk_raw.items()):
            prev = self._prev_disk.get(dev) if self._prev_disk else None
            if prev:
                r_iops = max(0, curr[0] - prev[0]) / dt
                r_bps = max(0, curr[1] - prev[1]) * 512.0 / dt
                w_iops = max(0, curr[2] - prev[2]) / dt
                w_bps = max(0, curr[3] - prev[3]) * 512.0 / dt
            else:
                r_iops = r_bps = w_iops = w_bps = 0.0
            total_read_bps += r_bps
            total_write_bps += w_bps
            disks.append({
                "name": dev, "read_bps": round(r_bps, 1), "write_bps": round(w_bps, 1),
                "read_iops": round(r_iops, 1), "write_iops": round(w_iops, 1),
            })
        self._prev_disk = disk_raw
        self._prev_ts = now_mono

        gpus, pid_vram = read_nvidia_gpus()
        if gpus:
            gpu_avg_util = round(sum(g["util_gpu"] for g in gpus) / len(gpus), 1)
            vram_used = round(sum(g["mem_used_mb"] for g in gpus), 0)
            vram_total = round(sum(g["mem_total_mb"] for g in gpus), 0)
            vram_pct = round((vram_used / vram_total * 100.0) if vram_total > 0 else 0.0, 1)
            gpu_pwr = round(sum(g["power_w"] for g in gpus), 1)
            gpu_pwr_lim = round(sum(g["power_limit_w"] for g in gpus), 1)
            gpu_max_temp = max(g["temp_c"] for g in gpus)
        else:
            gpu_avg_util = vram_used = vram_total = vram_pct = gpu_pwr = gpu_pwr_lim = 0.0
            gpu_max_temp = 0

        self._tick += 1
        if self._tick == 1 or self._tick % 2 == 0:
            self.top_processes = self._sample_processes(mem["total_bytes"], pid_vram)
        if self._tick == 1 or self._tick % 10 == 0:
            self.mounts = read_disk_mounts()
        temps = read_temperatures()

        snapshot = {
            "timestamp": round(now_wall, 3),
            "host": {**self.static_info, **load},
            "cpu": {**cpu_summary, "freq": cpu_freq, "per_core": per_core},
            "memory": mem,
            "gpu_summary": {
                "count": len(gpus), "avg_util": gpu_avg_util,
                "vram_used_mb": vram_used, "vram_total_mb": vram_total, "vram_percent": vram_pct,
                "total_power_w": gpu_pwr, "total_power_limit_w": gpu_pwr_lim, "max_temp_c": gpu_max_temp,
            },
            "gpus": gpus,
            "network": {
                "rx_bps": round(total_rx_bps, 1), "tx_bps": round(total_tx_bps, 1),
                "rx_total_bytes": total_rx_bytes, "tx_total_bytes": total_tx_bytes, "interfaces": interfaces,
            },
            "disk": {
                "read_bps": round(total_read_bps, 1), "write_bps": round(total_write_bps, 1),
                "devices": disks, "mounts": self.mounts,
            },
            "temperatures": temps,
            "processes": self.top_processes,
        }

        hist_point = {
            "t": round(now_wall, 1),
            "cpu": cpu_summary["total"], "cpu_u": cpu_summary["user"],
            "cpu_s": cpu_summary["system"], "cpu_io": cpu_summary["iowait"],
            "mem": mem["percent"],
            "rx": round(total_rx_bps, 1), "tx": round(total_tx_bps, 1),
            "dr": round(total_read_bps, 1), "dw": round(total_write_bps, 1),
            "gu": gpu_avg_util, "gv": vram_pct, "gp": gpu_pwr,
            "g": [[g["util_gpu"], g["mem_percent"], g["temp_c"], g["power_w"]] for g in gpus],
        }

        with self._lock:
            self.latest = snapshot
            self.history.append(hist_point)

    def _run_loop(self):
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                self._sample_once()
            except Exception as exc:
                sys.stderr.write(f"[linux-monitor] collector error: {exc}\n")
                sys.stderr.flush()
            wait = max(0.2, 1.0 - (time.monotonic() - t0))
            self._stop.wait(wait)

    def get_payload(self, settings, update_status, include_history=True, history_points=90, include_processes=True, initial_tab=None):
        with self._lock:
            snap = dict(self.latest) if self.latest else {}
            hist = list(self.history)[-max(1, min(HISTORY_LEN, int(history_points))):] if include_history else []
        if not include_processes and "processes" in snap:
            snap = {k: v for k, v in snap.items() if k != "processes"}
        payload = {
            "snapshot": snap,
            "history": hist,
            "settings": settings,
            "update": update_status,
        }
        if initial_tab:
            payload["initial_tab"] = initial_tab
        return payload

    def format_text_summary(self):
        with self._lock:
            s = self.latest
        if not s:
            return "Telemetry collector initializing..."
        h, c, m, gs, net, d = s["host"], s["cpu"], s["memory"], s["gpu_summary"], s["network"], s["disk"]

        def fmt_bytes(b):
            for u in ("B", "KiB", "MiB", "GiB", "TiB"):
                if abs(b) < 1024.0:
                    return f"{b:.1f} {u}"
                b /= 1024.0
            return f"{b:.1f} PiB"

        lines = [
            f"Host: {h['hostname']} ({h['os']}) | Load: {h['load_1m']} {h['load_5m']} {h['load_15m']}",
            f"CPU: {c['total']}% (user {c['user']}%, sys {c['system']}%, iowait {c['iowait']}%) | {h['logical_cores']} threads @ {c['freq']['avg_mhz']:.0f} MHz",
            f"Memory: {fmt_bytes(m['used_bytes'])} / {fmt_bytes(m['total_bytes'])} ({m['percent']}%) | Swap: {m['swap_percent']}%",
            f"Network: RX {fmt_bytes(net['rx_bps'])}/s | TX {fmt_bytes(net['tx_bps'])}/s",
            f"Disk I/O: Read {fmt_bytes(d['read_bps'])}/s | Write {fmt_bytes(d['write_bps'])}/s",
        ]
        if gs["count"] > 0:
            lines.append(
                f"GPUs ({gs['count']}x): Avg Util {gs['avg_util']}% | "
                f"VRAM {gs['vram_used_mb']/1024:.1f}/{gs['vram_total_mb']/1024:.1f} GiB ({gs['vram_percent']}%) | "
                f"Power {gs['total_power_w']:.0f}W / {gs['total_power_limit_w']:.0f}W | Max Temp {gs['max_temp_c']}C"
            )
            for g in s["gpus"]:
                lines.append(
                    f"  GPU {g['index']} ({g['name']}): Util {g['util_gpu']}% | "
                    f"VRAM {g['mem_used_mb']:.0f}/{g['mem_total_mb']:.0f} MiB ({g['mem_percent']}%) | "
                    f"{g['temp_c']}C | {g['power_w']:.0f}W"
                )
        return "\n".join(lines)


def build_app_html():
    html = UI_HTML_PATH.read_text(encoding="utf-8")
    sdk = UI_SDK_PATH.read_text(encoding="utf-8").strip()
    m = re.search(r"export\s*\{([^}]+)\}\s*;?\s*$", sdk)
    if not m:
        raise RuntimeError("Could not locate export block in ext-apps.mjs")
    pairs = []
    for item in m.group(1).split(","):
        item = item.strip()
        if not item:
            continue
        if " as " in item:
            local_name, exported_name = [x.strip() for x in item.split(" as ", 1)]
            pairs.append(f"{exported_name}: {local_name}")
        else:
            pairs.append(f"{item}: {item}")
    sdk_inlined = sdk[:m.start()] + "\nconst __MCP_SDK__ = {" + ", ".join(pairs) + "};\n"
    return html.replace("/*__MCP_EXT_APPS_SDK__*/", sdk_inlined, 1)


TOOLS = [
    {
        "name": "open_server_monitor",
        "title": "Server Monitor",
        "description": (
            "Open the real-time Linux server resource monitor dashboard with live charts for "
            "CPU, per-core heatmap, Memory/Swap, NVIDIA GPU utilization/VRAM/temperature/power, "
            "Network RX/TX speed, Disk I/O, and Top processes."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "tab": {
                    "type": "string",
                    "enum": ["overview", "gpu", "cpu", "network", "processes"],
                    "description": "Optional dashboard view tab to focus initially.",
                }
            },
            "additionalProperties": False,
        },
        "_meta": {
            "ui": {"resourceUri": APP_URI},
            "com.google.antigravity/ui": {
                "entrypoints": [
                    {"type": "aux_pane", "title": "Server Monitor"},
                    {"type": "global", "title": "Server Monitor"},
                ]
            },
        },
    },
    {
        "name": "get_server_metrics",
        "title": "Get Live Server Metrics",
        "description": "App-only polling endpoint returning the latest 1Hz telemetry snapshot, rolling history, settings, and update status.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "include_history": {"type": "boolean"},
                "history_points": {"type": "integer"},
                "include_processes": {"type": "boolean"},
            },
            "additionalProperties": False,
        },
        "_meta": {"ui": {"visibility": ["app"]}},
    },
    {
        "name": "set_plugin_settings",
        "title": "Update Plugin Settings",
        "description": "App-only endpoint to persist language preference (zh/en) and optional GitHub repository.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "lang": {"type": "string", "enum": ["", "zh", "en"]},
                "github_repo": {"type": "string"},
            },
            "additionalProperties": False,
        },
        "_meta": {"ui": {"visibility": ["app"]}},
    },
    {
        "name": "check_plugin_update",
        "title": "Check GitHub Plugin Update",
        "description": "App-only endpoint to check GitHub for the latest release or version of this plugin.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "force": {"type": "boolean"},
                "repo": {"type": "string"},
            },
            "additionalProperties": False,
        },
        "_meta": {"ui": {"visibility": ["app"]}},
    },
    {
        "name": "apply_plugin_update",
        "title": "Apply Hot Update From GitHub",
        "description": "App-only endpoint to download, validate, and hot-update the plugin from GitHub.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "force": {"type": "boolean"},
            },
            "additionalProperties": False,
        },
        "_meta": {"ui": {"visibility": ["app"]}},
    },
    {
        "name": "get_server_snapshot",
        "title": "Get Server Resource Snapshot",
        "description": "Return a concise text summary of current Linux CPU, Memory, GPU VRAM/utilization, Network speed, and Disk I/O without opening the UI.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]

RESOURCES = [
    {
        "uri": APP_URI,
        "name": "Linux Server Monitor Dashboard",
        "description": "Interactive real-time Linux server resource visualization dashboard.",
        "mimeType": "text/html;profile=mcp-app",
        "_meta": {"ui": {"prefersBorder": False}},
    }
]


class McpStdioServer:
    def __init__(self, data_dir):
        self.settings = SettingsStore(data_dir)
        self.updater = PluginUpdater(self.settings)
        self.collector = TelemetryCollector()
        self._write_lock = threading.Lock()
        self._restart_args = list(sys.argv)

    def send(self, obj):
        raw = (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        with self._write_lock:
            sys.stdout.buffer.write(raw)
            sys.stdout.buffer.flush()

    def send_result(self, req_id, result):
        self.send({"jsonrpc": "2.0", "id": req_id, "result": result})

    def send_error(self, req_id, code, message):
        self.send({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})

    def _schedule_hot_restart(self):
        def _restart():
            time.sleep(0.35)
            # 1) Try native Antigravity RefreshMcpServers if token is present in env
            csrf = os.environ.get("ANTIGRAVITY_CSRF_TOKEN")
            ls_addr = os.environ.get("ANTIGRAVITY_LS_ADDRESS")
            if csrf and ls_addr:
                try:
                    req = urllib.request.Request(
                        f"http://{ls_addr}/exa.language_server_pb.LanguageServerService/RefreshMcpServers",
                        data=json.dumps({"serverName": "linux-monitor_linux-monitor"}).encode("utf-8"),
                        headers={"Content-Type": "application/json", "x-codeium-csrf-token": csrf},
                        method="POST",
                    )
                    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                    with opener.open(req, timeout=3):
                        return
                except Exception:
                    pass
            # 2) Fallback: seamless in-place execv keeping stdin/stdout pipes alive
            try:
                sys.stdout.flush()
                sys.stderr.flush()
                os.execv(sys.executable, [sys.executable] + self._restart_args)
            except Exception as exc:
                sys.stderr.write(f"[linux-monitor] hot-restart failed: {exc}\n")
                sys.stderr.flush()

        threading.Thread(target=_restart, daemon=True).start()

    def handle_request(self, msg):
        method = msg.get("method")
        req_id = msg.get("id")
        params = msg.get("params") or {}
        if req_id is None:
            return

        try:
            if method == "initialize":
                proto = params.get("protocolVersion") or "2025-03-26"
                self.send_result(req_id, {
                    "protocolVersion": proto,
                    "capabilities": {
                        "tools": {"listChanged": False},
                        "resources": {"subscribe": False, "listChanged": False},
                    },
                    "serverInfo": {"name": SERVER_NAME, "version": get_local_version()},
                })
            elif method == "ping":
                self.send_result(req_id, {})
            elif method == "tools/list":
                self.send_result(req_id, {"tools": TOOLS})
            elif method == "resources/list":
                self.send_result(req_id, {"resources": RESOURCES})
            elif method == "resources/templates/list":
                self.send_result(req_id, {"resourceTemplates": []})
            elif method == "prompts/list":
                self.send_result(req_id, {"prompts": []})
            elif method == "resources/read":
                uri = params.get("uri")
                if uri != APP_URI:
                    self.send_error(req_id, -32002, f"Unknown resource URI: {uri}")
                    return
                html = build_app_html()
                self.send_result(req_id, {
                    "contents": [{
                        "uri": APP_URI,
                        "mimeType": "text/html;profile=mcp-app",
                        "text": html,
                        "_meta": {"ui": {"prefersBorder": False}},
                    }]
                })
            elif method == "tools/call":
                name = params.get("name")
                args = params.get("arguments") or {}
                if name == "open_server_monitor":
                    payload = self.collector.get_payload(
                        settings=self.settings.get(),
                        update_status=self.updater.get_cached_status(),
                        include_history=True,
                        history_points=90,
                        include_processes=True,
                        initial_tab=args.get("tab"),
                    )
                    self.send_result(req_id, {
                        "content": [{"type": "text", "text": self.collector.format_text_summary()}],
                        "structuredContent": payload,
                    })
                elif name == "get_server_metrics":
                    payload = self.collector.get_payload(
                        settings=self.settings.get(),
                        update_status=self.updater.get_cached_status(),
                        include_history=bool(args.get("include_history", True)),
                        history_points=safe_int(args.get("history_points", 90), 90),
                        include_processes=bool(args.get("include_processes", True)),
                    )
                    self.send_result(req_id, {
                        "content": [{"type": "text", "text": "ok"}],
                        "structuredContent": payload,
                    })
                elif name == "set_plugin_settings":
                    updated_settings = self.settings.update(args)
                    self.send_result(req_id, {
                        "content": [{"type": "text", "text": "ok"}],
                        "structuredContent": {
                            "settings": updated_settings,
                            "update": self.updater.get_cached_status(),
                        },
                    })
                elif name == "check_plugin_update":
                    res = self.updater.check_update(
                        force=bool(args.get("force", True)),
                        repo_override=str(args.get("repo") or ""),
                    )
                    self.send_result(req_id, {
                        "content": [{"type": "text", "text": res.get("status", "ok")}],
                        "structuredContent": {"update": res, "settings": self.settings.get()},
                    })
                elif name == "apply_plugin_update":
                    res = self.updater.apply_update(
                        repo_override=str(args.get("repo") or ""),
                        force=bool(args.get("force", False)),
                    )
                    self.send_result(req_id, {
                        "content": [{"type": "text", "text": "updated" if res.get("updated") else res.get("error", "ok")}],
                        "structuredContent": {
                            "result": res,
                            "update": self.updater.get_cached_status(),
                            "settings": self.settings.get(),
                        },
                    })
                    if res.get("ok") and res.get("server_changed"):
                        self._schedule_hot_restart()
                elif name == "get_server_snapshot":
                    self.send_result(req_id, {
                        "content": [{"type": "text", "text": self.collector.format_text_summary()}],
                    })
                else:
                    self.send_error(req_id, -32601, f"Unknown tool: {name}")
            else:
                self.send_error(req_id, -32601, f"Method not found: {method}")
        except Exception as exc:
            sys.stderr.write(f"[linux-monitor] error handling {method}: {exc}\n")
            sys.stderr.flush()
            self.send_error(req_id, -32603, f"Internal error: {exc}")

    def run(self):
        for raw_line in sys.stdin.buffer:
            line = raw_line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line.decode("utf-8"))
            except Exception as exc:
                sys.stderr.write(f"[linux-monitor] invalid json: {exc}\n")
                sys.stderr.flush()
                continue
            self.handle_request(msg)


def main():
    parser = argparse.ArgumentParser(description="Linux Server Monitor MCP App Server")
    parser.add_argument("--data-dir", default=str(PLUGIN_ROOT / ".data"))
    args, _ = parser.parse_known_args()
    McpStdioServer(args.data_dir).run()


if __name__ == "__main__":
    main()
