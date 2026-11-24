---
name: server-monitor
description: Open or query the real-time Linux Server Monitor dashboard (CPU, Memory, GPU VRAM & utilization, Network speed, Disk I/O, Top processes).
---

# Linux Server Monitor

Use the `linux-monitor` MCP server tools to inspect real-time Linux server resources or open the visual dashboard in the Antigravity side pane:

- Call `open_server_monitor` (with optional `tab`: `"overview"`, `"gpu"`, `"cpu"`, `"network"`, or `"processes"`) when the user wants to visually inspect live CPU, memory, GPU VRAM/utilization, network speed, disk I/O, or active processes.
- Call `get_server_snapshot` when you only need a concise text summary of current system load without opening the UI.
