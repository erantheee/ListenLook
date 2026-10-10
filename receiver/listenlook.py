#!/usr/bin/env python3
"""
ListenLook —— 把头部姿态变成鼠标。

一个文件，零第三方依赖（只用 Python 标准库），Windows / macOS / Linux 都能跑。
它做三件事：

  1. 从任意来源收头部姿态（UDP，ListenLook Pose v1 或 OpenTrack FreePIE 格式）
  2. 把姿态按你配的规则映射成鼠标移动 / 滚轮
  3. 用各平台的原生 API 把移动注入系统

它**不**负责采集 AirPods 的姿态 —— 那是 macapp/ 里那个小程序的活。
这个分工不是洁癖，是必须的：AirPods 的姿态数据在 Windows 上取不到（协议层就走不通），
所以「取数据」和「用数据」必须解耦，Windows 玩家才能靠手机或树莓派当传感器用上它。

    # 没有硬件时先看它怎么动（不碰你的鼠标）
    python3 listenlook.py --simulate --dry-run

    # 用手机当传感器：起服务，手机浏览器打开提示的 https 地址
    python3 listenlook.py --bind 0.0.0.0

    # 已验证的最短路径：看当前配置
    python3 listenlook.py --print-config

⚠️ 安全边界：协议无加密无认证，任何能往 UDP 端口发包的人都能控制你的鼠标。
   默认只监听回环地址，跨机使用要显式 --bind 0.0.0.0，且只在可信局域网里用。

⚠️ 权限：Windows 一般不需要额外授权；macOS 需要在
   「系统设置 > 隐私与安全性 > 辅助功能」里给运行本程序的终端（或 Python）打勾，
   否则鼠标注入会被系统静默丢弃 —— 程序会检测并提示你。
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import json
import math
import os
import platform
import signal
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field, asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

VERSION = "0.1.0"
PROTO_VERSION = 1
DEFAULT_UDP_PORT = 8420
DEFAULT_WEB_PORT = 8443   # 手机浏览器页面（HTTPS/WSS，浏览器读陀螺仪必须是安全上下文）
DEFAULT_HTTP_PORT = 8421  # 纯 HTTP 摄取端点（给 app / 脚本用，不需要证书）
STALE_AFTER_S = 0.5  # 超过这个时间没有数据报 = 掉线

SYSTEM = platform.system()  # 'Darwin' | 'Windows' | 'Linux'

# ─────────────────────────────────────────────────────────────────────────────
# 配置
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Config:
    # 网络
    bind: str = "127.0.0.1"
    udp_port: int = DEFAULT_UDP_PORT
    web_port: int = DEFAULT_WEB_PORT
    http_port: int = DEFAULT_HTTP_PORT
    imu_fusion: bool = True  # 只收到原始加速度/陀螺仪时，用互补滤波算姿态
    fmt: str = "auto"  # auto | listenlook | freepie

    # 映射
    mode: str = "relative"  # relative(角度偏移→速度) | rate(角速度→速度) | absolute(角度→屏幕位置)
    rate_hz: int = 100  # 映射循环频率
    deadzone_deg: float = 2.0  # 中心死区，防止静止时抖动
    gain: float = 8.0  # 速度增益：px/s 每 (度 或 度/秒 或 度)^curve
    curve: float = 1.5  # 响应曲线指数，>1 = 中心附近更精细
    max_speed_px_s: float = 1500.0  # 速度上限，防止光标瞬间飞到屏幕角落
    max_step_px: float = 60.0  # 单帧位移上限
    absolute_range_deg: float = 25.0  # absolute 模式下，多少度对应半屏

    invert_yaw: bool = False
    invert_pitch: bool = False
    invert_roll: bool = False
    swap_xy: bool = False  # 把 yaw/pitch 对调（有些人横着用）

    # 平滑（One Euro：静止时强平滑、快速转动时几乎不滞后）
    smooth: bool = True
    min_cutoff: float = 1.2
    beta: float = 0.05
    d_cutoff: float = 1.0

    # 滚轮：歪头滚动
    wheel_axis: str = "none"  # none | roll | yaw | pitch
    wheel_deg_per_notch: float = 6.0
    invert_wheel: bool = False

    # 激活方式
    activation: str = "always"  # always | hold
    hold_key: str = "f8"  # 见 KEYCODES；也接受 right_mouse / left_mouse

    # 其它
    output: str = "auto"  # auto | dry
    panel: bool = True
    stats: float = 0.0  # >0 时：每 N 秒往 stdout 打一行 JSON 统计（给脚本/CI 用）
    auto_recenter_idle_s: float = 0.0  # >0 时：静止这么久就把当前朝向当新零点


DEFAULTS = Config()


def load_config(path: Optional[str]) -> Config:
    cfg = Config()
    if not path:
        return cfg
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    for k, v in raw.items():
        if not hasattr(cfg, k):
            print(f"警告：配置里有未知字段 '{k}'，已忽略", file=sys.stderr)
            continue
        setattr(cfg, k, v)
    return cfg


# ─────────────────────────────────────────────────────────────────────────────
# 姿态状态
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Pose:
    yaw: float = 0.0
    pitch: float = 0.0
    roll: float = 0.0
    ok: bool = False
    src: str = "-"
    seq: int = 0
    t: float = 0.0  # 发送端时间戳（毫秒）
    recv_t: float = 0.0  # 本机收到的时间（perf_counter）

    def finite(self) -> bool:
        return all(
            isinstance(v, (int, float)) and math.isfinite(v)
            for v in (self.yaw, self.pitch, self.roll)
        )


class PoseState:
    """线程安全的"最新姿态"。多个来源（UDP / 手机 / 模拟）都往这里写。

    刻意不做队列：姿态是「最新值有意义」的流，缓冲旧帧只会制造延迟。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pose = Pose()
        self._frames = 0
        self._bad = 0

    def update(self, pose: Pose) -> None:
        pose.recv_t = time.perf_counter()
        with self._lock:
            self._pose = pose
            self._frames += 1

    def bump_bad(self) -> None:
        with self._lock:
            self._bad += 1

    def get(self) -> tuple[Pose, bool, int]:
        with self._lock:
            p = self._pose
            frames, bad = self._frames, self._bad
        fresh = (time.perf_counter() - p.recv_t) < STALE_AFTER_S and p.recv_t > 0
        return p, fresh, frames

    def stats(self) -> tuple[int, int]:
        with self._lock:
            return self._frames, self._bad


# ─────────────────────────────────────────────────────────────────────────────
# 平滑：One Euro Filter
#     (Casiez, Roussel, Vogel 2012)
#
# 为什么不用简单的滑动平均：头戴传感器有个根本矛盾 —— 静止时需要强平滑来压抖动
# （不然光标自己会飘，而且游戏里会看到准星在呼吸），快速转动时需要弱平滑来避免拖影。
# 固定系数的滤波器只能二选一。One Euro 用「角速度」动态调截止频率，两头都占。
# ─────────────────────────────────────────────────────────────────────────────


class LowPass:
    def __init__(self) -> None:
        self.y: Optional[float] = None

    def __call__(self, x: float, alpha: float) -> float:
        self.y = x if self.y is None else alpha * x + (1.0 - alpha) * self.y
        return self.y


class OneEuro:
    def __init__(self, min_cutoff: float, beta: float, d_cutoff: float) -> None:
        self.min_cutoff = min_cutoff
        self.beta = beta
        self.d_cutoff = d_cutoff
        self._x = LowPass()
        self._dx = LowPass()
        self._prev: Optional[float] = None
        self._prev_t: Optional[float] = None

    @staticmethod
    def _alpha(cutoff: float, dt: float) -> float:
        tau = 1.0 / (2.0 * math.pi * max(cutoff, 1e-6))
        return 1.0 / (1.0 + tau / max(dt, 1e-6))

    def __call__(self, x: float, dt: float) -> float:
        if self._prev is not None and self._prev_t is not None:
            dx = (x - self._prev) / max(dt, 1e-6)
        else:
            dx = 0.0
        dx_hat = self._dx(dx, self._alpha(self.d_cutoff, dt))
        cutoff = self.min_cutoff + self.beta * abs(dx_hat)
        y = self._x(x, self._alpha(cutoff, dt))
        self._prev, self._prev_t = x, (self._prev_t or 0.0) + dt
        return y

    def reset(self) -> None:
        self._x = LowPass()
        self._dx = LowPass()
        self._prev = None
        self._prev_t = None


# ─────────────────────────────────────────────────────────────────────────────
# 鼠标注入：每个平台一个后端
# ─────────────────────────────────────────────────────────────────────────────


# ctypes 结构体定义在模块级，不嵌套进类体：嵌套结构体之间无法互相引用
# （类体执行时类对象还不存在），`("origin", CGPoint)` 会直接 NameError。
class CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class CGSize(ctypes.Structure):
    _fields_ = [("width", ctypes.c_double), ("height", ctypes.c_double)]


class CGRect(ctypes.Structure):
    _fields_ = [("origin", CGPoint), ("size", CGSize)]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _INPUTunion(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT)]


class _WININPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("u", _INPUTunion)]


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class MouseBackend:
    name = "none"
    can_read_position = False

    def move_relative(self, dx: float, dy: float) -> None:
        raise NotImplementedError

    def move_absolute(self, x: float, y: float) -> None:
        raise NotImplementedError

    def scroll(self, notches: int) -> None:
        pass

    def position(self) -> Optional[tuple[float, float]]:
        return None

    def screen_union(self) -> Optional[tuple[float, float, float, float]]:
        """所有显示器合并后的边界 (x, y, w, h)。"""
        return None

    def permission_ok(self) -> tuple[bool, str]:
        return True, ""

    def close(self) -> None:
        pass


