#!/usr/bin/env python3
"""
ListenLook 接收端管线测试 —— 不需要任何硬件，也不需要真手机。

    python3 tests/test_pipeline.py

它把接收端当黑盒起子进程，用真实的 UDP 数据报喂它，然后检查统计输出。
覆盖的都是"静默失败"类的性质 —— 这些 bug 不会崩、不会报错，只会让工具悄悄不工作：

  1. JSON 姿态帧能进来，并且真的转成了鼠标位移
  2. OpenTrack FreePIE 兼容格式（48 字节）能被识别
  3. **掉线后必须停止输出**：否则光标会按最后一帧的姿态一直飘走
  4. 滚轮通道能出刻度
  5. 坏帧被计入 bad，而不是被当成姿态用
"""

from __future__ import annotations

import json
import math
import os
import signal
import socket
import struct
import subprocess
import sys
import threading
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
RECEIVER = os.path.join(ROOT, "receiver", "listenlook.py")
PORT = 18420  # 故意不用默认端口，避免和正在用的实例打架

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"   {detail}" if detail else ""))


class Receiver:
    def __init__(self, *extra: str) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, RECEIVER, "--dry-run", "--no-web", "--quiet",
             "--udp-port", str(PORT), "--stats", "0.4", *extra],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self.lines: list[dict] = []
        # 用后台线程读 stdout，而不是 select：**Windows 上 select 只能用于 socket，
        # 对管道会直接抛错**。读取线程到哪都能跑。
        self._lock = threading.Lock()
        self._drained = 0
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        # 等端口真正开始监听：盲等固定秒数在慢机器上会偶发失败
        deadline = time.time() + 6
        while time.time() < deadline:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.sendto(b"", ("127.0.0.1", PORT))
                s.close()
                break
            except OSError:
                time.sleep(0.05)

    def _read_loop(self) -> None:
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            with self._lock:
                self.lines.append(obj)

    def drain(self) -> list[dict]:
        """取出自上次调用以来新到的统计行。"""
        with self._lock:
            start = self._drained
            self._drained = len(self.lines)
            return self.lines[start:]

    def wait_line(self, timeout: float = 4.0) -> dict | None:
        deadline = time.time() + timeout
        got = self.drain()
        while time.time() < deadline:
            got = self.drain()
            if got:
                return got[-1]
            time.sleep(0.05)
        return self.lines[-1] if self.lines else None

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=3)


def udp_send(payload: bytes) -> None:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.sendto(payload, ("127.0.0.1", PORT))
    s.close()


def json_frame(yaw: float, pitch: float = 0.0, roll: float = 0.0, src: str = "test",
               ok: bool = True) -> bytes:
    return json.dumps({
        "v": 1, "src": src, "ok": ok, "seq": 1, "t": int(time.time() * 1000),
        "yaw": yaw, "pitch": pitch, "roll": roll,
    }).encode()


def stream(frames: int, hz: float, make) -> None:
    interval = 1.0 / hz
    next_t = time.perf_counter()
    for i in range(frames):
        udp_send(make(i))
        next_t += interval
        sleep = next_t - time.perf_counter()
        if sleep > 0:
            time.sleep(sleep)


def test_json_moves_cursor() -> None:
    print("\n[1] JSON 姿态帧 → 鼠标位移")
    r = Receiver()
    try:
        r.wait_line(3)
        before = r.wait_line(2)
        base = (before or {}).get("dry_moved", [0, 0])[0]
        # 先送一帧 0° 建立"正前方"：接收端把收到的第一帧当作零点（和 Mac App 一致），
        # 直接发 18° 的话那 18° 本身就是零点，永远算不出偏角 —— 这是真实使用顺序。
        udp_send(json_frame(0.0))
        time.sleep(0.3)
        stream(int(2.5 * 60), 60, lambda i: json_frame(18.0))
        after = r.wait_line(3)
        moved = (after or {}).get("dry_moved", [0, 0])[0] - base
        check("姿态帧被接收 (src=test)", (after or {}).get("src") == "test",
              f"src={(after or {}).get('src')}")
        check("产生向右的光标位移", moved > 50, f"位移 {moved:.0f}px")
        check("yaw 数值正确入库", abs((after or {}).get("yaw", 0) - 18.0) < 0.01,
              f"yaw={(after or {}).get('yaw')}")
        check("无坏帧", (after or {}).get("bad", 1) == 0)
    finally:
        r.close()


