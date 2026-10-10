# ListenLook

**用头部姿态控制鼠标 —— 拿 AirPods 当传感器，用来打游戏。**

ListenLook 把"头部朝向"变成一个鼠标。转头 → 光标移动；歪头 → 滚轮。戴着你本来就在戴的
AirPods，不需要额外硬件，不需要改装。

但它不只服务 AirPods —— 见下面那张真相表：**Windows 读不到 AirPods 的姿态**，
这是协议层的死路，不是没写驱动。所以 ListenLook 把"取姿态"和"用姿态"拆成两层，
任何能测出头朝向的东西都能当输入源，任何电脑都能当接收端。

```
   ┌──────────────────── 传感器源 ────────────────────┐
   │  AirPods (macOS 直连)     手机浏览器 / 手机 app   │
   │  树莓派 + BlueZ 桥        自制的 ESP32 + IMU      │
   └───────────────────────┬──────────────────────────┘
                           │  ListenLook Pose v1（UDP / HTTP，见 PROTOCOL.md）
                           ▼
   ┌──────────────────── 接收端（任意电脑）─────────────┐
   │  映射引擎：死区/增益/曲线/One Euro 平滑/滚轮/按键   │
   │  注入：Windows SendInput · macOS CGEvent · Linux uinput │
   └──────────────────────────────────────────────────┘
```

---

## ⚠️ 先说清楚：哪些平台能怎么用

这张表是整个项目的地基。我把它放在最前面，是因为**大部分同类项目会含糊带过**，
然后用户在 Windows 上折腾一晚上才发现取不到数据。

| 平台 | 直读 AirPods 姿态 | 可行路线 |
|---|---|---|
| **macOS 14+** | ✅ 原生 `CMHeadphoneMotionManager` | 本仓库 `macapp/`，零依赖，体验最好 |
| **Linux** | ✅ BlueZ 能开那条私有通道 | 本机跑桥（或收 UDP） |
| **Windows** | ❌ **协议层走不通** | 只能靠外部传感器源：Mac / 树莓派 / 手机 |
| **Android** | ✅（LibrePods 已证明，需 Shizuku/root） | 同上 |