class DarwinMouse(MouseBackend):
    """macOS：CoreGraphics 事件。

    注意一个平台差异：macOS 没有「相对移动」的事件原语（CGEvent 是绝对坐标模型），
    所以要自己读当前位置再算目标位置。这不是绕路，是 macOS 的输入模型本来如此。
    """

    name = "macos/CGEvent"
    can_read_position = True
    FRAMEWORK = "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
    kCGHIDEventTap = 0
    kCGEventMouseMoved = 5
    kCGEventLeftMouseDragged = 6
    kCGScrollEventUnitLine = 1
    kCGEventSourceStateHIDSystemState = 1

    def __init__(self) -> None:
        self.cg = ctypes.CDLL(self.FRAMEWORK)
        self.cg.CGEventCreate.restype = ctypes.c_void_p
        self.cg.CGEventCreate.argtypes = [ctypes.c_void_p]
        self.cg.CGEventGetLocation.restype = CGPoint
        self.cg.CGEventGetLocation.argtypes = [ctypes.c_void_p]
        self.cg.CGEventCreateMouseEvent.restype = ctypes.c_void_p
        self.cg.CGEventCreateMouseEvent.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            CGPoint,
            ctypes.c_uint32,
        ]
        self.cg.CGEventCreateScrollWheelEvent.restype = ctypes.c_void_p
        self.cg.CGEventCreateScrollWheelEvent.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_int32,
        ]
        self.cg.CGEventPost.restype = None
        self.cg.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
        self.cg.CFRelease.restype = None
        self.cg.CFRelease.argtypes = [ctypes.c_void_p]
        self.cg.CGMainDisplayID.restype = ctypes.c_uint32
        self.cg.CGGetActiveDisplayList.restype = ctypes.c_int32
        self.cg.CGGetActiveDisplayList.argtypes = [
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_uint32),
        ]
        self.cg.CGDisplayBounds.restype = CGRect
        self.cg.CGDisplayBounds.argtypes = [ctypes.c_uint32]
        self.cg.CGEventSourceKeyState.restype = ctypes.c_bool
        self.cg.CGEventSourceKeyState.argtypes = [ctypes.c_uint32, ctypes.c_ushort]
        self.cg.CGEventSourceButtonState.restype = ctypes.c_bool
        self.cg.CGEventSourceButtonState.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
        self.cg.CGWarpMouseCursorPosition.restype = ctypes.c_int32
        self.cg.CGWarpMouseCursorPosition.argtypes = [CGPoint]
        self.cg.AXIsProcessTrusted.restype = ctypes.c_bool
        self._bounds: Optional[tuple[float, float, float, float]] = None
        self._bounds_t = 0.0

        # 实测结论（本机 macOS 26.7）：
        #   有辅助功能权限 → CGEventPost 能产生**真正的鼠标移动事件**，游戏看得到。
        #   没有权限     → CGEventPost 被系统静默丢弃，但 CGWarpMouseCursorPosition
        #                  仍然可用（实测位移精确到 0.8px）。warp 只挪光标位置，
        #                  **不产生移动事件**，所以游戏读不到。
        # 也就是说：warp 是"没授权时至少能用"的降级档，不是替代品。
        # 想做这个工具的正事（玩游戏），辅助功能权限没有捷径。
        self.trusted = bool(self.cg.AXIsProcessTrusted())
        self.name = "macos/CGEvent" if self.trusted else "macos/CGWarp（降级：游戏读不到）"

    def permission_ok(self) -> tuple[bool, str]:
        if self.trusted:
            return True, ""
        return True, (
            "⚠️  未授予「辅助功能」权限，已自动降级为 CGWarp 模式：\n"
            "    光标会动，但**不产生鼠标移动事件** —— 游戏里不会有反应。\n"
            "    授权：系统设置 > 隐私与安全性 > 辅助功能，把运行本程序的 App\n"
            "    （终端 / iTerm / 本工具所在的应用）打勾，然后**重启本程序**。\n"
            "    macOS 不会给已经在运行的进程补发权限，这一步必须重启。"
        )

    def position(self) -> Optional[tuple[float, float]]:
        ev = self.cg.CGEventCreate(None)
        if not ev:
            return None
        try:
            p = self.cg.CGEventGetLocation(ev)
            return (p.x, p.y)
        finally:
            self.cg.CFRelease(ev)

    def screen_union(self) -> Optional[tuple[float, float, float, float]]:
        # 缓存 2 秒：显示器热插拔很罕见，但每帧都问系统是浪费
        now = time.perf_counter()
        if self._bounds and now - self._bounds_t < 2.0:
            return self._bounds
        count = ctypes.c_uint32(0)
        self.cg.CGGetActiveDisplayList(0, None, ctypes.byref(count))
        if count.value == 0:
            return None
        arr = (ctypes.c_uint32 * count.value)()
        self.cg.CGGetActiveDisplayList(count.value, arr, ctypes.byref(count))
        xs, ys, x2s, y2s = [], [], [], []
        for i in range(count.value):
            r = self.cg.CGDisplayBounds(arr[i])
            xs.append(r.origin.x)
            ys.append(r.origin.y)
            x2s.append(r.origin.x + r.size.width)
            y2s.append(r.origin.y + r.size.height)
        self._bounds = (min(xs), min(ys), max(x2s), max(y2s))
        self._bounds_t = now
        return self._bounds

    def _post(self, ev) -> None:
        if ev:
            self.cg.CGEventPost(self.kCGHIDEventTap, ev)
            self.cg.CFRelease(ev)

    def move_absolute(self, x: float, y: float) -> None:
        x, y = self._clamp(x, y)
        if self.trusted:
            ev = self.cg.CGEventCreateMouseEvent(
                None, self.kCGEventMouseMoved, CGPoint(x, y), 0
            )
            self._post(ev)
        else:
            # 降级模式也必须走 warp，否则"复位光标"这种绝对定位会被静默丢弃 ——
            # 实测踩过：自检里位移生效了，复位却没生效，光标被留在偏移位置。
            self.cg.CGWarpMouseCursorPosition(CGPoint(x, y))

    def _clamp(self, x: float, y: float) -> tuple[float, float]:
        b = self.screen_union()
        if not b:
            return x, y
        # 钳制到所有显示器的并集。多屏时这个并集是矩形，可能包含显示器之间的空隙，
        # 也就是光标有机会「跑到缝里」—— 已知的小瑕疵，不值得为它引入窗口系统依赖。
        return min(max(x, b[0]), b[2] - 1), min(max(y, b[1]), b[3] - 1)

    def move_relative(self, dx: float, dy: float) -> None:
        cur = self.position()
        if cur is None:
            return
        x, y = self._clamp(cur[0] + dx, cur[1] + dy)
        if self.trusted:
            self.move_absolute(x, y)
        else:
            # CGWarp 不需要任何权限，代价是它绕过事件系统直接改光标位置。
            self.cg.CGWarpMouseCursorPosition(CGPoint(x, y))

    def scroll(self, notches: int) -> None:
        if notches == 0:
            return
        ev = self.cg.CGEventCreateScrollWheelEvent(None, self.kCGScrollEventUnitLine, 1, notches)
        self._post(ev)

    def key_down(self, key: str) -> bool:
        code = MAC_KEYCODES.get(key)
        if code is None:
            return False
        return bool(self.cg.CGEventSourceKeyState(self.kCGEventSourceStateHIDSystemState, code))

    def button_down(self, button: str) -> bool:
        btn = {"left_mouse": 0, "right_mouse": 1, "middle_mouse": 2}.get(button)
        if btn is None:
            return False
        return bool(self.cg.CGEventSourceButtonState(self.kCGEventSourceStateHIDSystemState, btn))


class WindowsMouse(MouseBackend):
    """Windows：SendInput。相对移动是原生支持的（MOUSEEVENTF_MOVE 就是增量）。"""

    name = "windows/SendInput"
    can_read_position = True
    MOUSEEVENTF_MOVE = 0x0001
    MOUSEEVENTF_WHEEL = 0x0800
    INPUT_MOUSE = 0
    ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong

    def __init__(self) -> None:
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.user32.SendInput.argtypes = [
            ctypes.c_uint,
            ctypes.POINTER(_WININPUT),
            ctypes.c_int,
        ]
        self.user32.SendInput.restype = ctypes.c_uint
        self.user32.GetCursorPos.argtypes = [ctypes.POINTER(_POINT)]
        self.user32.GetCursorPos.restype = ctypes.c_bool
        self.user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
        self.user32.GetAsyncKeyState.restype = ctypes.c_short
        self.user32.SetProcessDPIAware()
        self._failed = 0

    def _send(self, flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> None:
        inp = _WININPUT(
            type=self.INPUT_MOUSE,
            u=_INPUTunion(
                mi=_MOUSEINPUT(dx, dy, ctypes.c_ulong(data).value, flags, 0, None)
            ),
        )
        if self.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp)) != 1:
            self._failed += 1

    def move_relative(self, dx: float, dy: float) -> None:
        # SendInput 只吃整数，小数部分由上层累积器处理（见 Mapper）
        self._send(self.MOUSEEVENTF_MOVE, int(dx), int(dy))

    def move_absolute(self, x: float, y: float) -> None:
        # 绝对模式用 0..65535 归一化坐标
        vx = int(max(0.0, min(1.0, x)) * 65535)
        vy = int(max(0.0, min(1.0, y)) * 65535)
        self._send(self.MOUSEEVENTF_MOVE | 0x8000 | 0x4000, vx, vy)

    def scroll(self, notches: int) -> None:
        if notches:
            self._send(self.MOUSEEVENTF_WHEEL, 0, 0, notches * 120)

    def position(self) -> Optional[tuple[float, float]]:
        p = _POINT()
        if self.user32.GetCursorPos(ctypes.byref(p)):
            return (float(p.x), float(p.y))
        return None

    def screen_union(self) -> Optional[tuple[float, float, float, float]]:
        try:
            gsm = self.user32.GetSystemMetrics
            return (
                float(gsm(76)),  # SM_XVIRTUALSCREEN
                float(gsm(77)),  # SM_YVIRTUALSCREEN
                float(gsm(78)),  # SM_CXVIRTUALSCREEN
                float(gsm(79)),  # SM_CYVIRTUALSCREEN
            )
        except Exception:
            return None

    def key_down(self, key: str) -> bool:
        vk = WIN_VK.get(key)
        if vk is None:
            return False
        return bool(self.user32.GetAsyncKeyState(vk) & 0x8000)

    def button_down(self, button: str) -> bool:
        vk = {"left_mouse": 0x01, "right_mouse": 0x02, "middle_mouse": 0x04}.get(button)
        if vk is None:
            return False
        return bool(self.user32.GetAsyncKeyState(vk) & 0x8000)


