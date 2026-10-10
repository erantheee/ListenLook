#!/usr/bin/env python3
"""
解析器与 IMU 融合的单元测试 —— 不需要网络，不需要手机，不需要跑服务。

    python3 tests/test_extract.py

为什么这些测试值得单独存在：接收端最容易坏的地方不是网络，而是**解析**。
现实里发送端五花八门（手机 app、自制硬件、随手写的脚本），字段命名毫无统一可言。
解析一旦错了，表现是"数据收到了但光标不动"或者"动得不对"，
这两种症状都极难从现象反推原因。所以这里把每种输入形状都钉死。
"""

from __future__ import annotations

import importlib.util
import math
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
spec = importlib.util.spec_from_file_location("listenlook", os.path.join(ROOT, "receiver", "listenlook.py"))
ll = importlib.util.module_from_spec(spec)
sys.modules["listenlook"] = ll
spec.loader.exec_module(ll)

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"   {detail}" if detail else ""))


def close(a: float, b: float, tol: float = 0.5) -> bool:
    return abs(a - b) <= tol


def test_explicit_angles() -> None:
    print("\n[1] 显式角度字段")
    pose, how = ll.extract_pose({"yaw": 12.0, "pitch": -5.0, "roll": 3.0}, None, 1 / 60)
    check("直接认出 yaw/pitch/roll", close(pose.yaw, 12) and close(pose.pitch, -5) and close(pose.roll, 3),
          f"{how} → ({pose.yaw}, {pose.pitch}, {pose.roll})")
    check("数值有限", pose.finite())


def test_alpha_beta_gamma() -> None:
    print("\n[2] 手机那套 alpha/beta/gamma（含符号翻转）")
    pose, how = ll.extract_pose({"alpha": 30.0, "beta": 10.0, "gamma": 4.0}, None, 1 / 60)
    # alpha 的正方向与我们的 yaw 相反，解析器必须自己翻过来
    check("alpha 被映射成 -yaw", close(pose.yaw, -30), f"yaw={pose.yaw}")
    check("beta → pitch", close(pose.pitch, 10), f"pitch={pose.pitch}")
    check("gamma → roll", close(pose.roll, 4), f"roll={pose.roll}")


def test_quaternion() -> None:
    print("\n[3] 四元数")
    pose, how = ll.extract_pose({"qw": 1.0, "qx": 0.0, "qy": 0.0, "qz": 0.0}, None, 1 / 60)
    check("单位四元数 = 零姿态", close(pose.yaw, 0) and close(pose.pitch, 0) and close(pose.roll, 0), how)
    s = math.sin(math.radians(45))
    c = math.cos(math.radians(45))
    pose, how = ll.extract_pose({"qw": c, "qx": 0.0, "qy": 0.0, "qz": s}, None, 1 / 60)
    check("绕 Z 轴 90° → yaw=90", close(pose.yaw, 90, 1.0), f"yaw={pose.yaw:.2f}")


def test_raw_imu_fusion() -> None:
    print("\n[4] 原始 IMU → 互补滤波")
    fuser = ll.ImuFuser()
    # 摆平放正：重力全在 +Z，绕 Z 轴以 30°/s 转 2 秒
    dt = 1 / 60
    yaw = pitch = roll = 0.0
    for _ in range(120):
        yaw, pitch, roll = fuser.update((0.0, 0.0, 9.81), (0.0, 0.0, 30.0), dt)
    check("yaw 积分正确（30°/s × 2s = 60°）", close(yaw, 60, 2.0), f"yaw={yaw:.2f}")
    check("水平放置时 pitch 不漂", close(pitch, 0, 1.5), f"pitch={pitch:.2f}")
    check("水平放置时 roll 不漂", close(roll, 0, 1.5), f"roll={roll:.2f}")

    # 侧倾 30°：ay = g·sin30, az = g·cos30
    f2 = ll.ImuFuser()
    _, _, roll2 = f2.update((0.0, 9.81 * math.sin(math.radians(30)), 9.81 * math.cos(math.radians(30))),
                            (0.0, 0.0, 0.0), dt)
    check("由重力方向解出 30° 侧倾", close(roll2, 30, 1.0), f"roll={roll2:.2f}")


def test_sensor_logger_shape() -> None:
    print("\n[5] Sensor Logger / phyphox 那种嵌套结构")
    payload = {
        "messageId": 7,
        "deviceId": "iPhone-de-Eran",
        "payload": [
            {"name": "accelerometer", "time": 1.0, "values": {"x": 0.0, "y": 0.0, "z": 9.81}},
            {"name": "gyroscope", "time": 1.0, "values": {"x": 0.0, "y": 0.0, "z": 45.0}},
        ],
    }
    fuser = ll.ImuFuser()
    yaw = 0.0
    for _ in range(60):
        pose, how = ll.extract_pose(payload, fuser, 1 / 60)
        yaw = pose.yaw
    check("从嵌套结构里找到 accel+gyro", "互补滤波" in how, how)
    check("融合出 yaw（45°/s × 1s ≈ 45°）", close(yaw, 45, 2.0), f"yaw={yaw:.2f}")
    check("来源标识取自 deviceId", pose.src == "iPhone-de-Eran", f"src={pose.src}")


def test_orientation_only() -> None:
    print("\n[6] 只有加速度计 / 无法解析的输入")
    pose, how = ll.extract_pose({"accel": {"x": 0.0, "y": 9.81, "z": 0.0}}, ll.ImuFuser(), 1 / 60)
    check("只有加速度计时仍给出倾角", close(pose.roll, 90, 1.0), f"roll={pose.roll:.2f} ({how})")
    check("此时 yaw 必须为 0（测不出来，不能瞎编）", close(pose.yaw, 0), f"yaw={pose.yaw}")

    pose, how = ll.extract_pose({"hello": "world", "n": 3}, ll.ImuFuser(), 1 / 60)
    check("无关 JSON 被判定为不可用", pose.ok is False, how)


def test_nonfinite_guard() -> None:
    print("\n[7] 非有限值防线")
    pose, how = ll.extract_pose({"yaw": float("nan"), "pitch": 1.0, "roll": 2.0}, None, 1 / 60)
    check("NaN 不得进入姿态", pose.finite() is False or not math.isnan(pose.pitch), how)


def main() -> int:
    print("ListenLook 解析器 / IMU 融合测试")
    test_explicit_angles()
    test_alpha_beta_gamma()
    test_quaternion()
    test_raw_imu_fusion()
    test_sensor_logger_shape()
    test_orientation_only()
    test_nonfinite_guard()
    print(f"\n{'=' * 58}\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        for f in FAIL:
            print(f"  ❌ {f}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
