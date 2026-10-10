#!/usr/bin/env python3
"""
USB HID gadget 后端的报文编码测试。

    python3 tests/test_hidg.py

这个后端让**电脑那边零软件**：树莓派装成一个 USB 鼠标插到 Windows 上，
游戏看到的是真设备，不走 SendInput，也就没有"合成事件被忽略"的问题。

测试方式：把 `/dev/hidg0` 换成一个普通文件。设备节点本来就是个字符设备，
后端只往里 write 原始 HID 报文 —— 所以用文件就能精确断言字节内容，
不需要树莓派、不需要内核 gadget 模块，在任何平台上都能跑。

重点测的是**拆帧**。HID boot 鼠标报文的 dx/dy 是有符号 int8，单帧上限 ±127。
如果把一个 300px 的位移直接塞进去，它会溢出成 -212px 的反向位移 ——
表现就是"快速转头时光标猛地往反方向飞"，而且只在移动快的时候出现，极难定位。
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

spec = importlib.util.spec_from_file_location(
    "listenlook", os.path.join(ROOT, "receiver", "listenlook.py")
)
ll = importlib.util.module_from_spec(spec)
sys.modules["listenlook"] = ll
spec.loader.exec_module(ll)

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"   {detail}" if detail else ""))


def with_fake_hidg(fn):
    """用临时文件冒充 /dev/hidg0，返回它收到的所有报文。"""
    with tempfile.NamedTemporaryFile(delete=False) as f:
        path = f.name
    try:
        dev = ll.HidgMouse(path)
        fn(dev)
        dev.close()
        with open(path, "rb") as f:
            return f.read()
    finally:
        os.unlink(path)


def test_single_report() -> None:
    print("\n[1] 单帧报文格式（boot 协议 4 字节：按键/dx/dy/滚轮）")
    data = with_fake_hidg(lambda d: d.move_relative(10, -20))
    check("正好 4 字节", len(data) == 4, f"{len(data)} 字节: {data.hex()}")
    check("按键字节为 0", data[0] == 0)
    check("dx 编码正确", data[1] == 10, f"dx={data[1]}")
    check("dy 编码为 int8 补码（-20 → 0xEC）", data[2] == 0xEC, f"dy=0x{data[2]:02X}")


def test_split_large_move() -> None:
    print("\n[2] 超过 ±127 的位移必须拆帧（不拆会溢出成反向，快速转头时光标会倒飞）")
    data = with_fake_hidg(lambda d: d.move_relative(300, -260))
    check("报文长度是 4 的整数倍", len(data) % 4 == 0, f"{len(data)} 字节")
    dxs = [int.from_bytes(data[i + 1:i + 2], "big", signed=True) for i in range(0, len(data), 4)]
    dys = [int.from_bytes(data[i + 2:i + 3], "big", signed=True) for i in range(0, len(data), 4)]
    check("每帧 dx 都在 ±127 内", all(-127 <= v <= 127 for v in dxs), f"{dxs}")
    check("每帧 dy 都在 ±127 内", all(-127 <= v <= 127 for v in dys), f"{dys}")
    check("dx 总和等于 300（没有溢出成负数）", sum(dxs) == 300, f"合计 {sum(dxs)}")
    check("dy 总和等于 -260", sum(dys) == -260, f"合计 {sum(dys)}")


def test_scroll() -> None:
    print("\n[3] 滚轮刻度")
    data = with_fake_hidg(lambda d: d.scroll(3))
    check("滚轮字节 = 3", data[3] == 3, f"wheel={data[3]}")
    data = with_fake_hidg(lambda d: d.scroll(-2))
    check("反向滚轮 = -2（补码）", data[3] == 0xFE, f"wheel=0x{data[3]:02X}")


def test_stats() -> None:
    print("\n[4] 统计（--stats 里要能看到 HID 真的发了多少）")

    def run(dev):
        dev.move_relative(50, 50)
        dev.scroll(1)

    with tempfile.NamedTemporaryFile(delete=False) as f:
        path = f.name
    try:
        dev = ll.HidgMouse(path)
        run(dev)
        st = dev.stats()
        dev.close()
        check("统计里有报文数", st.get("hidg_reports", 0) >= 2, str(st))
        check("统计里有累计位移", st.get("hidg_moved") == [50.0, 50.0], str(st))
    finally:
        os.unlink(path)


def test_zero_move_writes_nothing() -> None:
    print("\n[5] 零位移不该写报文（空转会把设备刷爆）")
    data = with_fake_hidg(lambda d: d.move_relative(0, 0))
    check("没有写出任何字节", len(data) == 0, f"{len(data)} 字节")


def main() -> int:
    print("ListenLook HID gadget 报文测试")
    test_single_report()
    test_split_large_move()
    test_scroll()
    test_stats()
    test_zero_move_writes_nothing()
    print(f"\n{'=' * 58}\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        for f in FAIL:
            print(f"  ❌ {f}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