class LinuxMouse(MouseBackend):
    """Linux：uinput 虚拟设备。

    这是三个平台里唯一「内核级」的注入方式，因此也是最通用的：
    它对游戏里的原始输入（raw input）同样可见。
    代价是需要 /dev/uinput 的写权限（通常要把用户加进 input 组，或写一条 udev 规则）。
    """

    name = "linux/uinput"

    def __init__(self) -> None:
        self.available = False
        self._fd = -1
        self._setup_uinput()

    def _setup_uinput(self) -> None:
        EV_KEY, EV_REL, EV_SYN = 0x01, 0x02, 0x00
        REL_X, REL_Y, REL_WHEEL = 0x00, 0x01, 0x08
        BTN_LEFT, BTN_RIGHT, BTN_MIDDLE = 0x110, 0x111, 0x112
        UI_SET_EVBIT, UI_SET_RELBIT, UI_SET_KEYBIT = 0x40045564, 0x40045565, 0x40045566
        UI_DEV_CREATE = 0x5501
        BUS_USB = 0x03

        class InputId(ctypes.Structure):
            _fields_ = [
                ("bustype", ctypes.c_uint16),
                ("vendor", ctypes.c_uint16),
                ("product", ctypes.c_uint16),
                ("version", ctypes.c_uint16),
            ]

        class UInputSetup(ctypes.Structure):
            _fields_ = [
                ("id", InputId),
                ("name", ctypes.c_char * 80),
                ("ff_effects_max", ctypes.c_uint32),
                ("absmax", ctypes.c_int32 * 64),
                ("absmin", ctypes.c_int32 * 64),
                ("absfuzz", ctypes.c_int32 * 64),
                ("absflat", ctypes.c_int32 * 64),
            ]

        try:
            self._fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
        except OSError as e:
            raise RuntimeError(
                f"打不开 /dev/uinput（{e.strerror}）。\n"
                "  修法：sudo modprobe uinput && sudo usermod -aG input $USER（重登后生效）"
            ) from e

        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        for ev in (EV_KEY, EV_REL, EV_SYN):
            libc.ioctl(self._fd, UI_SET_EVBIT, ev)
        for rel in (REL_X, REL_Y, REL_WHEEL):
            libc.ioctl(self._fd, UI_SET_RELBIT, rel)
        for btn in (BTN_LEFT, BTN_RIGHT, BTN_MIDDLE):
            libc.ioctl(self._fd, UI_SET_KEYBIT, btn)

        setup = UInputSetup()
        setup.id = InputId(BUS_USB, 0x1234, 0x5678, 1)
        setup.name = b"ListenLook Virtual Mouse"
        libc.write(self._fd, ctypes.byref(setup), ctypes.sizeof(setup))
        libc.ioctl(self._fd, UI_DEV_CREATE)
        self._libc = libc
        self.available = True

    class InputEvent(ctypes.Structure):
        _fields_ = [
            ("time", ctypes.c_long),
            ("type", ctypes.c_uint16),
            ("code", ctypes.c_uint16),
            ("value", ctypes.c_int32),
        ]

    def _emit(self, etype: int, code: int, value: int) -> None:
        ev = LinuxMouse.InputEvent(0, etype, code, value)
        os.write(self._fd, bytes(ev))

    def move_relative(self, dx: float, dy: float) -> None:
        if int(dx):
            self._emit(0x02, 0x00, int(dx))
        if int(dy):
            self._emit(0x02, 0x01, int(dy))
        self._emit(0x00, 0x00, 0)  # EV_SYN

    def scroll(self, notches: int) -> None:
        if notches:
            self._emit(0x02, 0x08, notches)
            self._emit(0x00, 0x00, 0)

    def key_down(self, key: str) -> bool:
        return False

    def button_down(self, button: str) -> bool:
        return False

    def close(self) -> None:
        if self._fd >= 0:
            try:
                self._libc.ioctl(self._fd, 0x5502)  # UI_DEV_DESTROY
                os.close(self._fd)
            except Exception:
                pass


class DryRunMouse(MouseBackend):
    """不碰鼠标，只统计"本来会移动多少"。用于调试和自动化测试。"""

    name = "dry-run"

    def __init__(self) -> None:
        self.dx = 0.0
        self.dy = 0.0
        self.scrolls = 0
        self.moves = 0
        self._pos = [0.0, 0.0]

    def move_relative(self, dx: float, dy: float) -> None:
        self.dx += dx
        self.dy += dy
        self._pos[0] += dx
        self._pos[1] += dy
        self.moves += 1

    def move_absolute(self, x: float, y: float) -> None:
        self.moves += 1
        self._pos = [x, y]

    def scroll(self, notches: int) -> None:
        self.scrolls += notches

    def position(self) -> Optional[tuple[float, float]]:
        return (self._pos[0], self._pos[1])

    def screen_union(self) -> Optional[tuple[float, float, float, float]]:
        return (0.0, 0.0, 1920.0, 1080.0)


def make_mouse(output: str) -> MouseBackend:
    if output == "dry":
        return DryRunMouse()
    try:
        if SYSTEM == "Darwin":
            return DarwinMouse()
        if SYSTEM == "Windows":
            return WindowsMouse()
        if SYSTEM == "Linux":
            return LinuxMouse()
    except Exception as e:
        print(f"⚠️  {SYSTEM} 鼠标后端初始化失败：{e}", file=sys.stderr)
        print("    退回到 dry-run（不会真的移动鼠标）", file=sys.stderr)
    return DryRunMouse()


# 键码表。只列了「当开关用」够用的那些，避免把整个 HID 表搬进来。
MAC_KEYCODES = {
    "caps_lock": 57,
    "tab": 48,
    "f8": 100,
    "f9": 101,
    "f10": 109,
    "shift": 56,
    "control": 59,
    "option": 58,
    "command": 55,
    "space": 49,
}
WIN_VK = {
    "caps_lock": 0x14,
    "tab": 0x09,
    "f8": 0x77,
    "f9": 0x78,
    "f10": 0x79,
    "shift": 0x10,
    "control": 0x11,
    "option": 0x12,
    "command": 0x5B,
    "space": 0x20,
}


# ─────────────────────────────────────────────────────────────────────────────
# 映射引擎
# ─────────────────────────────────────────────────────────────────────────────


def soft_deadzone(x: float, dz: float) -> float:
    """死区。用「减掉死区」而不是「死区内归零、死区外原值」——
    后者在死区边界上是不连续的，光标会在跨过边界时突然跳一下。"""
    if dz <= 0:
        return x
    if abs(x) <= dz:
        return 0.0
    return math.copysign(abs(x) - dz, x)