def test_freepie_compat() -> None:
    print("\n[2] OpenTrack FreePIE 兼容（48 字节二进制）")
    r = Receiver()
    try:
        r.wait_line(3)
        payload = struct.pack("<6d", 22.0, -8.0, 3.0, 0.0, 0.0, 0.0)
        stream(90, 60, lambda i: payload)
        line = r.wait_line(3)
        check("识别为 freepie 来源", (line or {}).get("src") == "freepie",
              f"src={(line or {}).get('src')}")
        check("yaw 解析正确", abs((line or {}).get("yaw", 0) - 22.0) < 0.01,
              f"yaw={(line or {}).get('yaw')}")
    finally:
        r.close()


def test_stale_stops_output() -> None:
    print("\n[3] 掉线后必须停止输出（安全属性）")
    r = Receiver()
    try:
        r.wait_line(3)
        udp_send(json_frame(0.0))
        time.sleep(0.3)
        stream(int(1.2 * 60), 60, lambda i: json_frame(25.0))
        during = r.wait_line(3)
        check("掉线前丢出位移", (during or {}).get("dry_moved", [0, 0])[0] > 20,
              f"位移 {(during or {}).get('dry_moved')}")
        time.sleep(1.6)  # 停止发送，超过 STALE_AFTER_S
        frozen = r.wait_line(2)
        check("超过 500ms 无包即判掉线", (frozen or {}).get("fresh") is False,
              f"fresh={(frozen or {}).get('fresh')}")
        a = (frozen or {}).get("dry_moved", [0, 0])
        time.sleep(1.0)
        b = (r.wait_line(2) or {}).get("dry_moved", [0, 0])
        check("掉线期间位移完全冻结", a == b, f"{a} → {b}")
    finally:
        r.close()


def test_wheel() -> None:
    print("\n[4] 滚轮通道")
    r = Receiver("--wheel", "roll")
    try:
        r.wait_line(3)
        # roll 从 -30° 匀速转到 +30°：每 6° 一格，应当出若干格
        stream(120, 60, lambda i: json_frame(0.0, 0.0, -30.0 + i * 0.5))
        line = r.wait_line(3)
        check("滚轮刻度被触发", (line or {}).get("dry_scrolls", 0) != 0,
              f"刻度 {(line or {}).get('dry_scrolls')}")
    finally:
        r.close()


def test_bad_frames() -> None:
    print("\n[5] 坏帧必须被隔离，不能当姿态用")
    r = Receiver()
    try:
        r.wait_line(3)
        udp_send(b"this is not json")
        udp_send(b'{"v":1,"src":"x","ok":true,"yaw":NaN}')
        udp_send(json.dumps({"v": 99, "yaw": 5.0}).encode())   # 版本不符
        udp_send(struct.pack("<6d", math.nan, 0, 0, 0, 0, 0))  # 非有限值
        time.sleep(0.8)
        line = r.wait_line(2)
        check("坏帧被计入 bad", (line or {}).get("bad", 0) >= 3,
              f"bad={(line or {}).get('bad')}")
        check("坏帧没有污染姿态", abs((line or {}).get("yaw", 999)) < 1e-9,
              f"yaw={(line or {}).get('yaw')}")
    finally:
        r.close()


def main() -> int:
    print(f"ListenLook 管线测试（UDP 端口 {PORT}）")
    test_json_moves_cursor()
    test_freepie_compat()
    test_stale_stops_output()
    test_wheel()
    test_bad_frames()
    print(f"\n{'=' * 58}\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        for f in FAIL:
            print(f"  ❌ {f}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