**为什么 Windows 不行**：AirPods 的姿态走蓝牙经典 L2CAP 的私有 PSM `0x1001`，
且链路加密，而 **Windows 没有用户态的 L2CAP API** —— raw Winsock `BTHPROTO_L2CAP`
连接直接失败。这不是"没人写驱动"，是操作系统没暴露这个能力。
（这条结论有实测支撑：[pods-head-tracker](https://github.com/batubozkan/pods-head-tracker)
的作者试过并记录了失败，他因此改用树莓派做桥。）

**好消息**：既然接受"传感器源可以换"，Windows 玩家的选择反而很多 ——
详见 **[docs/windows-routes.md](docs/windows-routes.md)**，四条路，从零成本到需要一块树莓派。

---

## 快速开始

### A. macOS + AirPods（最省事的一条）

```bash
cd macapp
./build.sh          # 编译 + 打包 .app（不需要 Xcode）

# ⚠️ 第一次必须让采集端"自己"去要权限，否则会被系统直接打死（不报错、不弹窗）：
open -n Out/airpods.app        # 或直接在访达里双击 Out/airpods.app，然后点「允许」
pkill -f "MacOS/airpods"       # 授权完成后先关掉它

# 之后就可以正常在终端里跑：
./run.sh &                     # 采集端：AirPods 姿态 → UDP
python3 ../receiver/listenlook.py   # 接收端：UDP → 鼠标
```

**为什么第一次不能直接在终端里跑**：macOS 把权限算在**责任进程**头上。
在终端/IDE 里 exec 这个二进制时，责任进程是那个宿主，而宿主没有
`NSMotionUsageDescription`，于是权限请求不是"被拒"而是**违反隐私策略直接 abort**。
双击 `Out/airpods.app` 时本程序自己是责任进程，弹窗才会正常出现。
详见 [macapp/README.md](macapp/README.md)。

接收端还需要**辅助功能**权限（见下面"权限"一节）；授权前也能跑，但只是降级模式。

### B. Windows / Linux（手机当传感器）

```bash
# 在要玩游戏的那台电脑上：
python3 receiver/listenlook.py --bind 0.0.0.0
```

它会打印两个地址，任选一条：

- **手机 app 路线（推荐，最稳）**：任何能往 HTTP POST JSON 的 app 都行 ——
  Sensor Logger、phyphox、Tasker、MacroDroid…… 指向
  `http://<电脑IP>:8421/pose`，选一个**朝向/姿态**类传感器（不要只选原始加速度计）。
  这条路不需要证书，也不受浏览器安全上下文限制。
- **浏览器路线**：手机浏览器打开 `https://<电脑IP>:8443/`，点"开始"。
  需要接受自签证书的警告。注意浏览器只在安全上下文下才给陀螺仪数据，
  所以这个页面必须是 HTTPS —— 用 HTTP 打开会静默没有数据。

```bash
# 先在电脑本地确认链路通不通（不开手机也能测）：
curl -X POST http://127.0.0.1:8421/pose -d '10 0 0'
python3 tests/fake_phone.py --seconds 5      # 用合成数据冒充手机
```

### C. 树莓派 / Linux 盒子当桥（AirPods + Windows 的完整方案）

这是"AirPods 在 Windows 上打游戏"唯一**声音和姿态都完整**的路线：
树莓派同时当蓝牙音频接收器和姿态桥，一条 USB 线接 Windows，游戏声音也走它。

本仓库**不包含**这部分实现 —— 现成的 [pods-head-tracker](https://github.com/batubozkan/pods-head-tracker)
（GPL-3.0）已经做好了，把它输出的 UDP 指向 ListenLook 即可，双方不需要共享代码。
细节和接线方式见 [docs/windows-routes.md](docs/windows-routes.md)。

---

## 调参：怎么让它"手感对"

默认参数是能直接用的，但手感是很个人的东西。按这个顺序调：

| 症状 | 改什么 |
|---|---|
| 静止时光标自己飘 | 加大 `--deadzone`（默认 2°，抖到 4~6） |
| 动起来有拖影、跟不上 | 减小 `min_cutoff`… 反之加大；或者 `--no-smooth` 先确认是不是平滑造成的 |
| 转半天光标才挪一点 | 加大 `--gain`（默认 8） |
| 中心附近太灵敏、瞄不准 | 加大 `--curve`（默认 1.5，中心附近更精细） |
| 左右反了 / 上下反了 | `--invert-yaw` / `--invert-pitch` |
| 想要"甩头才动、停住不动" | `--mode rate` |
| 想"偏着一点就一直走" | `--mode relative`（默认） |
| 歪头滚动 | `--wheel roll` |
| 只在按住某个键时生效 | `--activation hold --hold-key right_mouse` |

`--print-config` 打印当前生效配置；配置文件示例见
[receiver/listenlook.config.example.json](receiver/listenlook.config.example.json)。

---

## 权限

### macOS：两个权限，两个不同的坑

| 程序 | 需要的权限 | 关键点 |
|---|---|---|
| `macapp`（采集端） | 运动与健身 | 必须先双击 `.app` 让**它自己**弹窗；在别的宿主里跑会被 TCC 直接打死 |
| `receiver`（接收端） | 辅助功能 | 授权后必须**重启程序**，macOS 不给运行中的进程补发权限 |

没有辅助功能权限时，ListenLook 会**自动降级到 CGWarp 模式**并明确告诉你：
光标会动，但**不产生鼠标移动事件，游戏里不会有反应**。这是实测结论，不是保险起见 ——
`CGEventPost` 在无权限时被系统静默丢弃，而 `CGWarpMouseCursorPosition` 仍然可用
（实测位移精确到 0.8px）。降级模式对"用头点菜单"这类场景够用，对游戏不够。

授权路径：**系统设置 > 隐私与安全性 > 辅助功能** → 把运行本程序的 App
（终端 / iTerm / 你启动它的那个应用）打勾 → **重启程序**。
macOS 不会给已经在运行的进程补发权限，这一步必须重启，否则你会以为授权没生效。

### Windows

一般不需要额外授权。若某些游戏读原始 HID 输入而忽略合成事件，见"已知边界"。

### Linux

`uinput` 需要 `/dev/uinput` 写权限：

```bash
sudo modprobe uinput && sudo usermod -aG input $USER   # 重新登录后生效
```

---

## 已知边界（诚实清单）

- **游戏里的原始输入**：Windows 的 `SendInput` 对绝大多数游戏有效，但少数读原始 HID
  的竞技类游戏可能忽略合成事件。彻底的解法是虚拟 HID 驱动（vJoy / Interception）
  或硬件级注入 —— 那是另一个工程量级，本仓库没做。
- **yaw 会漂**：只用加速度计+陀螺仪时，yaw 没有绝对参考（要磁力计），会缓慢漂移。
  对"转头控制光标"没影响（我们要的就是相对转动量），按一下归零即可。别当 AHRS 用。
- **手机浏览器读不到数据**：先确认页面是 `https://` 打开的。HTTP 下浏览器不给陀螺仪数据，
  而且**不报错** —— 这是最容易被误判成"工具坏了"的坑。
- **多显示器**：光标钳制用的是所有显示器的并集矩形，多屏错位摆放时光标可能"跑到缝里"。
- **协议无加密无认证**：任何能往 UDP 端口发包的人都能控制你的鼠标。默认只监听回环地址，
  跨机必须显式 `--bind 0.0.0.0`，且**只在可信局域网里用**。
- **两个零点**：手机页面自己有"归零"，接收端也有一份零点。接收端在**来源变化或掉线恢复**
  时会自动重新取零点（否则重连后光标会朝一边飘）；页面上的归零只影响页面侧。

---

## 测试

不需要任何硬件：

```bash
python3 tests/test_extract.py     # 解析器 + IMU 融合（18 项）
python3 tests/test_pipeline.py    # 接收端管线：UDP/FreePIE/掉线停手/滚轮/坏帧（12 项）
python3 tests/fake_phone.py --seconds 5   # 冒充手机，压 WebSocket 链路
python3 receiver/listenlook.py --simulate --dry-run   # 合成姿态，不碰鼠标
python3 receiver/listenlook.py --self-test            # 验证注入链路（会轻微移动光标后复位）
```

`--dry-run` 全程不碰鼠标；`--stats 1` 每秒往 stdout 打一行 JSON 统计，方便脚本断言。

---

## 目录结构

```
receiver/listenlook.py    跨平台接收端：映射引擎 + 三平台鼠标注入 + 手机页面 + 摄取端点
                          单文件、零第三方依赖（只用标准库）
macapp/                   macOS 采集端：AirPods → UDP（swiftc 直接编译，不需要 Xcode）
PROTOCOL.md               ListenLook Pose v1 协议（含符号约定、心跳、安全边界）
docs/windows-routes.md    Windows 玩家的四条路
tests/                    管线测试、解析器测试、假手机
```

---

## 路线图

- [ ] 手势映射：点头/摇头 → 按键（协议里已留 `conf` 字段）
- [ ] Windows 单文件 exe（PyInstaller + GitHub Actions，朋友们不用装 Python）
- [ ] 全局热键（Windows `RegisterHotKey` / macOS CGEventTap），现在是按住键轮询
- [ ] Linux 桥的一键安装脚本（BlueZ 那条路）
- [ ] 收尾：把 `--activation hold` 的默认键做成可发现（面板提示当前用哪个键）

---

## 许可与致谢

MIT。

- [pods-head-tracker](https://github.com/batubozkan/pods-head-tracker)（GPL-3.0）：
  证实了"Windows 拿不到 AirPods 姿态、树莓派 + BlueZ 可以"这条路径。**本项目没有使用它的
  任何代码**，只在文档里指路，双方通过 UDP 协议互操作。
- [LibrePods](https://github.com/librepods-org/librepods)：AirPods 私有协议的逆向工作，
  是 Linux/Android 侧一切可能性的基础。
- [OpenTrack](https://github.com/opentrack/opentrack)：头部追踪生态的事实标准，
  本接收端兼容它的 FreePIE UDP 格式，因此可以直接吃它生态里的输出。
- One Euro Filter：Casiez, Roussel & Vogel (CHI 2012)，本项目的平滑算法。
