# 让电脑"以为"它在跟一个鼠标说话 —— USB HID 桥

这篇文档回答一个很自然的疑问：

> Windows 没有用户态的蓝牙 L2CAP API，那**写个软件伪装成鼠标**行不行？

**先给结论：这个想法对了一半，但它解决的不是被堵住的那一半。** 下面把两半分开讲。

---

## 两个完全不同的问题，别混在一起

| | 方向 | 卡在哪 | "伪装成鼠标"能帮上忙吗 |
|---|---|---|---|
| **① 取数据** | AirPods → 电脑 | AirPods 的姿态走蓝牙经典 **L2CAP 私有通道（PSM `0x1001`）**，且链路加密；**Windows 没有用户态的 L2CAP API** | ❌ 完全帮不上 |
| **② 送输入** | 软件 → 游戏 | 我们目前用 `SendInput` 合成鼠标事件；部分游戏读**原始 HID 输入**，会忽略或标记合成事件 | ✅ 这才是它真正能解决的 |

"伪装成鼠标"属于 ②。它救不了 ① —— 原因很直接：

- 鼠标协议里**根本没有"头部朝向"这个字段**，装了鼠标也传不了姿态；
- AirPods 也**不暴露 HID 描述符**。它的蓝牙服务是 `HFP AVRCP A2DP AACP GATT`
  （macOS 的蓝牙面板里能直接看到），没有 HID。所以"让 Windows 把 AirPods 认成鼠标"
  这件事在协议层就不存在。

**① 的出路只有一条**：换一个能读到姿态的地方 —— Linux（BlueZ 能开那条通道）、
iPhone（`CMHeadphoneMotionManager`）、Android（L2CAP CoC）、或者干脆换传感器（手机 IMU）。
详见 [windows-routes.md](windows-routes.md)。

---

## 但 ② 这条路值得做，而且比现在的方案更硬

把"伪装成鼠标"放在**电脑外面**做：一个 Linux 小盒子（树莓派 Zero 2 W，约 ¥100）：

```
  AirPods ──蓝牙经典──┐
                      │  ① 树莓派用 BlueZ 打开那条私有 L2CAP 通道，读出姿态
                  ┌───┴────────────────────────┐
                  │  树莓派 Zero 2 W            │
                  │  ② 本地跑 ListenLook 的映射  │
                  │  ③ 把自己装成 USB HID 鼠标   │  ← /dev/hidg0
                  └───┬────────────────────────┘
                      │  一条 USB 线（还顺便能当 USB 声卡，把游戏声音转给 AirPods）
                  ┌───┴────────────────────────┐
                  │  Windows 游戏电脑            │
                  │  看到的是：一个普通 USB 鼠标  │  ← 零软件、零驱动、零授权
                  └────────────────────────────┘
```

**这解决两个真问题：**

1. **原始输入（raw input）**。`SendInput` 产生的是合成事件，部分竞技类游戏会忽略它，
   或者干脆把它当成可疑行为；而 USB HID 是**真设备** —— 所有游戏、所有反作弊都当普通鼠标。
2. **电脑端零安装**。不用装 Python、不用给辅助功能权限、不用装驱动。插上就多一个鼠标。

