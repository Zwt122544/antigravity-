# Linux Server Monitor (`linux-monitor`)

An **Antigravity MCP App Plugin** that brings a real-time, zero-dependency Linux server resource visualization dashboard directly into the **Antigravity Left Sidebar** (`global`) and **Right Side Pane** (`aux_pane`).

> 一款适用于 **Antigravity** 的实时 Linux 服务器资源可视化监控插件，支持左侧边栏全屏视图与右侧辅助边栏实时同屏监控，内置中英双语切换与 GitHub 一键热更新。

## Features / 核心特性

- **Zero External Python Dependencies / 零依赖**：Runs on system `/usr/bin/python3` with a 1 Hz background telemetry collector and 120-second ring buffer (instant charts on open, `< 2 ms` RPC response).
- **Overview Dashboard / 总览仪表盘**：Animated radial gauges & sparklines for CPU, Memory, GPU VRAM/Util, and live Network RX/TX speed (`B/s` & `Mbps`), plus 4 synchronized high-DPI Canvas timelines with crosshair hover tooltips.
- **Multi-GPU Matrix / 多卡 GPU 矩阵**：Live fleet utilization curves and per-GPU cards (Compute %, VRAM GiB & %, Temperature, Power Draw/Limit, Fan %, Clocks, PCIe Gen/Width, P-State, and active GPU compute processes).
- **CPU & RAM / CPU 与内存**：User/System/IOWait breakdown, interactive **per-thread CPU heatmap**, stacked memory composition (Used / Cached / Buffers / Swap), and hardware thermal sensors.
- **Network & Disk / 网络与磁盘**：Dual-channel RX/TX oscilloscope, per-interface link speed/PPS/cumulative traffic, block device I/O & IOPS, and storage mount usage bars.
- **Process Monitor / 进程监控**：Real-time searchable process table sortable by CPU, RAM, or GPU VRAM.
- **Bilingual UI / 中英双语切换**：Instant `中 / EN` toggle in the top bar, persisted in Antigravity plugin data.
- **GitHub Auto-Check & Hot Update / 自动检测最新版本与一键热更新**：Automatically detects newer GitHub Releases (or `main` branch updates), validates downloaded archives, atomically updates plugin files while keeping settings intact, and hot-restarts the backend server.

## Installation / 安装方式

```bash
# Option 1: Install via Antigravity CLI
agy plugin install https://github.com/Zwt122544/antigravity-

# Option 2: Clone directly into Antigravity's plugins directory
git clone https://github.com/Zwt122544/antigravity-.git ~/.gemini/config/plugins/linux-monitor
```

No restart needed — open **Server Monitor** from the Antigravity left sidebar or the right side pane `+` menu.

## Publishing New Versions (Triggers Auto-Update) / 发布新版本

1. Bump `"version"` in `plugin.json` (e.g. `"1.2.0"`).
2. Commit, tag, and push:
   ```bash
   git add -A
   git commit -m "Release v1.2.0"
   git tag v1.2.0
   git push origin main --tags
   ```
3. Create a GitHub Release for tag `v1.2.0` (with release notes). Installed plugins will automatically detect `v1.2.0` and show the glowing **`↑ v1.2.0`** hot-update button!