class Mapper:
    """姿态 → 鼠标增量。

    三种模式的区别值得说清楚，因为手感完全不同：

      relative（默认）角度偏移 → 速度。头转到哪，光标就朝那个方向持续走。
                       能"停"在一个偏角上保持移动，适合缓慢探索/无障碍场景。
      rate            角速度 → 速度。头像鼠标一样"甩"，停下就不动。
                       幅度可控、最容易适应，玩射击游戏时微调更自然。
      absolute        角度 → 屏幕绝对位置。适合"用头指屏幕上的东西"，
                       但需要大角度，玩游戏不合适。
    """

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.fx = OneEuro(cfg.min_cutoff, cfg.beta, cfg.d_cutoff)
        self.fy = OneEuro(cfg.min_cutoff, cfg.beta, cfg.d_cutoff)
        self.fz = OneEuro(cfg.min_cutoff, cfg.beta, cfg.d_cutoff)
        self._acc_x = 0.0
        self._acc_y = 0.0
        self._prev_angle: dict[str, float] = {}
        self._wheel_acc = 0.0
        self._idle_since: Optional[float] = None
        self.center_yaw = 0.0
        self.center_pitch = 0.0

    def recenter(self, pose: Pose) -> None:
        self.center_yaw = pose.yaw
        self.center_pitch = pose.pitch
        self.fx.reset()
        self.fy.reset()
        self.fz.reset()
        self._acc_x = self._acc_y = self._wheel_acc = 0.0

    def _axis_value(self, name: str, pose: Pose) -> float:
        v = {"yaw": pose.yaw, "pitch": pose.pitch, "roll": pose.roll}[name]
        if (name == "yaw" and self.cfg.invert_yaw) or (
            name == "pitch" and self.cfg.invert_pitch
        ) or (name == "roll" and self.cfg.invert_roll):
            v = -v
        return v

    def step(self, pose: Pose, dt: float, wheel_only: bool = False) -> tuple[float, float, int]:
        """返回本帧应该注入的 (dx, dy, wheel_notches)。dx/dy 已含小数累积。"""
        cfg = self.cfg
        dt = max(dt, 1e-4)

        ax = "yaw"
        ay = "pitch"
        if cfg.swap_xy:
            ax, ay = ay, ax

        raw_x = self._axis_value(ax, pose) - (self.center_yaw if ax == "yaw" else self.center_pitch)
        raw_y = self._axis_value(ay, pose) - (self.center_pitch if ay == "pitch" else self.center_yaw)

        if cfg.smooth:
            sx = self.fx(raw_x, dt)
            sy = self.fy(raw_y, dt)
        else:
            sx, sy = raw_x, raw_y

        # ── 滚轮（歪头滚动）
        notches = 0
        if cfg.wheel_axis != "none":
            w = self._axis_value(cfg.wheel_axis, pose)
            prev = self._prev_angle.get("wheel", w)
            self._wheel_acc += (w - prev) * (-1.0 if cfg.invert_wheel else 1.0)
            self._prev_angle["wheel"] = w
            per = max(cfg.wheel_deg_per_notch, 0.5)
            while abs(self._wheel_acc) >= per:
                notches += 1 if self._wheel_acc > 0 else -1
                self._wheel_acc -= per if self._wheel_acc > 0 else -per

        if wheel_only:
            return 0.0, 0.0, notches

        # ── 光标速度 / 位置
        if cfg.mode == "absolute":
            b = None
            half_range = max(cfg.absolute_range_deg, 1.0)
            # 归一化到 -1..1，再用 gain 当"满屏比例"用（absolute 下 curve 不起作用）
            nx = max(-1.0, min(1.0, sx / half_range))
            ny = max(-1.0, min(1.0, sy / half_range))
            return nx, ny, notches  # 交由调用方按屏幕尺寸解释

        if cfg.mode == "rate":
            # 角速度 → 速度
            px = self._prev_angle.get("x")
            py = self._prev_angle.get("y")
            vx = 0.0 if px is None else (sx - px) / dt
            vy = 0.0 if py is None else (sy - py) / dt
            self._prev_angle["x"], self._prev_angle["y"] = sx, sy
            vx = soft_deadzone(vx, cfg.deadzone_deg * 4.0)  # 角速度的死区按比例放大
            vy = soft_deadzone(vy, cfg.deadzone_deg * 4.0)
            speed_x = cfg.gain * abs(vx) ** cfg.curve * (1 if vx >= 0 else -1)
            speed_y = cfg.gain * abs(vy) ** cfg.curve * (1 if vy >= 0 else -1)
        else:
            # 角度偏移 → 速度（relative，默认）
            off_x = soft_deadzone(sx, cfg.deadzone_deg)
            off_y = soft_deadzone(sy, cfg.deadzone_deg)
            speed_x = cfg.gain * abs(off_x) ** cfg.curve * (1 if off_x >= 0 else -1)
            speed_y = cfg.gain * abs(off_y) ** cfg.curve * (1 if off_y >= 0 else -1)
            # pitch 正值 = 抬头。屏幕坐标 y 向下，所以抬头 → 鼠标上移 → dy 为负。
            speed_y = -speed_y

        # dy 的符号约定在这里收口：负 = 光标上移。macOS(CGEvent) 与 Windows(SendInput)
        # 的屏幕坐标都是 y 向下为正，所以两个平台不需要分支。

        speed = math.hypot(speed_x, speed_y)
        if speed > cfg.max_speed_px_s:
            k = cfg.max_speed_px_s / speed
            speed_x *= k
            speed_y *= k

        step_x = speed_x * dt
        step_y = speed_y * dt
        step = math.hypot(step_x, step_y)
        if step > cfg.max_step_px:
            k = cfg.max_step_px / step
            step_x *= k
            step_y *= k

        self._acc_x += step_x
        self._acc_y += step_y
        ix, iy = math.trunc(self._acc_x), math.trunc(self._acc_y)
        self._acc_x -= ix
        self._acc_y -= iy
        return float(ix), float(iy), notches

    def maybe_auto_recenter(self, pose: Pose, dt: float) -> None:
        cfg = self.cfg
        if cfg.auto_recenter_idle_s <= 0:
            return
        # "静止"判据用角度变化率，而不是角度本身 —— 角度大不代表在动
        px = self._prev_angle.get("idle")
        moving = px is not None and abs(pose.yaw - px) / dt > 1.5
        self._prev_angle["idle"] = pose.yaw
        if moving:
            self._idle_since = None
            return
        now = time.perf_counter()
        if self._idle_since is None:
            self._idle_since = now
        elif now - self._idle_since > cfg.auto_recenter_idle_s:
            self.recenter(pose)
            self._idle_since = None


# ─────────────────────────────────────────────────────────────────────────────
# 姿态来源：UDP / 模拟 / 手机浏览器
# ─────────────────────────────────────────────────────────────────────────────


class ImuFuser:
    """加速度计 + 陀螺仪 → 姿态（互补滤波）。

    为什么需要它：能推到手机外面的传感器数据，最常见的就是**原始 accel + gyro**。
    大量现成工具（Sensor Logger、phyphox、Tasker 脚本、甚至 ESP32 + MPU6050 之类的自制硬件）
    都只会推这两路原始数据，不会推融合好的姿态。没有这一步，它们全都用不了 ——
    而融合只要三十行。

    原理很朴素，也够用：
      · 陀螺仪积分 → 短期准、长期漂（会漂到你怀疑人生）
      · 加速度计测重力方向 → 长期准、短期被运动噪声带飞
      · 两者按频率互补：快变信陀螺仪，慢变信加速度计

    ⚠️ 诚实边界：**yaw 没有绝对参考**，只能靠陀螺仪积分，所以它会持续漂移。
    对"用头控制鼠标"这件事这没问题 —— 我们要的本来就是相对转动量，玩家按一下归零就完事。
    但别拿它当 AHRS 用。这不是 Mahony/Madgwick，没有磁力计校正。
    """

    def __init__(self, accel_alpha: float = 0.02) -> None:
        self.accel_alpha = accel_alpha
        self.roll = 0.0
        self.pitch = 0.0
        self.yaw = 0.0
        self._init = False

    def update(self, accel: tuple[float, float, float],
               gyro: tuple[float, float, float], dt: float) -> tuple[float, float, float]:
        ax, ay, az = accel
        gx, gy, gz = gyro  # 期望单位：度/秒
        dt = max(1e-4, min(dt, 0.2))  # 夹住 dt：卡顿一下不应该让积分飞出去

        norm = math.sqrt(ax * ax + ay * ay + az * az)
        if not math.isfinite(norm) or norm < 1e-6:
            return self.yaw, self.pitch, self.roll

        # 加速度计给出的倾角（重力方向）。单位：度。
        acc_roll = math.degrees(math.atan2(ay, az))
        acc_pitch = math.degrees(math.atan2(-ax, math.sqrt(ay * ay + az * az)))

        if not self._init:
            self.roll, self.pitch = acc_roll, acc_pitch
            self._init = True
            return self.yaw, self.pitch, self.roll

        # 陀螺仪积分（快变）
        self.roll += gx * dt
        self.pitch += gy * dt
        self.yaw += gz * dt

        # 加速度计校正（慢变），把积分的漂移按比例拉回来
        a = self.accel_alpha
        self.roll += a * (((acc_roll - self.roll + 180) % 360) - 180)
        self.pitch += a * (((acc_pitch - self.pitch + 180) % 360) - 180)
        return self.yaw, self.pitch, self.roll


# 键名别名表。设计目标是"宽容"：不同工具对同一个物理量的叫法差异极大，
# 与其要求用户改发送端，不如在这里多认几种写法。
_ALIASES = {
    "yaw": {"yaw", "yawdeg", "azimuth", "heading", "alpha", "rotz", "zrotation", "yawangle"},
    "pitch": {"pitch", "pitchdeg", "beta", "elevation", "elev", "rotx", "xrotation", "pitchangle"},
    "roll": {"roll", "rolldeg", "gamma", "bank", "roty", "yrotation", "rollangle"},
}


def _triple(d: dict) -> Optional[tuple[str, tuple[float, float, float]]]:
    """从一个 dict 里认出"三元组"数值。返回 (形式, 三个数)。"""
    for keys in (("x", "y", "z"), ("ax", "ay", "az"), ("gx", "gy", "gz")):
        if all(
            k in d and isinstance(d[k], (int, float)) and not isinstance(d[k], bool)
            for k in keys
        ):
            return keys[0], (float(d[keys[0]]), float(d[keys[1]]), float(d[keys[2]]))
    inner = d.get("values")
    if isinstance(inner, dict):
        return _triple(inner)
    return None


def _collect(obj, nodes: list, scalars: dict, hint: str = "") -> None:
    """把任意 JSON 拆成两类东西：**带名字的三元组节点** 和 **散落的标量字段**。

    早期版本把所有数值拍平进一个字典，结果是致命且隐蔽的：
    `{accel:{x,y,z}, gyro:{x,y,z}}` 里陀螺仪的 z 会被加速度计的 z 占掉（同名键只能留一个），
    于是融合拿到的是"半个加速度计 + 半个陀螺仪"，表现是姿态能算出来但完全是错的。
    所以这里必须按节点收集，不能拍平。
    """
    if isinstance(obj, dict):
        h = str(obj.get("name") or obj.get("type") or obj.get("sensor") or hint or "").lower()
        for k, val in obj.items():
            kl = str(k).lower()
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                scalars.setdefault(kl, float(val))
            elif isinstance(val, str):
                scalars.setdefault("_str_" + kl, val)
        t = _triple(obj)
        if t:
            nodes.append((h, t[0], t[1]))
        for k, val in obj.items():
            if isinstance(val, (dict, list)):
                # 键名本身就是线索：`{"accel": {...}}` 里的 accel 必须传给子节点
                _collect(val, nodes, scalars, h or str(k).lower())
    elif isinstance(obj, list):
        for item in obj:
            _collect(item, nodes, scalars, hint)


