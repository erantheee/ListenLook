# ListenLook AirPods 采集端（macOS）

把 AirPods 的头部姿态以 **ListenLook Pose v1**（UDP JSON，见 [../PROTOCOL.md](../PROTOCOL.md)）发出去。

**这个程序只采集、只发送。** 鼠标映射与注入在 [../receiver/listenlook.py](../receiver/listenlook.py) 里 ——
特意这么分：AirPods 的姿态只有 macOS 拿得到，而"怎么用这个姿态"所有平台都需要，
两边通过 UDP 协议解耦，谁都不用为对方改代码。

```bash
./build.sh          # 编译 + 打包成 .app + 签名（不需要 Xcode）
./run.sh --json     # 构建并运行，每个更新打一行协议 JSON
```

---

## ⚠️ 第一次运行：授权流程有讲究，弄错会被系统直接打死

这是本项目最容易卡住的一步，而且失败方式极具误导性，所以写在最前面。

**现象**：进程不是报错、不是弹窗被拒，而是**直接被 kill**，stderr 什么都不打。
崩溃报告里只有一行线索：`responsibleProc: <某个宿主 App 的名字>`。

**原因**：macOS 的 TCC 权限归属于**责任进程（responsible process）**，
而不是你实际执行的那个二进制。如果你是在某个宿主 App（终端、IDE、某个 Electron 应用）
里 `exec` 了这个 bundle 里的二进制，TCC 会把责任算到**那个宿主**头上，
然后去读**宿主的** `Info.plist` —— 宿主没有 `NSMotionUsageDescription`，
于是权限请求不是"被拒绝"，而是**违反隐私策略直接 abort**。

**正确顺序**：

1. **第一次**：在访达里双击 `Out/airpods.app`，或者

   ```bash
   open -n Out/airpods.app
   ```

   这时**本程序自己是责任进程**，它带着 `NSMotionUsageDescription`，
   系统会正常弹出「想要访问运动与健身数据」→ 点 **允许**。

2. **之后**：日常也用 `open -n Out/airpods.app` 或双击启动。

   **不要指望在终端里 `./run.sh` 能采到数据** —— 这一条是实测踩出来的：
   终端启动时 TCC 把**责任进程**算成终端，而终端没有 `NSMotionUsageDescription`，
   于是 `authorizationStatus` 是 `notDetermined`，本程序会拒绝硬闯 CoreMotion
   （硬闯 = 被系统静默打死，更难查），改为按协议发 `ok:false` 心跳。
   接收端这时会显示「◍ 传感器无数据」。
   `./run.sh` 依然用于构建、`--help`、以及不受权限约束的调试场景。

3. 检查/修改授权：**系统设置 > 隐私与安全性 > 运动与健身**。

程序会自己检测这个状态并在面板上说明，**不会**在你从错误的宿主启动时硬闯 TCC
（硬闯的结果就是被静默打死，那样更难查）。它也会每秒复查一次权限 ——
你运行中途去系统设置里打开开关，面板会自己变过来，不用重启。

**从 `open` 启动时怎么退出**（没有终端可按键）：

```bash
pkill -f "MacOS/airpods"
```

---

## 选项

| 选项 | 说明 |
|---|---|
| `--host <ip>` | 目标地址，默认 `127.0.0.1`（只喂本机；跨机填接收端的局域网 IP） |
| `--port <n>` | 目标端口，默认 `8420` |
| `--json` | 每个更新打印一行协议 JSON（喂管道/日志用） |
| `--quiet` | 不画面板、不打印姿态 |
| `--hz <n>` | 发送速率上限，默认 `0` = 跟随 CoreMotion 回调（不额外限速） |
| `--out <path>` | 把 JSON 行写进文件 —— 用 `open` 启动时看不到 stdout，靠这个留证据 |
| `--seconds <n>` | 跑够 n 秒自动退出，默认 `0` = 一直跑（自检/脚本用） |
| `--demo` | 演示模式：发**合成姿态**，完全不碰 CoreMotion、不需要任何权限 |

`--demo` 有两个用途：没有 AirPods 的人可以先用它看清这个工具长什么样、会发什么数据；
CI 里也靠它验证面板渲染与发送链路（runner 上既没有 AirPods 也没法授予运动权限）。
它发出的帧带 `ok:true`，但设备名会标成「合成姿态（--demo，非实测）」，面板上也会自曝身份 ——
不会让人误以为看到了真实传感器数据。

```bash
./run.sh --demo                 # 看面板（八字形合成姿态）
./run.sh --demo --json          # 看它会发出什么协议帧
```

面板模式下：`r` = 归零（当前朝向当正前方），`q` = 退出。
stdout 不是终端时会自动转成 `--json` 行为，不会把 ANSI 转义写进你的日志管道。

## 符号约定怎么核对

协议规定 `yaw` 正值 = 头向**右**转，而 CoreMotion 的原始 yaw 正值是**向左**，
所以代码里翻了一次符号。这个翻法如果错了，表现是"转头时光标往反方向跑"，
而且很容易被误当成"用户在接收端配错了反向开关"。

核对方法：

```bash
LL_DEBUG_RAW=1 ./run.sh
```

面板会多显示一行 **CoreMotion 原始值（未翻符号）**。戴上耳机向右转，
看 raw yaw 是正还是负，就知道翻得对不对 —— 不用猜。

## 实现上的几条硬约束（都实测踩过）

- **只有 Command Line Tools，没有 Xcode**：`swiftc` 必须显式指定
  `-module-cache-path` 到可写目录，否则报一串误导性的 "SDK is not supported by the compiler"。
- **签名必须绑定 Info.plist**：`swiftc` 产出的二进制自带 linker-signed 占位签名，
  它**不绑定** `Info.plist`，TCC 读不到用途说明。所以每次构建都要重新签一次。
  `build.sh` 会核对 `codesign -dv` 的输出，`Info.plist=not bound` 就是签了个寂寞。
- **桌面在 iCloud Drive 同步下**：`xattr` 清掉之后会被文件提供者重新加上，
  导致 `codesign` 报 "resource fork, Finder information, or similar detritus not allowed"。
  所以是"清 → 签 → 失败重试"，不是清一次了事。
- **签名永远不许无限期等待**：`codesign` 取私钥时会敲钥匙串，钥匙串锁着就**弹窗等人输密码**，
  在终端里表现为永久挂起（不报错、不返回、没超时）。`build.sh` 给签名加了 20 秒硬超时，
  超时立刻失败并说清原因 —— 而不是让整个构建停在那儿等人来输一个他可能根本不知道的密码。

## 签名方式：默认 ad-hoc

`build.sh` 默认用 **ad-hoc**（`codesign --sign -`）。它不需要任何证书、**不访问钥匙串**，
而 TCC 需要的"Info.plist 被绑定"它完全做得到。

代价：ad-hoc 签名的 designated requirement 基于 cdhash，**每次重建 cdhash 都变**，
TCC 会对不上，于是重建后可能要点一次权限窗。

想避免这一点就建一个固定签名身份（DR 变成 `identifier + certificate root`，与二进制内容无关）：

```bash
LL_SIGN_ID="我的自签证书名" ./build.sh     # 用之前先解锁该钥匙串
```

⚠️ **不要用别的项目建的签名身份** —— 它的钥匙串密码你不知道，`codesign` 会挂在那里等密码。
本项目不依赖任何外部钥匙串。

## 相关工具

```bash
python3 tools/udp_listen.py      # 只监听 8420，把收到的数据报原样打出来（不依赖接收端）
```
