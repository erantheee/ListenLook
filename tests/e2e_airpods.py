#!/usr/bin/env python3
"""
真机全链路验证：AirPods → 姿态 → 鼠标光标真的动了。

    # 1. 先让采集端跑起来（真 AirPods）
    open -n macapp/Out/airpods.app
    # 2. 再跑这个
    python3 tests/e2e_airpods.py

它做四件事，缺一不可：

  1. 查辅助功能权限 —— macOS 在没有该权限时会**静默丢弃**注入的鼠标事件，
     程序自己完全不知道。不先查，后面所有观察都可能是假象。
  2. 起接收端（真输出模式，不是 --dry-run），同时采样光标位置。
  3. 从接收端的统计数据里确认**姿态帧真的在进来**（否则"光标没动"是因为没数据，
     而不是因为注入失败 —— 这两种失败长得一模一样，必须分开）。
  4. 比对光标轨迹：路径长度说明"动过"，净位移说明"动到哪去了"。

测试期间请**左右转头**：头不动的话，映射输出本来就该是 0，那不是故障。

退出码：0 = 全链路通过，2 = 权限问题，3 = 没收到姿态，4 = 收到姿态但光标没动。
"""

from __future__ import annotations

import ctypes
import importlib.util
import os
import re
import signal
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Windows 上把输出重定向到管道/文件时，Python 用的是**本地代码页**（英文系统 cp1252），
# 打印中文或 █ 这类字符会直接 UnicodeEncodeError 崩掉 —— CI 里就是这么挂的。
# 这不是 CI 特有的问题：Windows 用户只要 `--stats > 日志.txt` 就会踩到。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
sys.path.insert(0, os.path.join(ROOT, "receiver"))


def load_receiver():
    spec = importlib.util.spec_from_file_location(
        "listenlook", os.path.join(ROOT, "receiver", "listenlook.py")
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["listenlook"] = mod
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    ll = load_receiver()
    print("ListenLook 真机全链路验证")
    print("=" * 58)

    # ── 1. 权限
    mouse = ll.make_mouse("auto")
    ok, msg = mouse.permission_ok()
    print(f"鼠标后端：{mouse.name}")
    if not ok:
        print("❌ " + msg)
        return 2
    if "CGWarp" in mouse.name:
        print("⚠️  当前是 CGWarp 降级模式：光标会动，但**游戏读不到**。")
        print("    要验证游戏能读到的那种注入，请先授予「辅助功能」权限并重启本脚本。")

    # ── 2. 起接收端 + 采样光标
    proc = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "receiver", "listenlook.py"),
         "--no-web", "--quiet", "--stats", "1"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    print("接收端已启动，采样 12 秒 —— 请现在开始左右转头 👀")

    samples: list[tuple[float, float]] = []
    stats: list[dict] = []
    import json as _json
    import select

    t0 = time.time()
    last_sample = 0.0
    while time.time() - t0 < 12.0:
        now = time.time()
        if now - last_sample > 0.05:
            p = mouse.position()
            if p:
                samples.append((now - t0, p[0], p[1]))
            last_sample = now
        r, _, _ = select.select([proc.stdout], [], [], 0)
        if r:
            line = proc.stdout.readline()
            try:
                stats.append(_json.loads(line))
            except Exception:
                pass

    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=4)
    except subprocess.TimeoutExpired:
        proc.kill()

    # ── 3. 姿态到底有没有进来
    fresh = [s for s in stats if s.get("fresh") and s.get("ok")]
    print(f"接收端统计行 {len(stats)} 条，其中「在线且有数据」{len(fresh)} 条")
    if fresh:
        last = fresh[-1]
        print(f"  最后一帧：src={last.get('src')} yaw={last.get('yaw'):+.2f} "
              f"pitch={last.get('pitch'):+.2f} roll={last.get('roll'):+.2f} "
              f"速率={last.get('rate_hz')}Hz 坏帧={last.get('bad')}")
    if not fresh:
        print("❌ 没收到有效姿态帧。先确认采集端在跑：open -n macapp/Out/airpods.app")
        return 3

    # ── 4. 光标轨迹
    if len(samples) < 10:
        print("⚠️  采样点太少，无法判断轨迹")
        return 2
    path = 0.0
    for a, b in zip(samples, samples[1:]):
        path += ((b[1] - a[1]) ** 2 + (b[2] - a[2]) ** 2) ** 0.5
    first, lastp = samples[0], samples[-1]
    net = ((lastp[1] - first[1]) ** 2 + (lastp[2] - first[2]) ** 2) ** 0.5
    print(f"\n光标轨迹：采样 {len(samples)} 点，路径总长 {path:.0f}px，净位移 {net:.0f}px")
    print(f"  起点 ({first[1]:.0f},{first[2]:.0f}) → 终点 ({lastp[1]:.0f},{lastp[2]:.0f})")

    if path < 20:
        print("❌ 姿态在进，但光标几乎没动。可能原因：头没动（映射输出本就该是 0）、")
        print("   死区太大、或注入权限不足。先用 --simulate --dry-run 排除映射侧问题。")
        return 4

    print("\n✅ 全链路通过：真 AirPods → 姿态 → 鼠标光标")
    if "CGWarp" in mouse.name:
        print("   （注意：这是降级模式。给「辅助功能」权限后才是游戏能读到的那种注入）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