# 朝向类节点的关键词。注意 "rotation_vector"（安卓的朝向传感器）和
# "rotationrate"（陀螺仪）长得很像但语义完全相反，必须分开。
_ORIENT_HINTS = ("orient", "attitude", "euler", "azimuth", "heading", "rotation_vector",
                 "rotationvector", "quaternion")
_GYRO_HINTS = ("gyro", "rotationrate", "angular", "rotation_rate")
_ACCEL_HINTS = ("accel", "gravity", "gforce", "g_force", "linearacceleration")


def extract_pose(obj, fuser: Optional["ImuFuser"], dt: float) -> tuple[Pose, str]:
    """从**任意形状**的 JSON 里尽力提取一个姿态。

    返回 (pose, 说明)。说明会回给发送端 —— 这类工具最烦的就是
    "我明明发了数据，它却说没收到"，所以必须把"你的字段被怎么理解的"讲出来。

    识别顺序是有讲究的：显式角度 > 四元数 > 朝向节点 > 原始 IMU 融合。
    越靠前的越可靠，绝不能反过来（融合出来的角度会漂，不该盖过明确给出的角度）。
    """
    nodes: list = []
    nums: dict = {}
    _collect(obj, nodes, nums)
    src = str(nums.get("_str_src") or nums.get("_str_deviceid") or nums.get("_str_devicename")
              or nums.get("_str_name") or "http")

    # ① 显式角度字段
    found: dict = {}
    for axis, names in _ALIASES.items():
        for n in names:
            if n in nums:
                found[axis] = nums[n]
                break
    if len(found) == 3:
        # 浏览器/手机那套 alpha/beta/gamma：alpha 的正方向与我们的 yaw 相反
        if any(k in nums for k in ("alpha", "beta", "gamma")) and "yaw" not in nums:
            found["yaw"] = -found["yaw"]
        return (Pose(yaw=found["yaw"], pitch=found["pitch"], roll=found["roll"],
                     ok=True, src=src), "显式角度字段")
    if len(found) == 2 and "yaw" not in found:
        return (Pose(yaw=0.0, pitch=found["pitch"], roll=found["roll"], ok=True, src=src),
                "只有倾角字段（无 yaw）")

    # ② 四元数
    q = None
    if all(k in nums for k in ("qw", "qx", "qy", "qz")):
        q = (nums["qw"], nums["qx"], nums["qy"], nums["qz"])
    else:
        for h, kind, t in nodes:
            if "quaternion" in h or "attitude" in h:
                if all(k in nums for k in ("w", "x", "y", "z")):
                    q = (nums["w"], nums["x"], nums["y"], nums["z"])
                    break
    if q:
        w, x, y, z = q
        n = math.sqrt(w * w + x * x + y * y + z * z)
        if n > 1e-6:
            w, x, y, z = w / n, x / n, y / n, z / n
            yaw = math.degrees(math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z)))
            pitch = math.degrees(math.asin(max(-1.0, min(1.0, 2 * (w * y - z * x)))))
            roll = math.degrees(math.atan2(2 * (w * x + y * z), 1 - 2 * (x * x + y * y)))
            return (Pose(yaw=yaw, pitch=pitch, roll=roll, ok=True, src=src), "四元数")

    # ③ 朝向节点：安卓的 orientation 传感器就是 (azimuth, pitch, roll) 三元组
    for h, _kind, t in nodes:
        if any(w in h for w in _ORIENT_HINTS):
            return (Pose(yaw=t[0], pitch=t[1], roll=t[2], ok=True, src=src),
                    "朝向节点（按 x=方位角, y=俯仰, z=翻滚 解释）")

    # ④ 原始 IMU
    accel = gyro = None
    if all(k in nums for k in ("ax", "ay", "az")):
        accel = (nums["ax"], nums["ay"], nums["az"])
    if all(k in nums for k in ("gx", "gy", "gz")):
        gyro = (nums["gx"], nums["gy"], nums["gz"])
    for h, kind, t in nodes:
        if accel is None and kind in ("x", "a") and any(w in h for w in _ACCEL_HINTS):
            accel = t
        if gyro is None and kind in ("x", "g") and any(w in h for w in _GYRO_HINTS):
            gyro = t

    if accel and gyro and fuser is not None:
        y, pi, r = fuser.update(accel, gyro, dt)
        return (Pose(yaw=y, pitch=pi, roll=r, ok=True, src=src), "加速度计+陀螺仪互补滤波")
    if accel:
        # 只有加速度计：能给出静态倾角，但转头测不出来（重力方向没变）
        ax, ay, az = accel
        return (Pose(yaw=0.0,
                     pitch=math.degrees(math.atan2(-ax, math.sqrt(ay * ay + az * az))),
                     roll=math.degrees(math.atan2(ay, az)), ok=True, src=src),
                "仅加速度计（无 yaw）")

    return (Pose(ok=False, src=src), "没找到可用字段")



