#!/usr/bin/env bash
# 构建 ListenLook 的 macOS 传感器端（airpods）。
# 改编自用户自己仓库的 ows-earbud-demo/build.sh —— 那两份本机约束的实测记录
# （模块缓存、裸二进制拿不到 TCC 权限）原样保留在下面。
#
#   ./build.sh            # 构建，产出 Out/airpods.app
#   ./build.sh --clean    # 先清 Out 和模块缓存再构建
#
# 本机两条硬约束（已实测，不要试图绕过）：
#   1. 本机只有 Command Line Tools，**没有 Xcode**。swiftc 默认的模块缓存目录
#      会被沙箱拒绝，报出来的错却是误导性的 "SDK is not supported by the compiler"。
#      必须显式 -module-cache-path 到仓库内可写目录。
#   2. **裸 CLI 二进制拿不到运动权限**。responsible process 上没有
#      NSMotionUsageDescription 时，CoreMotion 不返回错误，直接把进程打死
#      （__TCC_CRASHING_DUE_TO_PRIVACY_VIOLATION__）。所以必须打成 .app bundle
#      并签名 —— 这是本脚本存在的主要理由，不是为了"像个正经 app"。

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="airpods"
OUT="$ROOT/Out"
CACHE="$ROOT/.modulecache"          # ← 必须在仓库内、必须可写
APP="$OUT/$TARGET.app"
BUNDLE_ID="org.listenlook.airpods"

if [ "${1:-}" = "--clean" ]; then
  echo "清理 $OUT 和 $CACHE ..."
  rm -rf "$OUT" "$CACHE"
fi

TARGET_DIR="$ROOT/Sources/$TARGET"
SHARED_DIR="$ROOT/Sources/shared"
MAIN="$TARGET_DIR/main.swift"
PLIST="$TARGET_DIR/Info.plist"

if [ ! -f "$MAIN" ]; then
  echo "error: 找不到 $MAIN" >&2
  exit 1
fi
if [ ! -f "$PLIST" ]; then
  echo "error: 找不到 $PLIST —— 没有 Info.plist 就打不出能拿权限的 bundle" >&2
  exit 1
fi

# ── Info.plist 先校验 ──
# 坏掉的 plist 会让 codesign 报 "Info.plist=not bound"，进而让 TCC 读不到
# NSMotionUsageDescription —— 结果**不是"弹窗被拒"，而是进程被直接打死**，
# 且没有任何可读的错误。实测踩过：把 <string> 手误写成 <key>，
# 权限状态一直卡在 denied，查了很久。
if ! plutil -lint "$PLIST" >/dev/null 2>&1; then
  echo "error: Info.plist 格式非法，先修它：" >&2
  plutil -lint "$PLIST" >&2
  exit 1
fi
# 顺手确认那条要命的键真的在、且非空。这条检查便宜到没有理由不做。
MOTION_DESC="$(/usr/libexec/PlistBuddy -c 'Print :NSMotionUsageDescription' "$PLIST" 2>/dev/null || true)"
if [ -z "$MOTION_DESC" ]; then
  echo "error: Info.plist 里 NSMotionUsageDescription 缺失或为空。" >&2
  echo "       没有它，CoreMotion 会把进程打死而不是返回错误。拒绝构建。" >&2
  exit 1
fi

mkdir -p "$OUT" "$CACHE"

# ── 收集源文件 ──
# Sources/shared/*.swift（HeadTracker / PoseSender / Panel）
# + Sources/<target>/*.swift（main.swift）
# 注意 swiftc 顶层代码只允许出现在 main.swift 里，文件名不能改。
SOURCES=()
while IFS= read -r f; do SOURCES+=("$f"); done < <(find "$SHARED_DIR" -name '*.swift' | sort)
while IFS= read -r f; do SOURCES+=("$f"); done < <(find "$TARGET_DIR" -name '*.swift' | sort)

echo "building $TARGET ...  (${#SOURCES[@]} 个源文件)"
for f in "${SOURCES[@]}"; do echo "  ${f#"$ROOT"/}"; done

