#!/usr/bin/env bash
# 构建并运行 ListenLook 传感器端。
#
#   ./run.sh                 # 实时面板（需要真终端）
#   ./run.sh --json          # 每个更新打一行协议 JSON
#   ./run.sh --quiet         # 只发不显示
#   ./run.sh --host 192.168.1.20 --port 8420
#
# ⚠️ **exec 二进制本体，不要用 `open`。** 两条实测结论（来自 ows-earbud-demo）：
#   1. `open -W app --args a b c` 在本机**传不进参数**（argv 里根本没有它们）；
#   2. 直接执行 bundle 内的二进制，TCC **仍然认这个 bundle**（读 Info.plist、
#      认签名身份），不会重新弹窗、也不会被当成"没说明的裸二进制"打死。
# 所以直接 exec：参数能用、stdout/stderr 直连你的终端、权限状态还在。
# 另一个好处是 Ctrl-C 直接进到进程本身，不用跟 `open` 那一层较劲。

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="airpods"
BIN="$ROOT/Out/$TARGET.app/Contents/MacOS/$TARGET"

# 每次都重建。swiftc 编这几个文件只要一两秒，而"跑的是不是当前源码"
# 这种事不值得用增量判断去换 —— 一旦搞错，调试方向会全歪。
"$ROOT/build.sh" >/dev/null

if [ ! -x "$BIN" ]; then
  echo "error: 构建产物不存在：$BIN" >&2
  echo "       先跑 ./build.sh 看完整报错。" >&2
  exit 1
fi

exec "$BIN" "$@"
