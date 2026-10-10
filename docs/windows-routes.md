# Windows 玩家的四条路

这篇文档只回答一个问题：**我朋友有 AirPods（或 AirPods Pro）加一台 Windows 电脑，
怎么让他用上 ListenLook？**

先说结论，免得白折腾：

> **Windows 读不到 AirPods 的姿态数据，这条堵死在协议层，任何软件都绕不过去。**

AirPods 的姿态走蓝牙经典 **L2CAP 私有通道 PSM `0x1001`**，而且链路是**加密**的。
Windows 没有用户态的 L2CAP API —— 用 raw Winsock 直接连 `BTHPROTO_L2CAP` 会失败。
这不是"驱动没写"，是系统没暴露这个能力。

（这不是我的推测：[pods-head-tracker](https://github.com/batubozkan/pods-head-tracker)
的作者做过同样的实验、得到同样的失败，并因此改用树莓派做桥。）

所以出路只有一条逻辑：**换一个能测姿态的东西当传感器**。下面是四条，按"成本从低到高"排。

---

## 路线 1：手机当传感器（零成本，今天就能玩）

**需要什么**：一部安卓或 iPhone。**不需要**额外买任何东西。
AirPods 继续连着 Windows 电脑放游戏声音 —— 它在这条路线里只负责音频，不负责姿态。

**为什么可行**：任何手机都有陀螺仪。让手机把姿态推给电脑，电脑这边只负责映射和注入鼠标。

**怎么做**：

```powershell
# 在 Windows 上。两种方式二选一：

# ① 不想装 Python：直接下载 CI 打包好的单文件 exe（推荐）
#    https://github.com/erantheee/ListenLook/actions/workflows/ci.yml
#    最新一次成功的 run → Artifacts → ListenLook-windows-exe
#    或者看 Releases 页（打了 tag 的版本会附上去）
.\ListenLook.exe --bind 0.0.0.0

# ② 已经装了 Python：
python receiver/listenlook.py --bind 0.0.0.0
```

然后二选一：

**(a) 手机 app 推 HTTP（推荐，最稳）**

装任意一个能推传感器数据的 app —— Sensor Logger、phyphox、Tasker、MacroDroid 都行 ——
把目标设成：

```
POST http://<电脑IP>:8421/pose
```

选**朝向 / 姿态 / Device Motion** 类的传感器。这条路：
- 不需要证书（原生 app 不受浏览器"安全上下文"限制）；
- 不需要在我们这边写任何适配 —— 接收端会递归扫描 JSON、按字段别名识别，
  必要时自己用加速度计+陀螺仪做互补滤波融合；
- 每次请求的响应会告诉你**它把你的字段理解成了什么**，例如：

```json
{"ok": true, "understood_as": "加速度计+陀螺仪互补滤波", "yaw": 23.4, "pitch": 0.0, "roll": 0.0}
```

如果 `understood_as` 是"没找到可用字段"，把 app 换成推**姿态/朝向**而不是纯加速度计。

**(b) 手机浏览器打开页面**

`https://<电脑IP>:8443/` —— 注意必须是 **HTTPS**。浏览器只在安全上下文里给陀螺仪数据，
而且用 HTTP 打开时**不报错、就是没数据**，这个坑非常容易被误判成"工具坏了"。
自签证书会弹警告，选"继续访问"。少数浏览器在证书不受信时仍会拒绝提供传感器数据，
那就退回 (a)。

**缺点**：头上要绑个手机（发带、帽子里、魔术贴都行）。以及手机页面切到后台会停止上报 ——
这是浏览器限制，不是 bug。

**延迟**：局域网内手机 → 电脑约 2–10 ms，比想象中好。

---

## 路线 2：iPhone 当桥（如果你朋友有 iPhone）

iPhone 上 `CMHeadphoneMotionManager` 是**可用**的（iOS 14+），也就是说 iPhone 能像 Mac 一样
直接读到 AirPods 的姿态。所以理论上：iPhone 读姿态 → Wi-Fi 推给 Windows → 电脑映射成鼠标。

**目前的状态**：这条路的**发送端还没写**（需要一个 iOS app，而本机无法编译/测试 iOS）。
接收端已经就绪 —— 任何能按协议推 UDP/HTTP 的东西它都收。

**要先验证的关键问题**：AirPods 能否**同时**保持"音频连 Windows"和"姿态连 iPhone"。
AirPods 支持维持多条链路（Apple 的多设备自动切换就依赖这一点），但"姿态链路在 iPhone、
音频链路在 PC"这个组合需要实测。如果不能，代价就是游戏声音得从电脑音箱出，
AirPods 只当传感器 —— 体验打折，但能用。

**优先级**：中。它有明确的用户群（iPhone + Windows 是很常见的组合），
但存在一个未验证的前提，动手前应该先花十分钟在朋友的设备上确认。

---

## 路线 3：树莓派 / 任意 Linux 盒子当桥（唯一"完整可玩"的方案）

这是 AirPods + Windows 组合的**正解**：Linux 有 BlueZ，能开那条 Windows 开不了的私有通道。

**需要什么**：一块树莓派（Zero 2 W 就够）或任何带蓝牙的 Linux 机器。

**好处不止姿态**：pods-head-tracker 这个项目把树莓派同时做成了 **USB 声卡** ——
Windows 把游戏声音输出到这个"声卡"，树莓派再通过 A2DP 转发给 AirPods。
**一条 USB 线同时传姿态和游戏声音**，这才是完整的游戏体验。

**怎么做**：

1. 按 [pods-head-tracker](https://github.com/batubozkan/pods-head-tracker) 的说明装好树莓派
   （它用 Python 标准库 + BlueZ，不需要编译）。
2. 把它输出的 UDP 目标指向跑着 ListenLook 的 Windows 电脑的 `8420` 端口。
   ListenLook 兼容它的 **FreePIE UDP** 格式（48 字节 = 6 个 little-endian float64），
   会自动识别，不需要配任何东西。
3. Windows 上 `python3 receiver/listenlook.py --bind 0.0.0.0`。

**许可说明**：pods-head-tracker 是 GPL-3.0，ListenLook 是 MIT。
**两者不共享任何代码** —— 它们通过 UDP 协议通信，属于不同程序的互操作，
不构成衍生作品。本项目只在文档里指路，没有复制它一行代码。

**成本**：树莓派 Zero 2 W 约 ¥100–150，加一张卡和线，约 ¥200。

---

## 路线 4：不用 AirPods，用别的东西测姿态

如果你只是想在 Windows 上玩头部追踪，而不是"非要用 AirPods"，选择反而更多：

| 传感器 | 说明 |
|---|---|
| 手机 | 见路线 1，零成本 |
| 自制硬件 | ESP32 + MPU6050/BNO085，约 ¥30。推原始 accel+gyro 到 `POST /pose` 即可 —— 接收端会自己做互补滤波 |
| Webcam | OpenTrack 的 neuralnet tracker 用普通摄像头做面部追踪，零额外硬件。它输出 FreePIE UDP，可以直接喂给 ListenLook |
| 现成头部追踪器 | 任何输出 FreePIE UDP 的设备/软件都能直接接上 |

---

## 四条路对比

| 路线 | 成本 | 姿态从哪来 | 游戏声音 | 验证状态 |
|---|---|---|---|---|
| 1 手机当传感器 | ¥0 | 手机陀螺仪 | AirPods 连 PC ✅ | ✅ 接收端已验证，缺真机联调 |
| 2 iPhone 当桥 | ¥0 | AirPods（经 iPhone） | 待验证 | ⚠️ 发送端未实现 |
| 3 树莓派桥 | ~¥200 | AirPods（经 BlueZ） | 一条 USB 线全搞定 ✅ | ✅ 方案已被他人验证 |
| 4 换传感器 | ¥0–30 | 手机 / ESP32 / 摄像头 | 随意 | ✅ 协议已就绪 |

**给"我朋友有 AirPods + Windows"这个具体问题的建议**：
先走**路线 1**（今天就能玩，零成本），如果想要"戴 AirPods 就能用、不用绑手机"的完整体验，
再上**路线 3**。