# ── 打包 + 编译 ──
# bundle 结构在编译前就建好，直接往 Contents/MacOS/ 里出二进制：
# 这样二进制天生在 bundle 里，不会被拷来拷去改变 TCC 眼里的"身份"。
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"

swiftc \
  -module-cache-path "$CACHE" \
  -O \
  -framework CoreMotion \
  -o "$APP/Contents/MacOS/$TARGET" \
  "${SOURCES[@]}"

cp "$PLIST" "$APP/Contents/Info.plist"
# PkgInfo 不是必须的，但加上无害，且让某些老工具认得这是个 app。
printf 'APPL????' > "$APP/Contents/PkgInfo"

# ── 签名 ──
# 默认 **ad-hoc**（`codesign --sign -`）。看起来"低配"，但对本程序是对的：
#
# 1) 本机只有 Command Line Tools，没有 Xcode，也就没有 Apple 开发者证书。
#    ad-hoc 不需要任何证书，因此也**不需要访问钥匙串** ——
#    而"不需要访问钥匙串"这件事是硬要求，见下。
# 2) TCC 需要的是"Info.plist 被绑定进签名"，ad-hoc 完全做得到
#    （`Info.plist=not bound` 才是致命的，那说明签了个寂寞）。
# 3) 用 `swiftc` 直接产出的二进制自带一个 **linker-signed** 的占位签名
#    （flags=adhoc,linker-signed）。那个签名**不绑定 Info.plist** ——
#    如果我们不重新签一次，TCC 读不到 NSMotionUsageDescription，
#    运行时不是弹窗被拒，而是进程被直接打死。所以这一步不能省。
#
# ⚠️ 实测踩过的坑（浪费了一个下午，写在这里免得再踩）：
#    这台机器上**有**一个可用的 codesigning 身份（别的项目建的）。
#    用它签的时候 codesign 会去取私钥 → 钥匙串是锁的 → **永久挂起**：
#    没有超时、没有报错、也不返回，构建就那么停着。
#    加了 `--options runtime` 更容易触发（它要额外读私钥做 hardened runtime）。
#    结论：构建脚本里**不要**自动使用需要钥匙串的身份。
#
# 想用固定身份（好处：ad-hoc 的 designated requirement 基于 cdhash，
# cdhash 每次重建都变，TCC 授权会对不上，于是重建后要重新点一次权限窗；
# 固定证书的 DR 是 `identifier "..." and certificate root = H"..."`，
# 与二进制内容无关，重建多少次授权都还在）——就显式打开：
#
#     LL_SIGN_ID="我的自签证书名" ./build.sh
#
# 用之前**先解锁钥匙串**，否则会挂：
#     security unlock-keychain ~/Library/Keychains/login.keychain-db
if [ -n "${LL_SIGN_ID:-}" ]; then
  echo
  echo "── 签名（固定身份，LL_SIGN_ID 指定）──"
  echo "  · $LL_SIGN_ID"
  SIGN_ID="$LL_SIGN_ID"
else
  echo
  echo "── 签名（ad-hoc）──"
  echo "  · 想用固定身份避免重建后重新授权：LL_SIGN_ID=\"证书名\" ./build.sh"
  SIGN_ID="-"
fi

# ⚠️ xattr 要在**签名前**清，而且可能要清不止一次。
# codesign 遇到扩展属性会报 "resource fork, Finder information, or similar
# detritus not allowed" 而失败；失败后 Info.plist 不绑定进签名
# （codesign -dv 显示 "Info.plist=not bound"），TCC 就读不到用途说明。
# 为什么一次不够：这台机器的桌面在 iCloud Drive 同步下，文件提供者会往里塞
# com.apple.fileprovider.fpfs#P，而且**清掉之后还会再加**。
# 所以可靠的做法是「清 → 签 → 失败就重试」，而不是清一次了事。
# 给命令加硬超时。macOS 没有 coreutils 的 timeout，所以自己做一个。
#
# 为什么必须有：codesign 取私钥时会去敲钥匙串，钥匙串锁着的话它**弹窗等人输密码**
# —— 在终端里就是永久挂起：不报错、不返回、没有超时。本项目实测踩过一次
# （另一个项目建的 owsdemo 钥匙串，密码只有那个项目知道），用户会莫名其妙被问密码。
# 所以签名这一步永远不许无限期等待：超时 → 明确失败并说清原因。
run_with_timeout() {
  local secs="$1"; shift
  "$@" &
  local pid=$!
  ( sleep "$secs"; kill -9 "$pid" 2>/dev/null ) &
  local watcher=$!
  wait "$pid" 2>/dev/null
  local rc=$?
  kill -9 "$watcher" 2>/dev/null
  wait "$watcher" 2>/dev/null
  return $rc
}

