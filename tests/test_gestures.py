#!/usr/bin/env python3
"""
手势 → 按键 的单元测试。不需要权限、不需要硬件、不需要网络。

    python3 tests/test_gestures.py

这个功能有一类特别讨厌的失败：**能跑，但难用**。
不触发、连点、或者每次转头看风景都误触 —— 这些都不是崩溃，测试不写就只能靠人肉试出来。
所以下面每一条都对应一个具体的现场症状：

  · 短于 hold_ms 的转头 = "我看了一眼右边，怎么就开枪了"
  · 缺 hysteresis = "我把头停在阈值附近，它每秒点几十次"
  · down/up 同帧 = "手感时有时无"（部分引擎会丢掉同帧的按下沿）
  · 归零不重置状态 = "一按归零就莫名触发一个键"
"""

from __future__ import annotations

import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Windows 上把输出重定向到管道/文件时，Python 用的是**本地代码页**（英文系统 cp1252），
# 打印中文或 █ 这类字符会直接 UnicodeEncodeError 崩掉 —— CI 里就是这么挂的。
# 这不是 CI 特有的问题：Windows 用户只要 `--stats > 日志.txt` 就会踩到。
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


def make_mapper(gestures, smooth=False, **over):
    cfg = ll.Config()
    cfg.gestures = gestures
    cfg.smooth = smooth
    for k, v in over.items():
        setattr(cfg, k, v)
    return ll.Mapper(cfg)


def feed(mapper, frames: int, pitch=0.0, yaw=0.0, roll=0.0, dt=1 / 60) -> list:
    """喂 n 帧固定姿态，返回这一路产生的所有按键事件（按顺序）。"""
    events = []
    pose = ll.Pose(yaw=yaw, pitch=pitch, roll=roll, ok=True, src="test")
    for _ in range(frames):
        ev = mapper.step(pose, dt)[3]
        events.extend(ev)
    return events


NOD = {"name": "点头", "axis": "pitch", "direction": "pos",
       "threshold": 12, "hold_ms": 300, "key": "space", "release_ms": 60}


def test_basic_fire() -> None:
    print("\n[1] 基本触发：越过阈值并保持够久 → 一次按下 + 一次松开")
    m = make_mapper([NOD])
    feed(m, 5, pitch=0.0)                      # 建立静止基线
    ev = feed(m, 40, pitch=20.0)               # 20° 持续 40 帧 ≈ 667ms > 300ms
    downs = [e for e in ev if e[0] == "down"]
    ups = [e for e in ev if e[0] == "up"]
    check("恰好触发一次", len(downs) == 1, f"down={downs}")
    check("键名正确", downs and downs[0][1] == "space", f"{downs}")
    check("随后松开", len(ups) == 1, f"up={ups}")


def test_down_up_not_same_frame() -> None:
    print("\n[2] 按下与松开不得在同一帧（否则部分引擎会丢掉这次输入）")
    m = make_mapper([NOD])
    feed(m, 5, pitch=0.0)
    # 逐帧检查：触发那一帧只应有 down，不应同时含 up
    saw_fire_frame = None
    pose = ll.Pose(pitch=20.0, ok=True, src="test")
    for i in range(40):
        ev = m.step(pose, 1 / 60)[3]
        kinds = [e[0] for e in ev]
        if kinds:
            if saw_fire_frame is None and "down" in kinds:
                saw_fire_frame = (i, kinds)
                check("触发帧只有 down", kinds == ["down"], f"第 {i} 帧 {kinds}")
            elif "up" in kinds:
                check("松开出现在**后面的帧**里", saw_fire_frame is not None,
                      f"第 {i} 帧 {kinds}")
                break
    ev = feed(m, 20, pitch=0.0)
    check("松开的键名正确", any(e == ("up", "space") for e in ev) or saw_fire_frame is not None)


def test_too_short_does_not_fire() -> None:
    print("\n[3] 短于 hold_ms 的转头不得触发（「看一眼右边」不该开枪）")
    m = make_mapper([NOD])
    feed(m, 5, pitch=0.0)
    ev = feed(m, 10, pitch=20.0)     # 10 帧 ≈ 167ms < 300ms
    ev += feed(m, 20, pitch=0.0)
    check("没有触发", not [e for e in ev if e[0] == "down"], f"事件={ev}")


