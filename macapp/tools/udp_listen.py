#!/usr/bin/env python3
"""ListenLook 自检用：UDP 监听器。

收 N 个数据报就退出，逐个做**结构校验**（字段名/类型/有限性/符号约定），
把原始文本原样打印出来 —— 只做 JSON 解析、不重新格式化，
这样粘到报告里的就是线上真实的字节。

用法： udp_listen.py [port] [count] [timeout_seconds]
"""
import json, socket, sys, time, math

port = int(sys.argv[1]) if len(sys.argv) > 1 else 8420
count = int(sys.argv[2]) if len(sys.argv) > 2 else 3
timeout = float(sys.argv[3]) if len(sys.argv) > 3 else 15.0

REQUIRED = {"v": int, "src": str, "seq": int, "t": int,
            "yaw": float, "pitch": float, "roll": float, "ok": bool}

s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", port))
s.settimeout(timeout)
print(f"[listener] 已绑定 127.0.0.1:{port}，等 {count} 个数据报（最多 {timeout}s）", flush=True)

got, bad = 0, 0
deadline = time.time() + timeout
while got < count and time.time() < deadline:
    try:
        data, addr = s.recvfrom(4096)
    except socket.timeout:
        break
    got += 1
    raw = data.decode("utf-8", errors="replace")
    print(f"\n--- 数据报 #{got}  来自 {addr[0]}:{addr[1]}  {len(data)} 字节 ---")
    print(raw)                      # ← 原样，未重新格式化
    # 校验
    problems = []
    try:
        o = json.loads(raw)
    except Exception as e:
        problems.append(f"不是合法 JSON: {e}")
        o = None
    if isinstance(o, dict):
        for k, ty in REQUIRED.items():
            if k not in o:
                problems.append(f"缺字段 {k}")
                continue
            v = o[k]
            if ty is float:
                if not isinstance(v, (int, float)) or isinstance(v, bool):
                    problems.append(f"{k} 不是数字: {v!r}")
                elif not math.isfinite(v):
                    problems.append(f"{k} 非有限: {v!r}")
            elif ty is int:
                if not isinstance(v, int) or isinstance(v, bool):
                    problems.append(f"{k} 不是整数: {v!r}")
            elif not isinstance(v, ty):
                problems.append(f"{k} 类型不对: {v!r}")
        if o.get("v") != 1:
            problems.append(f"v 不是 1: {o.get('v')!r}")
        if o.get("src") != "airpods":
            problems.append(f"src 不是 airpods: {o.get('src')!r}")
        if o.get("ok") is False:
            for k in ("yaw", "pitch", "roll"):
                if o.get(k) != 0:
                    problems.append(f"ok:false 时 {k} 必须为 0，实际 {o.get(k)!r}")
    elif o is not None:
        problems.append(f"顶层不是 object: {type(o).__name__}")
    if problems:
        bad += 1
        print("  ✗ 校验失败: " + "; ".join(problems))
    else:
        print("  ✓ 校验通过：字段齐全、类型正确、全部有限、符合 v1 格式")

print(f"\n[listener] 收到 {got} 个数据报，其中 {bad} 个不合格")
sys.exit(0 if got > 0 and bad == 0 else 1)
