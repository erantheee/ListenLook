#!/usr/bin/env python3
"""
假手机 —— 用标准库模拟 ListenLook 的手机传感器页面。

用途有两个，都不是"好玩"：

  1. **验证接收端的 WebSocket 链路**（握手、分片、TLS、姿态入库）不需要真手机就能跑，
     所以它可以放在 CI 里；
  2. 手边没有手机、或者想复现某个具体角度时，用它精确喂一个已知姿态 ——
     比如"持续 yaw=+15° 两秒"，来看映射输出是否符合预期。这比用手晃头可靠得多。

    # 先起接收端（另开一个终端）
    python3 receiver/listenlook.py --dry-run --stats 1

    # 再喂 5 秒合成姿态
    python3 tests/fake_phone.py --seconds 5

    # 复现一个固定角度（调试映射曲线时最有用）
    python3 tests/fake_phone.py --fixed-yaw 15 --seconds 3
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import socket
import ssl
import struct
import sys
import time


def handshake(sock: socket.socket, host: str, path: str = "/ws") -> None:
    key = base64.b64encode(os.urandom(16)).decode()
    req = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n"
    )
    sock.sendall(req.encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise SystemExit("握手失败：连接被对端关闭")
        buf += chunk
    head = buf.split(b"\r\n\r\n", 1)[0].decode("latin-1")
    if "101" not in head.split("\r\n")[0]:
        raise SystemExit(f"握手失败：{head.splitlines()[0]}")
    expect = base64.b64encode(
        hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
    ).decode()
    if expect.lower() not in head.lower():
        raise SystemExit("握手失败：Sec-WebSocket-Accept 校验不通过")


def send_text(sock: socket.socket, text: str) -> None:
    payload = text.encode()
    mask = os.urandom(4)
    n = len(payload)
    if n < 126:
        header = bytes([0x81, 0x80 | n])
    elif n < (1 << 16):
        header = bytes([0x81, 0x80 | 126]) + struct.pack(">H", n)
    else:
        header = bytes([0x81, 0x80 | 127]) + struct.pack(">Q", n)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    sock.sendall(header + mask + masked)


def main() -> int:
    ap = argparse.ArgumentParser(description="模拟 ListenLook 手机传感器客户端")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8443)
    ap.add_argument("--seconds", type=float, default=5.0)
    ap.add_argument("--hz", type=float, default=60.0)
    ap.add_argument("--fixed-yaw", type=float, help="固定 yaw 角度（度），不摆动")
    ap.add_argument("--fixed-pitch", type=float, default=0.0)
    ap.add_argument("--plain", action="store_true", help="用 ws:// 而不是 wss://（测试 --no-tls 场景）")
    args = ap.parse_args()

    raw = socket.create_connection((args.host, args.port), timeout=5)
    if args.plain:
        sock = raw
    else:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        # 自签证书：这里就该跳过校验。客户端是测试工具，连的是自己刚起的服务。
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        sock = ctx.wrap_socket(raw, server_hostname=args.host)

    handshake(sock, f"{args.host}:{args.port}")
    print(f"✅ WebSocket 握手成功 → {args.host}:{args.port}")

    t0 = time.perf_counter()
    n = 0
    interval = 1.0 / max(args.hz, 1.0)
    next_t = t0
    try:
        while time.perf_counter() - t0 < args.seconds:
            t = time.perf_counter() - t0
            if args.fixed_yaw is not None:
                yaw, pitch, roll = args.fixed_yaw, args.fixed_pitch, 0.0
            else:
                yaw = 20.0 * __import__("math").sin(t * 0.8)
                pitch = 12.0 * __import__("math").sin(t * 1.3)
                roll = 5.0 * __import__("math").sin(t * 0.4)
            send_text(
                sock,
                json.dumps(
                    {
                        "v": 1, "src": "phone", "ok": True, "seq": n,
                        "t": int(time.time() * 1000),
                        "yaw": round(yaw, 3), "pitch": round(pitch, 3), "roll": round(roll, 3),
                    },
                    ensure_ascii=False,
                ),
            )
            n += 1
            if n % 30 == 0:
                print(f"  已发送 {n} 帧  yaw={yaw:+6.1f} pitch={pitch:+6.1f}")
            next_t += interval
            sleep = next_t - time.perf_counter()
            if sleep > 0:
                time.sleep(sleep)
    except KeyboardInterrupt:
        pass
    finally:
        sock.close()
    print(f"✅ 共发送 {n} 帧，{n / max(args.seconds, 1e-6):.1f} Hz")
    return 0


if __name__ == "__main__":
    sys.exit(main())