def parse_datagram(data: bytes, fmt: str) -> Optional[Pose]:
    """解析一个 UDP 数据报。自动识别 ListenLook JSON 与 OpenTrack FreePIE 二进制。"""
    if fmt in ("auto", "freepie") and len(data) == 48:
        # OpenTrack FreePIE：6 个 little-endian float64 = yaw,pitch,roll,x,y,z
        try:
            vals = struct.unpack("<6d", data)
        except struct.error:
            return None
        if all(math.isfinite(v) for v in vals):
            return Pose(yaw=vals[0], pitch=vals[1], roll=vals[2], ok=True, src="freepie")
        return None
    if fmt == "freepie":
        return None

    try:
        obj = json.loads(data.decode("utf-8", "replace"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(obj, dict):
        return None
    if int(obj.get("v", 0)) != PROTO_VERSION:
        return None
    pose = Pose(
        yaw=float(obj.get("yaw", 0.0)),
        pitch=float(obj.get("pitch", 0.0)),
        roll=float(obj.get("roll", 0.0)),
        ok=bool(obj.get("ok", False)),
        src=str(obj.get("src", "?")),
        seq=int(obj.get("seq", 0)),
        t=float(obj.get("t", 0.0)),
    )
    if not pose.finite():
        return None
    if not pose.ok:
        # ok:false 也要收：接收端需要知道"传感器挂了"，而不是以为"玩家没动"
        return pose
    return pose


class UdpSource(threading.Thread):
    def __init__(self, cfg: Config, state: PoseState) -> None:
        super().__init__(daemon=True, name="udp")
        self.cfg = cfg
        self.state = state
        self.stop_flag = threading.Event()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((cfg.bind, cfg.udp_port))
        self.sock.settimeout(0.3)
        self.bound = self.sock.getsockname()

    def run(self) -> None:
        while not self.stop_flag.is_set():
            try:
                data, _addr = self.sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            pose = parse_datagram(data, self.cfg.fmt)
            if pose is None:
                self.state.bump_bad()
                continue
            self.state.update(pose)
        self.sock.close()


class SimSource(threading.Thread):
    """没有硬件时的合成姿态源：慢速画 8 字 + 一点呼吸抖动。

    存在的理由很实际：绝大多数想试这个工具的人手边没有 AirPods，
    或者想在改配置时先看效果。没有它，第一次运行只能看到"掉线"。
    """

    def __init__(self, state: PoseState) -> None:
        super().__init__(daemon=True, name="sim")
        self.state = state
        self.stop_flag = threading.Event()

    def run(self) -> None:
        t0 = time.perf_counter()
        while not self.stop_flag.is_set():
            t = time.perf_counter() - t0
            self.state.update(
                Pose(
                    yaw=18.0 * math.sin(t * 0.7),
                    pitch=10.0 * math.sin(t * 1.1 + 0.7),
                    roll=6.0 * math.sin(t * 0.35),
                    ok=True,
                    src="sim",
                    seq=int(t * 100),
                    t=time.time() * 1000.0,
                )
            )
            time.sleep(1 / 100)


# ── 手机当传感器：一个极小的 WebSocket 服务 + 一个自带传感器页 ──────────────


PHONE_PAGE = r"""<!DOCTYPE html>
<html lang="zh"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>ListenLook 手机传感器</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  body { margin:0; background:#0b0d10; color:#e8eaed;
         font:15px/1.5 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif; }
  .wrap { max-width:520px; margin:0 auto; padding:20px 16px 40px; }
  h1 { font-size:19px; margin:0 0 4px; letter-spacing:.3px; }
  .sub { color:#8b949e; font-size:13px; margin-bottom:18px; }
  button { font:inherit; border:0; border-radius:12px; padding:15px 18px; color:#fff;
           background:#2f81f7; width:100%; font-weight:600; }
  button:active { opacity:.8; }
  button.ghost { background:#21262d; color:#c9d1d9; font-weight:500; }
  .row { display:flex; gap:10px; margin-top:10px; }
  .card { background:#111418; border:1px solid #21262d; border-radius:14px; padding:14px; margin-top:14px; }
  .axis { margin:12px 0; }
  .axis .top { display:flex; justify-content:space-between; font-size:12px; color:#8b949e;
               letter-spacing:.5px; margin-bottom:6px; }
  .axis b { color:#e8eaed; font-variant-numeric:tabular-nums; font-size:13px; }
  .track { position:relative; height:10px; background:#1c2128; border-radius:6px; overflow:hidden; }
  .fill { position:absolute; top:0; bottom:0; left:50%; width:0; background:#2f81f7; border-radius:6px; }
  .mid { position:absolute; left:50%; top:-2px; bottom:-2px; width:1px; background:#30363d; }
  #status { margin-top:14px; font-size:13px; color:#8b949e; }
  .dot { display:inline-block; width:8px; height:8px; border-radius:50%; background:#f85149;
         margin-right:7px; vertical-align:1px; }
  .dot.on { background:#3fb950; }
  label.sw { display:flex; align-items:center; justify-content:space-between;
             padding:9px 0; font-size:14px; color:#c9d1d9; border-bottom:1px solid #1c2128; }
  label.sw:last-child { border-bottom:0; }
  input[type=checkbox] { width:20px; height:20px; accent-color:#2f81f7; }
  .hint { color:#6e7681; font-size:12px; margin-top:10px; }
</style></head>
<body><div class="wrap">
  <h1>ListenLook 手机传感器</h1>
  <div class="sub">把这个页面留在前台，手机就是头部追踪器。</div>

  <button id="go">开始（需要授权动作传感器）</button>

  <div class="card">
    <div class="axis"><div class="top"><span>YAW 左右转头</span><b id="vy">0°</b></div>
      <div class="track"><i class="mid"></i><i class="fill" id="fy"></i></div></div>
    <div class="axis"><div class="top"><span>PITCH 抬头低头</span><b id="vp">0°</b></div>
      <div class="track"><i class="mid"></i><i class="fill" id="fp"></i></div></div>
    <div class="axis"><div class="top"><span>ROLL 歪头</span><b id="vr">0°</b></div>
      <div class="track"><i class="mid"></i><i class="fill" id="fr"></i></div></div>
  </div>

  <div class="card">
    <label class="sw">YAW 反向<input type="checkbox" id="iy"></label>
    <label class="sw">PITCH 反向<input type="checkbox" id="ip"></label>
    <label class="sw">ROLL 反向<input type="checkbox" id="ir"></label>
  </div>

  <div class="row">
    <button class="ghost" id="zero">归零（当前朝向 = 正前方）</button>
  </div>

  <div id="status"><span class="dot"></span>未连接</div>
  <div class="hint">提示：把手机横屏绑在头上时，先点「归零」再开始玩。iOS 需要点上面的按钮授权；
    页面切到后台会停止上报，这是浏览器的限制，不是 bug。</div>
</div>
<script>
(() => {
  const $ = id => document.getElementById(id);
  let ws = null, running = false, zero = null, last = {a:0,b:0,g:0}, frames = 0, t0 = 0;
  const inv = { y:false, p:false, r:false };
  for (const k of ['y','p','r']) {
    const el = $('i'+k);
    el.checked = localStorage.getItem('inv_'+k) === '1';
    inv[k] = el.checked;
    el.onchange = () => { inv[k] = el.checked; localStorage.setItem('inv_'+k, el.checked?'1':'0'); };
  }
  const status = (txt, on) => { $('status').innerHTML = '<span class="dot'+(on?' on':'')+'"></span>'+txt; };

  function connect() {
    const proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
    ws = new WebSocket(proto + location.host + '/ws');
    ws.onopen = () => status('已连接，等待传感器数据…', true);
    ws.onclose = () => { status('连接断开，3 秒后重连…', false); setTimeout(connect, 3000); };
    ws.onerror = () => {};
  }

  function delta(cur, prev) {           // 归一化到 (-180,180]，处理 359→1 的跨圈
    let d = cur - prev;
    while (d > 180) d -= 360;
    while (d < -180) d += 360;
    return d;
  }

  function onOrient(e) {
    if (e.alpha === null && e.beta === null && e.gamma === null) return;
    const a = e.alpha || 0, b = e.beta || 0, g = e.gamma || 0;
    if (zero === null) zero = { a, b, g };

    // 相对角度。alpha 是绕 Z 轴（转头），beta 是前后倾（抬头），gamma 是左右倾（歪头）。
    let yaw   = -delta(a, zero.a) * (inv.y ? -1 : 1);
    let pitch =  delta(b, zero.b) * (inv.p ? -1 : 1);
    let roll  =  delta(g, zero.g) * (inv.r ? -1 : 1);

    $('vy').textContent = yaw.toFixed(1) + '°';
    $('vp').textContent = pitch.toFixed(1) + '°';
    $('vr').textContent = roll.toFixed(1) + '°';
    const bar = (fill, v, range) => {
      const pct = Math.max(-1, Math.min(1, v / range)) * 50;
      fill.style.width = Math.abs(pct) + '%';
      fill.style.left = (pct >= 0 ? 50 : 50 + pct) + '%';
    };
    bar($('fy'), yaw, 90); bar($('fp'), pitch, 60); bar($('fr'), roll, 60);

    frames++;
    const now = performance.now();
    if (now - t0 > 1000) { t0 = now; /* 速率仅用于显示 */ }
    if (ws && ws.readyState === 1) {
      ws.send(JSON.stringify({
        v: 1, src: 'phone', ok: true, seq: frames, t: Date.now(),
        yaw: +yaw.toFixed(3), pitch: +pitch.toFixed(3), roll: +roll.toFixed(3)
      }));
    }
  }

  $('go').onclick = async () => {
    if (running) { running = false; window.removeEventListener('deviceorientation', onOrient);
      status('已暂停', false); $('go').textContent = '继续'; return; }
    try {
      // iOS 13+ 必须在用户手势里申请权限，否则事件永远不来（且不报错）
      if (typeof DeviceOrientationEvent !== 'undefined' &&
          typeof DeviceOrientationEvent.requestPermission === 'function') {
        const p = await DeviceOrientationEvent.requestPermission();
        if (p !== 'granted') { status('传感器权限被拒绝', false); return; }
      }
    } catch (err) { status('权限申请失败：' + err.message, false); return; }
    window.addEventListener('deviceorientation', onOrient);
    running = true; zero = null; $('go').textContent = '暂停';
    status('正在上报', true);
  };
  $('zero').onclick = () => { zero = { a: last.a, b: last.b, g: last.g }; };

  connect();
})();
</script>
</body></html>
"""


class PhoneHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ListenLook/" + VERSION
    state: PoseState = None  # 由 serve_phone() 注入
    cfg: Config = None

    def log_message(self, fmt: str, *args) -> None:  # 静音默认访问日志
        pass

    def _send(self, code: int, ctype: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", PHONE_PAGE.encode("utf-8"))
        elif path == "/pose.json":
            pose, fresh, _ = self.state.get()
            self._send(200, "application/json", json.dumps({**asdict(pose), "fresh": fresh}).encode())
        elif path == "/health":
            self._send(200, "text/plain", b"ok\n")
        elif path == "/ws":
            self._websocket()
        else:
            self._send(404, "text/plain", b"not found\n")

    # ── 极简 WebSocket（RFC 6455）服务端。只为收手机的姿态流，
    #    所以只实现必需的那部分：握手、文本帧、分片累积、ping/pong、close。
    def _websocket(self) -> None:
        key = self.headers.get("Sec-WebSocket-Key")
        if not key:
            self._send(400, "text/plain", b"expected websocket\n")
            return
        accept = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
        ).decode()
        self.wfile.write(
            (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
            ).encode()
        )
        self.wfile.flush()
        sock = self.connection
        sock.settimeout(1.0)
        buf = b""
        frag = b""
        try:
            while True:
                try:
                    frame = self._read_frame(sock, buf)
                except socket.timeout:
                    continue
                if frame is None:
                    break
                buf = b""
                opcode, payload, fin = frame
                if opcode == 0x8:  # close
                    break
                if opcode == 0x9:  # ping → pong
                    self._write_frame(sock, 0xA, payload)
                    continue
                if opcode == 0xA:
                    continue
                if opcode in (0x1, 0x2):
                    frag = payload
                    if not fin:
                        continue
                elif opcode == 0x0:
                    frag += payload
                    if not fin:
                        continue
                else:
                    continue
                self._handle_message(frag)
                frag = b""
        except (OSError, ConnectionError):
            pass
        finally:
            try:
                sock.close()
            except OSError:
                pass
            self.close_connection = True

    def _read_frame(self, sock, _buf: bytes):
        def recvn(n: int) -> bytes:
            out = b""
            while len(out) < n:
                chunk = sock.recv(n - len(out))
                if not chunk:
                    raise ConnectionError("eof")
                out += chunk
            return out

        hdr = recvn(2)
        b0, b1 = hdr[0], hdr[1]
        fin = bool(b0 & 0x80)
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        length = b1 & 0x7F
        if length == 126:
            length = struct.unpack(">H", recvn(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", recvn(8))[0]
        if length > 1 << 20:  # 1MB 保护：姿态帧不可能这么大
            raise ConnectionError("frame too large")
        mask = recvn(4) if masked else b"\x00" * 4
        payload = recvn(length) if length else b""
        if masked:
            payload = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
        return opcode, payload, fin

    def _write_frame(self, sock, opcode: int, payload: bytes) -> None:
        head = bytes([0x80 | opcode])
        n = len(payload)
        if n < 126:
            head += bytes([n])
        elif n < (1 << 16):
            head += bytes([126]) + struct.pack(">H", n)
        else:
            head += bytes([127]) + struct.pack(">Q", n)
        sock.sendall(head + payload)

    def _handle_message(self, payload: bytes) -> None:
        pose = parse_datagram(payload, "listenlook")
        if pose is None:
            self.state.bump_bad()
            return
        self.state.update(pose)


class IngestHandler(PhoneHandler):
    """纯 HTTP 摄取端点 —— 这台机器上最重要的一个接口。

    为什么必须有不带 TLS 的入口：浏览器读陀螺仪要求安全上下文（HTTPS），
    但**原生 app 没有这个限制**。Sensor Logger、phyphox、Tasker、一个 curl 脚本、
    甚至自制硬件，都可以直接往这里 POST 一段 JSON，不用管证书。

    这个端点的设计目标是"宽容"：不要求发送端遵守我们的字段命名，
    而是递归扫描 JSON、按别名匹配、必要时自己做 IMU 融合。
    因为让用户改发送端（尤其是一个已经装好的手机 app）是不现实的。
    """

    fuser: Optional[ImuFuser] = None
    lock = threading.Lock()
    last_t = 0.0

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")

    def _send(self, code: int, ctype: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:  # 浏览器跨域预检
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        # 上限 1MB：姿态帧不该这么大，挡住畸形请求
        body = self.rfile.read(min(length, 1 << 20)) if length else b""

        payload = None
        ctype = (self.headers.get("Content-Type") or "").lower()
        if "json" in ctype or body.strip().startswith((b"{", b"[")):
            try:
                payload = json.loads(body.decode("utf-8", "replace"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                payload = None
        if payload is None:
            # 也接受纯文本 "yaw,pitch,roll" —— 方便 shell 一行命令测试
            try:
                parts = [float(x) for x in body.decode().replace(",", " ").split()]
                if len(parts) >= 3:
                    payload = {"yaw": parts[0], "pitch": parts[1], "roll": parts[2]}
            except (ValueError, UnicodeDecodeError):
                payload = None
        if payload is None:
            self.state.bump_bad()
            self._send(400, "application/json",
                       json.dumps({"ok": False, "error": "无法解析请求体"}, ensure_ascii=False).encode())
            return

        with IngestHandler.lock:
            now = time.perf_counter()
            dt = now - IngestHandler.last_t if IngestHandler.last_t else 1 / 60
            IngestHandler.last_t = now
            pose, how = extract_pose(payload, self.fuser, dt)

        if not pose.finite():
            self.state.bump_bad()
            self._send(400, "application/json",
                       json.dumps({"ok": False, "error": "数值非有限"}, ensure_ascii=False).encode())
            return

        self.state.update(pose)
        self._send(200, "application/json", json.dumps({
            "ok": True, "understood_as": how, "src": pose.src,
            "yaw": round(pose.yaw, 3), "pitch": round(pose.pitch, 3), "roll": round(pose.roll, 3),
        }, ensure_ascii=False).encode())


def serve_ingest(cfg: Config, state: PoseState, fuser: ImuFuser) -> ThreadingHTTPServer:
    IngestHandler.state = state
    IngestHandler.cfg = cfg
    IngestHandler.fuser = fuser
    httpd = ThreadingHTTPServer((cfg.bind, cfg.http_port), IngestHandler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True, name="ingest").start()
    return httpd


def serve_phone(cfg: Config, state: PoseState, certfile: str, keyfile: str) -> ThreadingHTTPServer:
    PhoneHandler.state = state
    PhoneHandler.cfg = cfg
    httpd = ThreadingHTTPServer((cfg.bind, cfg.web_port), PhoneHandler)
    httpd.daemon_threads = True
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile, keyfile)
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    t = threading.Thread(target=httpd.serve_forever, daemon=True, name="phone")
    t.start()
    return httpd


def ensure_cert(certdir: str, lan_ip: str) -> tuple[str, str]:
    """手机浏览器要用陀螺仪，页面必须是安全上下文（HTTPS）。

    所以这里必须能给出证书。优先用 openssl 现场生成（SAN 里带上真实局域网 IP，
    手机只会看到"未知颁发机构"一个警告）；如果没有 openssl 就退回仓库里那份
    开发用自签证书（会多一个"名称不匹配"警告，点"继续访问"即可）。
    """
    os.makedirs(certdir, exist_ok=True)
    cert = os.path.join(certdir, "listenlook.crt")
    key = os.path.join(certdir, "listenlook.key")
    if os.path.exists(cert) and os.path.exists(key):
        return cert, key
    san = f"DNS:localhost,DNS:listenlook.local,IP:127.0.0.1,IP:{lan_ip}"
    try:
        subprocess.run(
            [
                "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                "-keyout", key, "-out", cert, "-days", "3650",
                "-subj", "/CN=listenlook.local",
                "-addext", f"subjectAltName={san}",
            ],
            check=True,
            capture_output=True,
        )
        os.chmod(key, 0o600)
        return cert, key
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        # Windows 上通常没有 openssl。这时不能直接放弃 —— 手机浏览器那条路要靠 HTTPS，
        # 而"装个手机 app 往 HTTP 端口推数据"那条更稳的路不受影响。
        # 所以退回随仓库分发的一次性自签证书（私钥是公开的，只为了让浏览器认它是安全上下文）。
        fallback_crt = os.path.join(certdir, "dev-fallback.crt")
        fallback_key = os.path.join(certdir, "dev-fallback.key")
        if os.path.exists(fallback_crt) and os.path.exists(fallback_key):
            print(
                f"ℹ️  没有 openssl（{type(e).__name__}），改用仓库自带的兜底证书。\n"
                "    手机浏览器会多一条「名称不匹配」警告，点继续访问即可。",
                file=sys.stderr,
            )
            return fallback_crt, fallback_key
        raise SystemExit(
            f"⚠️  生成自签证书失败（{e}），且找不到兜底证书 dev-fallback.crt。\n"
            "    手机浏览器要求 HTTPS 才能读陀螺仪。请安装 openssl 后重试，\n"
            "    或改用手机 app 推送（POST http://<本机IP>:8421/pose，不需要证书）。"
        )


def lan_ip() -> str:
    """拿到本机在局域网里的地址。不产生真实流量：连一个不可路由的地址只为让内核选出出口网卡。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


# ─────────────────────────────────────────────────────────────────────────────
# 面板
# ─────────────────────────────────────────────────────────────────────────────


class Panel:
    def __init__(self, cfg: Config, mouse: MouseBackend, on: bool = True) -> None:
        self.cfg = cfg
        self.mouse = mouse
        self.enabled = cfg.panel and sys.stderr.isatty()
        self.active = on
        self._last = 0.0
        self._start = time.perf_counter()
        self._frames_seen = 0

    def line(self, pose: Pose, fresh: bool, dx: float, dy: float, rate: float, note: str = "") -> None:
        if not self.enabled:
            return
        now = time.perf_counter()
        if now - self._last < 0.1:
            return
        self._last = now

        def bar(v: float, full: float, width: int = 11) -> str:
            k = max(-1.0, min(1.0, v / full))
            n = int(round(abs(k) * (width - 1) / 2))
            cells = ["·"] * width
            cells[width // 2] = "|"
            if k >= 0:
                for i in range(n):
                    cells[width // 2 + 1 + i] = "█"
            else:
                for i in range(n):
                    cells[width // 2 - 1 - i] = "█"
            return "".join(cells)

        state = "● 在线" if fresh else "○ 掉线"
        gate = "开" if self.active else "关"
        src = pose.src if fresh else "-"
        out = f"[{gate}] {state} src={src:<7} {rate:5.1f}Hz  "
        out += f"yaw{pose.yaw:+7.1f} {bar(pose.yaw, 45)}  "
        out += f"pitch{pose.pitch:+6.1f} {bar(pose.pitch, 30)}  "
        out += f"roll{pose.roll:+6.1f} {bar(pose.roll, 30)}  "
        out += f"→ {dx:+5.0f},{dy:+5.0f}px"
        if note:
            out += "  " + note
        sys.stderr.write("\r\033[K" + out)
        sys.stderr.flush()

    def finish(self) -> None:
        if self.enabled:
            sys.stderr.write("\n")
            sys.stderr.flush()


# ─────────────────────────────────────────────────────────────────────────────
# 主循环
# ─────────────────────────────────────────────────────────────────────────────


def hold_key_down(mouse: MouseBackend, key: str) -> bool:
    if key.endswith("mouse"):
        return mouse.button_down(key)
    return mouse.key_down(key)


def run(cfg: Config, mouse: MouseBackend, state: PoseState, stop: threading.Event) -> None:
    mapper = Mapper(cfg)
    panel = Panel(cfg, mouse, on=(cfg.activation == "always"))
    dt_target = 1.0 / max(1, cfg.rate_hz)
    last_t = time.perf_counter()
    last_pose_t = 0.0
    rate = 0.0
    held = False
    recentered = False
    need_recenter = False  # 掉线恢复 / 换了来源 → 必须重新取零点
    last_src = None

    stats_every = cfg.stats if cfg.stats and cfg.stats > 0 else 0.0
    t_start = time.perf_counter()
    next_stats = t_start + stats_every if stats_every else float("inf")
    stats_t = t_start  # 基准点必须是起点本身：用 next_stats 会让第一行的速率除出天文数字
    stats_frames = 0

    while not stop.is_set():
        now = time.perf_counter()
        dt = now - last_t
        if dt < dt_target:
            time.sleep(min(dt_target - dt, dt_target))
            continue
        last_t = now

        pose, fresh, frames = state.get()
        if frames != panel._frames_seen:
            rate = (frames - panel._frames_seen) / max(dt, 1e-3)
            panel._frames_seen = frames

        # ── 无人值守统计：一行 JSON，方便脚本断言「姿态真的进来过、鼠标真的动过」
        if now >= next_stats:
            f_total, f_bad = state.stats()
            rec = {
                "t": round(now - t_start, 2),
                "frames": f_total,
                "frames_delta": f_total - stats_frames,
                "rate_hz": round((f_total - stats_frames) / max(now - stats_t, 1e-6), 1),
                "bad": f_bad,
                "fresh": fresh,
                "ok": pose.ok,
                "src": pose.src,
                "yaw": round(pose.yaw, 2),
                "pitch": round(pose.pitch, 2),
                "roll": round(pose.roll, 2),
            }
            if isinstance(mouse, DryRunMouse):
                rec["dry_moved"] = [round(mouse.dx, 1), round(mouse.dy, 1)]
                rec["dry_moves"] = mouse.moves
                rec["dry_scrolls"] = mouse.scrolls
            print(json.dumps(rec, ensure_ascii=False), flush=True)
            stats_frames = f_total
            stats_t = now
            next_stats = now + stats_every

        if not fresh or not pose.ok:
            # 掉线/传感器不可用时必须立刻停手。否则光标会按最后一帧的姿态一直飘过去。
            # 同时记下"恢复后要重新取零点"：手机页面切到后台再回来、App 重启之后，
            # 玩家的头多半已经不在原来的朝向了，沿用旧零点会让光标一上来就朝一边飘。
            need_recenter = True
            if cfg.activation == "hold":
                panel.active = False
            panel.line(pose, False, 0, 0, 0.0, note="已停止输出（等数据）")
            time.sleep(dt_target)
            continue

        # 换来源（手机页面重连、换成树莓派桥）同样视为新会话 —— 两个来源的"零点"
        # 没有任何可比性，直接沿用会得到一个恒定的假偏角。
        if pose.src != last_src:
            need_recenter = True
            last_src = pose.src

        if not recentered or need_recenter:
            mapper.recenter(pose)
            recentered = True
            need_recenter = False

        if cfg.activation == "hold":
            held = hold_key_down(mouse, cfg.hold_key)
            panel.active = held
        else:
            held = True

        mapper.maybe_auto_recenter(pose, dt)
        dx, dy, notches = mapper.step(pose, dt, wheel_only=not held)

        if held:
            if cfg.mode == "absolute":
                b = mouse.screen_union() or (0.0, 0.0, 1.0, 1.0)
                # dx/dy 这里是 -1..1 的归一化位置
                mouse.move_absolute(b[0] + (dx + 1) * 0.5 * b[2], b[1] + (dy + 1) * 0.5 * b[3])
                dx = dy = 0.0
            elif dx or dy:
                mouse.move_relative(dx, dy)
        if notches:
            mouse.scroll(notches)

        panel.line(pose, True, dx, dy, rate)
        time.sleep(dt_target)

    panel.finish()


def self_test(cfg: Config, mouse: MouseBackend) -> int:
    """证明"注入链路真的通"：读当前位置 → 注入一个有界位移 → 读回 → 复位。

    这一步的意义不是好看：macOS 上缺辅助功能权限时，注入会被**静默丢弃**，
    程序自己完全不知道。不主动验一次，用户只会看到"程序在跑但鼠标不动"。
    """
    ok, msg = mouse.permission_ok()
    print(f"后端：{mouse.name}")
    if not ok:
        print("❌ " + msg)
        return 2
    start = mouse.position()
    print(f"起始光标位置：{start}")
    if start is None:
        print("⚠️  该后端不支持读位置，跳过位移验证（注入仍会工作）")
        return 0
    mouse.move_relative(40, -25)
    time.sleep(0.05)
    mid = mouse.position()
    print(f"注入 (+40,-25) 后：{mid}")
    mouse.move_absolute(start[0], start[1])
    time.sleep(0.05)
    back = mouse.position()
    print(f"复位后：{back}")
    if mid is None:
        return 2
    got = (mid[0] - start[0], mid[1] - start[1])
    print(f"观测到位移：({got[0]:+.1f},{got[1]:+.1f})  期望 (+40.0,-25.0)")
    if abs(got[0] - 40) <= 3 and abs(got[1] + 25) <= 3:
        print("✅ 注入链路正常")
        return 0
    print("❌ 位移与期望不符 —— 大概率是权限问题（macOS 辅助功能 / Linux uinput）")
    return 2


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="listenlook",
        description="ListenLook —— 把头部姿态变成鼠标（跨平台接收端）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--config", help="JSON 配置文件（命令行参数优先）")
    ap.add_argument("--print-config", action="store_true", help="打印生效配置后退出")
    ap.add_argument("--self-test", action="store_true", help="验证鼠标注入链路（会轻微移动光标）")
    ap.add_argument("--simulate", action="store_true", help="内置合成姿态源（没硬件时试玩）")
    ap.add_argument("--dry-run", action="store_true", help="不真的动鼠标，只统计")
    ap.add_argument("--no-web", action="store_true", help="不启动手机传感器页面")
    ap.add_argument("--bind", help="监听地址（默认 127.0.0.1；跨机用 0.0.0.0）")
    ap.add_argument("--udp-port", type=int, help="姿态 UDP 端口（默认 8420）")
    ap.add_argument("--web-port", type=int, help="手机页面 HTTPS 端口（默认 8443）")
    ap.add_argument("--http-port", type=int, help="纯 HTTP 摄取端口（默认 8421，app/脚本用）")
    ap.add_argument("--no-imu-fusion", action="store_true",
                    help="关闭加速度计+陀螺仪互补滤波（只用显式姿态角/四元数）")
    ap.add_argument("--format", dest="fmt", choices=["auto", "listenlook", "freepie"],
                    help="姿态格式（默认 auto，自动识别 48 字节 FreePIE 包）")
    ap.add_argument("--mode", choices=["relative", "rate", "absolute"], help="映射模式")
    ap.add_argument("--gain", type=float, help="速度增益 px/s")
    ap.add_argument("--curve", type=float, help="响应曲线指数，>1 中心更精细")
    ap.add_argument("--deadzone", type=float, dest="deadzone_deg", help="中心死区（度）")
    ap.add_argument("--max-speed", type=float, dest="max_speed_px_s", help="速度上限 px/s")
    ap.add_argument("--invert-yaw", action="store_true")
    ap.add_argument("--invert-pitch", action="store_true")
    ap.add_argument("--invert-roll", action="store_true")
    ap.add_argument("--swap-xy", action="store_true", help="交换 yaw/pitch 两个轴")
    ap.add_argument("--wheel", dest="wheel_axis", choices=["none", "roll", "yaw", "pitch"],
                    help="用哪个轴驱动滚轮")
    ap.add_argument("--hold-key", help="activation=hold 时要按住的键（f8/space/caps_lock/right_mouse…）")
    ap.add_argument("--activation", choices=["always", "hold"], help="always=一直生效，hold=按住才生效")
    ap.add_argument("--rate", type=int, dest="rate_hz", help="映射循环频率 Hz（默认 100）")
    ap.add_argument("--no-smooth", action="store_true", help="关闭 One Euro 平滑")
    ap.add_argument("--quiet", action="store_true", help="不显示实时面板")
    ap.add_argument("--stats", type=float, metavar="SEC", default=None,
                    help="每 SEC 秒往 stdout 打一行 JSON 统计（无人值守/自动化测试用）")
    ap.add_argument("--version", action="version", version=f"ListenLook {VERSION}")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    for k, v in vars(args).items():
        if v is None or k in ("config", "print_config", "self_test", "simulate", "dry_run",
                              "no_web", "no_smooth", "no_imu_fusion", "quiet", "stats", "version"):
            continue
        setattr(cfg, k, v)
    if args.no_smooth:
        cfg.smooth = False
    if args.no_imu_fusion:
        cfg.imu_fusion = False
    if args.dry_run:
        cfg.output = "dry"
    if args.quiet:
        cfg.panel = False
    if args.stats is not None:
        cfg.stats = args.stats

    if args.print_config:
        print(json.dumps(asdict(cfg), indent=2, ensure_ascii=False))
        return 0

    mouse = make_mouse(cfg.output)

    if args.self_test:
        return self_test(cfg, mouse)

    ok, msg = mouse.permission_ok()
    if not ok:
        print("⚠️  " + msg, file=sys.stderr)

    state = PoseState()
    threads: list[threading.Thread] = []
    httpd = None

    udp = UdpSource(cfg, state)
    threads.append(udp)
    udp.start()

    if args.simulate:
        sim = SimSource(state)
        threads.append(sim)
        sim.start()

    fuser = ImuFuser() if cfg.imu_fusion else None

    if not args.no_web:
        ip = lan_ip()
        # 纯 HTTP 摄取端点先起：它不依赖证书，是 Windows 用户最可能走通的那条路。
        ingest = serve_ingest(cfg, state, fuser)
        print(f"📥 姿态摄取端点（app / 脚本用，无需证书）：", file=sys.stderr)
        print(f"      POST http://{ip}:{cfg.http_port}/pose   ← 手机 app、curl、自制硬件", file=sys.stderr)
        print(f"      GET  http://{ip}:{cfg.http_port}/pose.json  （看当前姿态，调试用）", file=sys.stderr)
        try:
            cert, key = ensure_cert(
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "certs"), ip)
            httpd = serve_phone(cfg, state, cert, key)
            print(f"📱 手机浏览器页面（需 HTTPS）：https://{ip}:{cfg.web_port}/", file=sys.stderr)
            print("   （自签证书会提示不安全，选「继续访问」；若浏览器拒绝读陀螺仪，"
                  "改用上面的摄取端点 + 手机 app）", file=sys.stderr)
        except SystemExit as e:
            # 没有 openssl 也必须能跑：手机 app 那条路不需要证书
            print(f"⚠️  {e}", file=sys.stderr)
            print("   已跳过浏览器页面，摄取端点不受影响。", file=sys.stderr)

    print(f"🎧 姿态输入：UDP {udp.bound[0]}:{udp.bound[1]}  (格式 {cfg.fmt})", file=sys.stderr)
    print(f"🖱  鼠标输出：{mouse.name}   模式 {cfg.mode}  "
          f"{'按住 ' + cfg.hold_key + ' 才生效' if cfg.activation == 'hold' else '持续生效'}"
          f"{'  [dry-run 不动鼠标]' if cfg.output == 'dry' else ''}", file=sys.stderr)
    print("   Ctrl-C 退出", file=sys.stderr)

    stop = threading.Event()

    def on_signal(_sig, _frm):
        stop.set()

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    try:
        run(cfg, mouse, state, stop)
    finally:
        stop.set()
        udp.stop_flag.set()
        for t in threads:
            t.join(timeout=0.5)
        if httpd:
            httpd.shutdown()
        mouse.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