**已有先例**（这条路不是我们发明的）：
- [pihidproxy](https://github.com/arnoldxt/pihidproxy) —— 用 Pi Zero 把蓝牙键鼠桥成 USB HID
- [bluetooth-usb-peripheral-relay](https://github.com/bahaaador/bluetooth-usb-peripheral-relay)

**代价**：需要一块能跑 Linux 的板子 + 一条 USB 数据线。Windows 上不再需要任何软件，
但姿态的读取搬到了板子上。

---

## 怎么在 ListenLook 里用

接收端内置了这个后端（`--output hidg`）：

```bash
# 在树莓派上（先按下一节配好 gadget）
python3 receiver/listenlook.py --output hidg --hidg-device /dev/hidg0 --bind 0.0.0.0
```

同一份映射引擎、同一份配置、同一套调参开关 —— 只是最后一步从"往本机注入"
换成"往 USB 设备节点写 HID 报文"。

### 报文编码里那个必须知道的坑

HID boot 鼠标报文是 4 字节：`按键 / dx / dy / 滚轮`，后三个是**有符号 int8**。
所以**单帧位移上限是 ±127**。

如果把 300px 的位移直接塞进去，它会溢出成 `-212px` —— 表现是
**"快速转头时光标猛地往反方向飞"**，慢速移动时完全正常。这种"只在动得快时出现"的
bug 极难定位，所以后端会把大位移**拆成多帧**发送，并且有专门的测试钉住它
（`tests/test_hidg.py`，用普通文件冒充 `/dev/hidg0`，不需要树莓派就能跑）。

### 树莓派侧的一次性配置（libcomposite）

```bash
# /boot/firmware/config.txt（老系统是 /boot/config.txt）加上：
#   dtoverlay=dwc2
# /boot/firmware/cmdline.txt 的 rootwait 后面加：
#   modules-load=dwc2,libcomposite

# 然后建一个 HID gadget（写进 /etc/rc.local 或做成 systemd 服务）
cd /sys/kernel/config/usb_gadget
mkdir -p listenlook && cd listenlook
echo 0x1d6b > idVendor   # Linux Foundation
echo 0x0104 > idProduct  # 多功能复合设备
mkdir -p strings/0x409
echo "ListenLook" > strings/0x409/manufacturer
echo "Head Tracking Mouse" > strings/0x409/product
mkdir -p functions/hid.usb0
echo 1 > functions/hid.usb0/protocol   # 1 = 鼠标
echo 0 > functions/hid.usb0/subclass
echo 4 > functions/hid.usb0/report_length
# 4 字节相对鼠标报文：按键 / dx / dy / 滚轮
printf '\x05\x01\x09\x02\xa1\x01\x09\x01\xa1\x00\x05\x09\x19\x01\x29\x03\x15\x00\x25\x01\x95\x03\x75\x01\x81\x02\x95\x01\x75\x05\x81\x03\x05\x01\x09\x30\x09\x31\x09\x38\x15\x81\x25\x7f\x75\x08\x95\x03\x81\x06\xc0\xc0' \
  > functions/hid.usb0/report_desc
mkdir -p configs/c.1/strings/0x409
echo "ListenLook" > configs/c.1/strings/0x409/configuration
ln -s functions/hid.usb0 configs/c.1/
ls /sys/class/udc > UDC      # 这一步之后 /dev/hidg0 出现
```

看到 `/dev/hidg0` 就说明电脑那边已经"看到"一个新鼠标了。

---

## 顺便回答："那写个 Windows 驱动行不行？"

**行，但代价不成比例。** 需要把一句话修正一下：

> "Windows 没有 L2CAP" 应改为 **"Windows 没有*用户态*的 L2CAP API"**。

微软的驱动文档里明确有 [Creating a L2CAP Client Connection to a Remote Device](https://learn.microsoft.com/en-us/windows-hardware/drivers/bluetooth/creating-a-l2cap-client-connection-to-a-remote-device)：
内核态的 **L2CAP profile driver** 可以对着指定 PSM 发 `BRB_L2CA_OPEN_CHANNEL`
打开通道。也就是说，能力是存在的，只是**只对内核态驱动开放**。

真要走这条路，得同时扛下：

- 内核态开发（WDF/Bluetooth profile driver，蓝屏风险自负）；
- **驱动签名**（EV 证书 / WHQL，否则用户得开测试模式才能装）；
- 链路加密的密钥处理（AirPods 的姿态只在加密链路上发）。

为一个小工具做这些不划算 —— 同样的问题，一块 ¥100 的树莓派加一条 USB 线就解决了，
而且是**电脑端零安装**。

## 另一条纯软件路线（不买硬件）

USB 蓝牙适配器 + `usbipd-win` 把它直通进 WSL2 或虚拟机，在 Linux 里跑 BlueZ 读姿态，
再通过 localhost 把姿态喂给 Windows 上的接收端。**纯软件，但更折腾**：

- WSL2 的默认内核**没有编蓝牙模块**，要自己编译内核；
- 虚拟机（VirtualBox/VMware）更稳，但更重，且 USB 直通偶尔抽风。

结论：能用，但树莓派那条路花的钱比省下的时间便宜。

---

## 一张表收尾

| 方案 | 电脑端要装东西吗 | 原始输入游戏能识别吗 | 需要买硬件吗 | 姿态从哪来 |
|---|---|---|---|---|
| 本机注入（现在的默认） | 要（Python/辅助功能权限） | ⚠️ 多数可以，少数读 raw input 的不行 | 不要 | AirPods（macOS）或手机 |
| **USB HID 桥（本文档）** | **完全不要** | **✅ 真设备，一定识别** | 要（树莓派 ~¥100） | AirPods（经 BlueZ） |
| Windows 内核驱动 | 要装驱动（需签名）+ 可能开测试模式 | ✅ | 不要 | AirPods（需要自己实现 L2CAP） |
| USB 适配器 + WSL2/虚拟机 | 要（WSL2 + 自编内核 / 虚拟机） | ⚠️ 取决于最后怎么注入 | 要（一个 USB 蓝牙适配器 ~¥30） | AirPods（经 BlueZ） |
