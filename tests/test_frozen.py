#!/usr/bin/env python3
"""
打包（PyInstaller 单文件）后的路径逻辑测试。

    python3 tests/test_frozen.py

为什么必须测：打成单文件 exe 之后 `__file__` 指向一个**临时解包目录**，
每次运行都可能被清掉、路径也和源码布局不同。这类 bug 的典型症状是
"源码跑得好好的，exe 一跑就找不到兜底证书 → 手机页面整条路不可用" ——
而且只在打包后才出现，本地开发永远碰不到。

这里用 monkeypatch 模拟冻结环境的三个特征：sys.frozen / sys._MEIPASS / 无 openssl。
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location(
    "listenlook", os.path.join(ROOT, "receiver", "listenlook.py")
)
ll = importlib.util.module_from_spec(spec)
sys.modules["listenlook"] = ll
spec.loader.exec_module(ll)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'\u2705' if cond else '\u274c'} {name}" + (f"   {detail}" if detail else ""))


def test_normal_layout():
    print("\n[1] 源码布局：证书就在 receiver/certs 里")
    found = ll._find_fallback_cert()
    check("找到了兜底证书", found is not None, str(found))
    if found:
        check("是 crt + key 一对", found[0].endswith(".crt") and found[1].endswith(".key"))


def test_frozen_layout():
    print("\n[2] 冻结布局：证书在 _MEIPASS 里，且 app_dir 必须指向用户目录")
    tmp = tempfile.mkdtemp()
    try:
        packed = os.path.join(tmp, "certs")
        os.makedirs(packed)
        for n in ("dev-fallback.crt", "dev-fallback.key"):
            shutil.copy(os.path.join(ROOT, "receiver", "certs", n), os.path.join(packed, n))

        old_frozen = getattr(sys, "frozen", None)
        old_meipass = getattr(sys, "_MEIPASS", None)
        old_home = os.environ.get("HOME")
        old_profile = os.environ.get("USERPROFILE")
        home = os.path.join(tmp, "home")
        os.makedirs(home)
        try:
            sys.frozen = True           # type: ignore[attr-defined]
            sys._MEIPASS = tmp          # type: ignore[attr-defined]
            # Windows 的 expanduser 认 USERPROFILE、不认 HOME，两个都设才跨平台
            os.environ["HOME"] = home
            os.environ["USERPROFILE"] = home
            check("app_dir 指向用户目录而不是临时解包目录",
                  ll.app_dir().startswith(home), ll.app_dir())
            check("bundled_dir 指向解包目录", ll.bundled_dir() == tmp)
            found = ll._find_fallback_cert()
            check("在 _MEIPASS/certs 里找到兜底证书", found is not None, str(found))
        finally:
            if old_frozen is None:
                del sys.frozen            # type: ignore[attr-defined]
            else:
                sys.frozen = old_frozen   # type: ignore[attr-defined]
            if old_meipass is None:
                del sys._MEIPASS          # type: ignore[attr-defined]
            else:
                sys._MEIPASS = old_meipass  # type: ignore[attr-defined]
            if old_home is not None:
                os.environ["HOME"] = old_home
            if old_profile is not None:
                os.environ["USERPROFILE"] = old_profile
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_no_openssl_uses_fallback():
    print("\n[3] 没有 openssl 时必须退回兜底证书，而不是直接放弃")
    tmp = tempfile.mkdtemp()
    try:
        certdir = os.path.join(tmp, "certs")
        old_path = os.environ.get("PATH")
        os.environ["PATH"] = "/nonexistent"
        try:
            crt, key = ll.ensure_cert(certdir, "127.0.0.1")
            check("拿到了可用的证书路径", os.path.exists(crt) and os.path.exists(key),
                  os.path.basename(crt))
        finally:
            if old_path is not None:
                os.environ["PATH"] = old_path
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    print("ListenLook 打包路径测试")
    test_normal_layout()
    test_frozen_layout()
    test_no_openssl_uses_fallback()
    print(f"\n{'=' * 58}\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        for f in FAIL:
            print(f"  \u274c {f}")
        return 1
    print("\u2705 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
