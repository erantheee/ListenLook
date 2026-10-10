#!/usr/bin/env python3
"""
面板验证。

    python3 tests/test_panel.py

分两层，因为这两层的失败方式完全不同：

  ① **渲染逻辑**（本文件主体）：条形图算得对不对、数值格式对不对、
     超量程会不会画出框外、NaN 会不会把面板打崩。与 TTY 无关，任何环境都能测。

  ② **TTY 守门**（PTY 那段）：两个程序都写成"不是终端就不画面板"，
     免得把 ANSI 控制码灌进日志管道。这条守门逻辑需要真 PTY 才能验证。

⚠️ 某些沙箱环境禁掉了 PTY（`openpty: Operation not permitted`）。
这时第 ② 层会**明确跳过**并打印原因 —— 跳过就是跳过，不会伪装成通过。
CI（GitHub Actions）和本机终端里都能正常跑到第 ② 层。
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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


def render(pose, fresh=True, dx=0.0, dy=0.0, rate=60.0, note="") -> str:
    """把面板输出抓成字符串。enabled 手动打开，绕开 isatty（那是第 ② 层的事）。"""
    panel = ll.Panel(ll.Config(), ll.DryRunMouse(), on=True)
    panel.enabled = True
    panel._last = 0.0  # 关掉 10Hz 节流，保证这次一定画
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        panel.line(pose, fresh, dx, dy, rate, note)
    return buf.getvalue()


def test_renders_all_axes() -> None:
    print("\n[1] 三轴都画出来，且带条形图")
    out = render(ll.Pose(yaw=12.5, pitch=-8.0, roll=3.0, ok=True, src="airpods"),
                 dx=120, dy=-40, rate=58.3)
    check("有 yaw", "yaw" in out)
    check("有 pitch", "pitch" in out)
    check("有 roll", "roll" in out)
    check("有块字符（画的是条形图，没退化成一行日志）", "█" in out)
    check("有中心刻度", "|" in out)
    check("数值带正负号", "+12.5" in out and "-8.0" in out, out.strip()[:70])
    check("显示速率", "58.3Hz" in out)
    check("显示注入增量", "+120" in out and "-40" in out)


def test_state_indicators() -> None:
    print("\n[2] 在线 / 掉线 / 开关状态必须一眼看出")
    online = render(ll.Pose(yaw=1, ok=True, src="airpods"))
    offline = render(ll.Pose(ok=False, src="-"), fresh=False)
    check("在线有明确标记", "在线" in online)
    check("掉线有明确标记", "掉线" in offline)
    check("掉线时说明已停止输出", "停止输出" in offline or "等数据" in offline,
          offline.strip()[-36:])
    check("显示来源名", "airpods" in online)


def test_extremes_do_not_overflow() -> None:
    print("\n[3] 超量程不得画出框外（角度打满时最容易露馅）")
    for yaw in (0.0, 44.9, 45.0, 180.0, -180.0, 1e6):
        out = render(ll.Pose(yaw=yaw, pitch=0, roll=0, ok=True, src="t"))
        bars = re.findall(r"[·|█]+", out)
        widths = [len(b) for b in bars]
        check(f"yaw={yaw:g} 条形图不超框", all(w <= 11 for w in widths), f"宽度={widths[:4]}")


def test_nan_does_not_crash() -> None:
    print("\n[4] 非有限值不得把面板打崩")
    for bad in (float("nan"), float("inf"), float("-inf")):
        try:
            out = render(ll.Pose(yaw=bad, pitch=bad, roll=bad, ok=True, src="t"))
            ok, detail = True, f"输出 {len(out)} 字节"
        except Exception as e:  # noqa: BLE001
            ok, detail = False, f"{type(e).__name__}: {e}"
        check(f"{bad} 不抛异常", ok, detail)


def test_zero_pose() -> None:
    print("\n[5] 全零姿态（正前方静止）")
    out = render(ll.Pose(yaw=0.0, pitch=0.0, roll=0.0, ok=True, src="t"), dx=0, dy=0)
    check("正常渲染", "yaw" in out and "+0.0" in out)


def test_pty_layer() -> str:
    print("\n[6] TTY 守门层（需要真 PTY）")
    try:
        import pty as pty_mod
        mfd, sfd = pty_mod.openpty()
    except (OSError, ImportError) as e:
        # Windows 没有 pty 模块（抛 ImportError），沙箱里可能禁掉 PTY（抛 OSError）。
        # 两种情况都是"这层测不了"，不是代码坏了。
        print(f"  ⏭  本环境无法分配 PTY（{getattr(e, 'strerror', None) or e}）—— 跳过这一层。")
        print("     这不是代码问题：CI 与真实终端里会正常执行。")
        return "skip"

    import select
    import signal
    import subprocess
    import time

    proc = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "receiver", "listenlook.py"),
         "--simulate", "--dry-run", "--no-web"],
        cwd=ROOT, stdin=sfd, stdout=sfd, stderr=sfd, close_fds=True,
    )
    os.close(sfd)
    out = b""
    t0 = time.time()
    while time.time() - t0 < 3.0 and proc.poll() is None:
        r, _, _ = select.select([mfd], [], [], 0.2)
        if r:
            try:
                out += os.read(mfd, 65536)
            except OSError:
                break
    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
    os.close(mfd)
    text = out.decode("utf-8", "replace")
    check("TTY 下面板确实画出来了", all(k in text for k in ("yaw", "pitch", "roll")),
          f"捕获 {len(text)} 字节")
    check("TTY 下画的是条形图", "█" in text)
    return "ran"


def main() -> int:
    print("ListenLook 面板验证")
    test_renders_all_axes()
    test_state_indicators()
    test_extremes_do_not_overflow()
    test_nan_does_not_crash()
    test_zero_pose()
    layer2 = test_pty_layer()
    print(f"\n{'=' * 58}\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项"
          + ("（PTY 层已跳过）" if layer2 == "skip" else ""))
    if FAIL:
        for f in FAIL:
            print(f"  ❌ {f}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