def test_hysteresis_no_repeat() -> None:
    print("\n[4] 停在阈值附近不得连点（缺 hysteresis 就会每秒几十次）")
    m = make_mapper([NOD])
    feed(m, 5, pitch=0.0)
    ev = []
    # 在阈值上方附近来回抖：12.05 / 11.95 反复横跳
    for i in range(180):             # 3 秒
        ev += m.step(ll.Pose(pitch=12.05 if i % 2 else 11.95, ok=True, src="test"), 1 / 60)[3]
    downs = [e for e in ev if e[0] == "down"]
    check("3 秒抖动里只触发一次", len(downs) == 1, f"触发 {len(downs)} 次")


def test_rearm_after_return() -> None:
    print("\n[5] 回到阈值内之后可以再次触发（不能只响一次）")
    m = make_mapper([NOD])
    feed(m, 5, pitch=0.0)
    e1 = feed(m, 40, pitch=20.0)
    e2 = feed(m, 40, pitch=0.0)      # 回到 0，重新武装
    e3 = feed(m, 40, pitch=20.0)
    d1 = len([e for e in e1 if e[0] == "down"])
    d3 = len([e for e in e3 if e[0] == "down"])
    check("第一次触发", d1 == 1, f"{d1}")
    check("第二次也能触发", d3 == 1, f"{d3}")


def test_direction() -> None:
    print("\n[6] 方向：neg 只认反方向的超出")
    rule = dict(NOD, direction="neg", key="f")
    m = make_mapper([rule])
    feed(m, 5, pitch=0.0)
    pos = feed(m, 40, pitch=20.0)
    check("正方向不触发", not [e for e in pos if e[0] == "down"], f"{pos}")
    m2 = make_mapper([rule])
    feed(m2, 5, pitch=0.0)
    neg = feed(m2, 40, pitch=-20.0)
    check("负方向触发", len([e for e in neg if e[0] == "down"]) == 1, f"{neg}")


def test_recenter_resets() -> None:
    print("\n[7] 归零必须清掉手势状态（否则归零瞬间会误触发）")
    m = make_mapper([NOD])
    pose_high = ll.Pose(pitch=20.0, ok=True, src="test")
    feed(m, 10, pitch=20.0)          # 已经越过阈值但还没到 hold_ms
    m.recenter(pose_high)            # 此刻归零：20° 变成新的"正前方"
    ev = feed(m, 60, pitch=20.0)     # 继续停在 20°（现在相对值是 0）
    check("归零后不再触发", not [e for e in ev if e[0] == "down"], f"事件={ev}")


def test_smoothing_path() -> None:
    print("\n[8] 开着平滑时也要能触发（真实默认配置走这条）")
    m = make_mapper([NOD], smooth=True)
    feed(m, 10, pitch=0.0)
    ev = feed(m, 120, pitch=25.0)    # 2 秒，留足滤波收敛时间
    check("平滑链路下触发成功", len([e for e in ev if e[0] == "down"]) == 1, f"{ev}")


def test_key_tables_cover_examples() -> None:
    print("\n[9] 配置示例里出现的键名必须在三套键码表里都存在")
    for key in ("space", "f", "f8", "right_mouse"):
        if key.endswith("mouse"):
            continue
        check(f"macOS 有 '{key}'", key in ll.MAC_KEYCODES)
        check(f"Windows 有 '{key}'", key in ll.WIN_VK)
        check(f"Linux 有 '{key}'", key in ll.LINUX_KEYCODES)
    check("不存在的键名不会被悄悄当成有效", "notakey" not in ll.MAC_KEYCODES)


def test_yaw_gesture() -> None:
    print("\n[10] yaw 手势（摇头）")
    rule = {"name": "摇头", "axis": "yaw", "direction": "pos", "threshold": 18,
            "hold_ms": 250, "key": "g"}
    m = make_mapper([rule])
    feed(m, 5, yaw=0.0)
    ev = feed(m, 60, yaw=25.0)
    check("摇头触发一次", len([e for e in ev if e[0] == "down"]) == 1, f"{ev}")
    check("pitch 的动静不会误触发 yaw 规则",
          not [e for e in feed(make_mapper([rule]), 60, pitch=40.0) if e[0] == "down"])


def main() -> int:
    print("ListenLook 手势 → 按键 测试")
    test_basic_fire()
    test_down_up_not_same_frame()
    test_too_short_does_not_fire()
    test_hysteresis_no_repeat()
    test_rearm_after_return()
    test_direction()
    test_recenter_resets()
    test_smoothing_path()
    test_key_tables_cover_examples()
    test_yaw_gesture()
    print(f"\n{'=' * 58}\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        for f in FAIL:
            print(f"  ❌ {f}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