sign_with() {
  local identity="$1"
  local err=""
  for _ in 1 2 3 4; do
    xattr -cr "$APP"
    if err=$(run_with_timeout 20 codesign --force --sign "$identity" --identifier "$BUNDLE_ID" "$APP" 2>&1); then
      xattr -cr "$APP" 2>/dev/null || true
      return 0
    fi
    if [ $? -eq 137 ] || echo "$err" | grep -q "Terminated"; then
      echo "  ⚠️  codesign 超时（20s）—— 几乎肯定是钥匙串锁着在等密码。" >&2
    fi
    sleep 0.4
  done
  [ -n "$err" ] && echo "  codesign($identity) 失败：$err" >&2
  return 1
}

if ! sign_with "$SIGN_ID"; then
  echo "error: 签名失败。bundle 里的二进制会带着 swiftc 的 linker-signed 占位签名，" >&2
  echo "       Info.plist 未绑定 → 运行时会被 TCC 直接打死。拒绝产出这种产物。" >&2
  if [ "$SIGN_ID" != "-" ]; then
    echo "" >&2
    echo "  你指定了固定签名身份 LL_SIGN_ID=\"$SIGN_ID\"。" >&2
    echo "  若它是"别的项目"建的、钥匙串密码你不知道 —— **不要用它**：" >&2
    echo "      unset LL_SIGN_ID && ./build.sh" >&2
    echo "  ad-hoc 签名完全够用，代价只是重建后要重新点一次运动权限窗。" >&2
  fi
  exit 1
fi

# ── 校验产物 ──
# 签名残留：codesign 被中断时会留下 *.cstemp，留在 bundle 里会污染哈希、
# 也会让下一次签名报奇怪的错。顺手清掉。
find "$APP" -name '*.cstemp' -delete 2>/dev/null || true

echo
echo "── 产物 ──"
ls -la "$APP"
echo
ls -la "$APP/Contents/MacOS/$TARGET"
echo
codesign -dv "$APP" 2>&1 | grep -E '^Identifier|^CodeDirectory|^Signature|^Info.plist|^Format' | sed 's/^/  /' || true

# 最后一道硬检查：Info.plist 必须绑定进签名、标识必须是我们给的那个。
# 这两条任一不成立，TCC 都会读不到用途说明 —— 那次运行必然是白跑。
SIGN_DV="$(codesign -dv "$APP" 2>&1)"
if echo "$SIGN_DV" | grep -q 'Info.plist=not bound'; then
  echo "error: Info.plist 没绑定进签名 —— TCC 读不到 NSMotionUsageDescription，" >&2
  echo "       运行时会表现为进程被直接打死（__TCC_CRASHING_DUE_TO_PRIVACY_VIOLATION__）。" >&2
  echo "       修法：./build.sh --clean 重跑一次。" >&2
  exit 1
fi
if ! echo "$SIGN_DV" | grep -q "^Identifier=$BUNDLE_ID$"; then
  echo "error: 签名标识不是 $BUNDLE_ID —— TCC 会把授权记到别的身份下。" >&2
  exit 1
fi
# 真去读一次 Info.plist 里的用途说明，确认 TCC 能看到它。
if ! /usr/libexec/PlistBuddy -c 'Print :NSMotionUsageDescription' \
      "$APP/Contents/Info.plist" >/dev/null 2>&1; then
  echo "error: bundle 内的 Info.plist 读不到 NSMotionUsageDescription。" >&2
  exit 1
fi

echo
echo "built: $APP"
echo "run:   ./run.sh [--json|--quiet|--host <ip>|--port <n>|--hz <n>]"
