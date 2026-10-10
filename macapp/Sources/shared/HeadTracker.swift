// ListenLook —— AirPods 头部朝向采集（macOS 传感器端）
//
// 这份文件改编自用户自己仓库里的 ows-earbud-demo/Sources/shared/HeadTracker.swift。
// 保留了它最值钱的两样东西：
//   1. TCC 硬化逻辑（权限预检 + wrongLauncher 判定）—— 见下面 start() 上面那段注解；
//   2. 踩过的坑的原始记录（NaN 首帧、responsible process、__TCC_CRASHING_…）。
// 丢掉了 HeadPlane（12 扇区钟面那套是 demo 的展示需求，协议里没有它）。
//
// ═══ 为什么只有 AirPods 能用 ═══
// `CMHeadphoneMotionManager` 是 Apple 的私有通道，**只对 AirPods（及 Beats）开放**。
// 第三方蓝牙耳夹（小米耳夹、华为 FreeClip、Bose Ultra Open）在标准 Bluetooth
// profile 上不暴露任何 IMU 数据。这是实测确认过的，不是猜测。
// 耳夹 / 安卓 / Windows 的头部姿态由别的 src（phone / bridge）提供，与本文件无关 ——
// 这正是 ListenLook 把"取数"和"用数"拆开的原因。
//
// ═══ 本文件在 ListenLook 里的角色 ═══
//   AirPods IMU → HeadTracker（本文件，产出**协议符号约定**下的相对角度）
//              → PoseSender（UDP JSON）
//              → 别人的 receiver（映射 + 鼠标注入，不在本仓库）
// 本文件**不碰鼠标、不碰键鼠事件、不发声、不碰麦克风/摄像头**。纯输入。
//
// ═══ 失败模式 ═══
// AirPods 没连、没戴、或电量极低 → `isDeviceMotionAvailable == false`。
// 这时**不允许静默降级、也不允许退出**：上层继续以 ≤4 Hz 发 `ok:false`，
// 面板显示"未检测到 AirPods"。接收端靠 ok 位区分"玩家没动"和"传感器挂了"。

import Foundation
import CoreMotion

/// 一帧头部姿态。角度单位：度。
///
/// 符号约定 = **ListenLook Pose v1 的线格式约定**（PROTOCOL.md 那张表），
/// 不是 CoreMotion 的原始约定。翻符号在 `handleMotion` 里做，理由写在那儿。
public struct PoseReading {
    /// 偏航：正值 = 头向**右**转
    public var yawDeg: Double = 0
    /// 俯仰：正值 = **抬头**向上
    public var pitchDeg: Double = 0
    /// 翻滚：正值 = 头向**右**歪
    public var rollDeg: Double = 0
    /// 本帧姿态是否有效（false 时上面三个角度必须当作 0）
    public var available: Bool = false
    /// 数据源的人类可读名字，给面板显示用
    public var deviceName: String = ""
    public init() {}
}

public final class HeadTracker {

    // MARK: - 权限预检的结论

    /// 权限预检的结论，供调用方在面板/日志里显示。
    public enum StartResult {
        case ok                 // 已授权并开始接收
        case noDevice           // 没检测到 AirPods（或没戴）
        case denied             // 用户拒绝过 / 被 MDM 策略限制
        case wrongLauncher      // 没授权，且当前进程无法弹窗（启动方式不对）
    }

    /// 数据来源/授权状态的粗分类，用来决定面板说什么、以及要不要发 ok:false。
    public enum Status: Equatable {
        case starting           // 还没 start()
        case streaming          // 正在收数据
        case noDevice           // 授权没问题，但设备不在
        case denied             // 权限被拒
        case wrongLauncher      // 权限没定，但本进程弹不出窗
        case failed             // 其它错误，描述在 statusMessage 里
    }

    // MARK: - 状态

    private let manager = CMHeadphoneMotionManager()
    private let stateLock = NSLock()
    private var _status: Status = .starting
    private var _message: String = "（尚未启动）"
    private var _reading = PoseReading()

    // 起始/归零基准姿态。CoreMotion 的 callback 在后台队列，
    // recenter() 可能从主线程调 —— 用独立的锁保护，不要和 stateLock 混用（会嵌套）。
    private let refLock = NSLock()
    private var referenceAttitude: CMAttitude?

    private var started = false

    /// 采样回调。**注意：在 CoreMotion 的后台队列上被调用**，不是主线程。
    /// 上层若要在里面更新 UI，自己回主线程。
    /// 回调参数已经过有限性检查，可以直接用。
    public var onSample: ((PoseReading) -> Void)?

    public init() {}

    /// CoreMotion **原始**角度（未翻符号、未减参考姿态），只在 LL_DEBUG_RAW=1 时
    /// 被读出来显示在面板上。存在的唯一理由：符号约定是**推导**出来的
    /// （见 handleMotion 里那段长注释），必须留一条能现场实测核对的通道 ——
    /// 戴上耳机向右转，看 raw yaw 的符号，就知道推导对不对。
    private var _raw = (yaw: Double.nan, pitch: Double.nan, roll: Double.nan, at: 0.0)
    public var rawDebug: String {
        stateLock.lock()
        let (y, p, r, at) = (_raw.yaw, _raw.pitch, _raw.roll, _raw.at)
        stateLock.unlock()
        guard y.isFinite, p.isFinite, r.isFinite else { return "CoreMotion raw: (还没有有效帧)" }
        let age = at > 0 ? Date().timeIntervalSince1970 - at : 0
        // 这里可以放心用 String(format:)：上面已经挡掉非有限值。
        return String(format: "CoreMotion raw (未翻符号, 绝对值): yaw %+.1f  pitch %+.1f  roll %+.1f   [%.1fs 前]",
                      y, p, r, max(0, age))
    }

    // MARK: - 只读查询

    public var status: Status { stateLock.lock(); defer { stateLock.unlock() }; return _status }
    public var statusMessage: String { stateLock.lock(); defer { stateLock.unlock() }; return _message }
    public var reading: PoseReading { stateLock.lock(); defer { stateLock.unlock() }; return _reading }

    /// 设备是否支持并已连接。**注意它不代表有权限** —— 权限要看 authorizationStatus。
    public var isDeviceAvailable: Bool { manager.isDeviceMotionAvailable }

    /// 供面板显示的权限状态文字（中英）。
    public static func authorizationDescription() -> String {
        switch CMHeadphoneMotionManager.authorizationStatus() {
        case .authorized:    return "已授权（authorized）"
        case .denied:        return "已拒绝（denied）"
        case .restricted:    return "被策略限制（restricted）"
        case .notDetermined: return "未决定（notDetermined）"
        @unknown default:    return "未知（unknown）"
        }
    }

    // MARK: - 启动

    /// ⚠️ **预检权限**，不要在未授权时直接碰 CoreMotion。
    ///
    /// 踩过的坑（原样保留，这是这个文件存在的主要理由）：
    /// 从别的进程树里（比如 IDE / 终端 / 某个宿主 app）直接执行 bundle 内的二进制时，
    /// TCC 把 responsible process 算成**那个宿主**。宿主没有 NSMotionUsageDescription
    /// → 请求权限时不是弹窗、不是返回错误，而是
    /// **`__TCC_CRASHING_DUE_TO_PRIVACY_VIOLATION__` 直接 abort**。
    /// 崩溃报告里只有一行线索：`responsibleProc: <宿主的名字>`。
    ///
    /// 所以：先查 authorizationStatus。
    ///   · .denied / .restricted → 直接放弃，给出可操作的提示（**绝不调 start()**）
    ///   · .notDetermined        → 只有在"自己是 responsible process"时才请求
    ///   · .authorized           → 正常开始
    ///
    /// 另外：裸 CLI 二进制（不在 .app bundle 里）**根本拿不到运动权限**，
    /// 因为 responsible process 上没有 NSMotionUsageDescription。
    /// 必须走 build.sh 产出的 .app bundle —— 见 Sources/airpods/Info.plist。
    @discardableResult
    public func start() -> StartResult {
        let auth = CMHeadphoneMotionManager.authorizationStatus()
        if auth == .denied || auth == .restricted {
            setStatus(.denied, auth == .denied
                      ? "运动权限被拒绝 —— 到 系统设置 > 隐私与安全性 > 运动与健身 里手工打开 ListenLook"
                      : "运动权限被系统策略限制（restricted）—— 本机管理员/MDM 关掉了它，改不了")
            return .denied
        }

        guard manager.isDeviceMotionAvailable else {
            setStatus(.noDevice, "未检测到 AirPods —— 确认已连接且已戴上（isDeviceMotionAvailable=false）")
            return .noDevice
        }

        // 到这里状态是 .authorized 或 .notDetermined。
        // .notDetermined 时，请求权限**只有在自己是 responsible process 时**才会成功。
        // 不是的话，CoreMotion 不会返回错误、不会弹窗 —— 进程被 TCC 直接打死。
        // 所以必须自己判断能不能弹，判不出来就宁可报 wrongLauncher 也别去送死。
        if auth == .notDetermined {
            if let why = HeadTracker.launchContextProblem() {
                setStatus(.wrongLauncher, why)
                return .wrongLauncher
            }
        }

        startUnchecked()
        return .ok
    }

    /// 判断"当前这个进程能不能真的弹出运动权限窗"。返回 nil 表示可以。
    ///
    /// 这是本文件里**最贵的**一段判断，代价是实测出来的：
    /// 我们从 DeepSeek Harness Desktop（一个 IDE 类的宿主 app）里 `exec`
    /// 这个 bundle 内的二进制时，进程**确实**死在了 startDeviceMotionUpdates 上。
    /// 崩溃报告里写着：
    ///
    ///     "termination": {"namespace":"TCC",
    ///       "details":["This app has crashed because it attempted to access
    ///                  privacy-sensitive data without a usage description. ..."]}
    ///     "responsibleProc" : "DeepSeek Harness Desktop"
    ///
    /// 注意：bundle 里的 NSMotionUsageDescription **是有的**（PlistBuddy 读得到、
    /// 也绑定进签名了）。死因不是"我们没写说明"，而是 TCC 把 responsible process
    /// 算成了**宿主**，然后去读**宿主的** Info.plist —— 宿主没有那条说明，
    /// 于是判定为"无说明访问隐私数据"，直接 abort。
    /// 这就是原 HeadTracker 注释里那句 `responsibleProc: <宿主的名字>` 的完整含义。
    ///
    /// 所以判断规则是：**从自己往上走父进程链，只要碰到一个 .app bundle
    /// 且它没有 NSMotionUsageDescription，就一定会被打死。**
    /// 这是 TCC 的实际规则，不是启发式猜测 —— 比"可执行文件在不在 .app 里"准得多。
    static func launchContextProblem() -> String? {
        // 1) 最基础的一条：自己得在 .app bundle 里。
        //    裸二进制没有 Info.plist 可言，responsible process 就是它自己，
        //    必然没有用途说明 —— 必然被打死。
        let exe = Bundle.main.executableURL?.resolvingSymlinksInPath().path ?? ""
        let insideApp = Bundle.main.bundleURL.pathExtension == "app"
            && exe.contains(".app/Contents/MacOS/")
        if !insideApp {
            return "运动权限未授权，且当前进程不在 .app bundle 里 —— 裸二进制没有 Info.plist，"
                 + "TCC 必然打死它。请用 ./run.sh 启动 bundle 内的二进制。"
        }

        // 2) 往上找宿主 app。踩过的坑：宿主没有 NSMotionUsageDescription 时，
        //    TCC 会把责任算到它头上 —— 我们的说明写得再清楚也没用。
        var pid = getpid()
        var hops = 0
        while hops < 8 {
            hops += 1
            let parent = parentPID(of: pid)
            if parent <= 1 { break }          // 到 launchd 了，是干净启动
            // 直接由 launchd（或 Finder / open）拉起来的进程，自己就是 responsible
            // process，宿主是 launchd —— 它没有 bundle，也就不会被拿来顶罪。
            if let path = executablePath(of: parent), path.hasSuffix("/launchd") { break }
            if let bundle = appBundlePath(containing: parent) {
                if !bundleHasMotionUsage(bundle) {
                    return "运动权限未授权，且启动它的宿主 App（\(bundle)）没有声明 NSMotionUsageDescription —— "
                         + "TCC 会把责任算到宿主头上，进程会被直接打死（实测崩溃报告里的 "
                         + "responsibleProc 就是宿主）。请在「终端」里直接跑本程序的 run.sh，"
                         + "或双击 Out/airpods.app。"
                }
                // 宿主持有说明：TCC 认它，可以弹。到此为止。
                break
            }
            pid = parent
        }
        return nil
    }

    // ── 下面几个都是 libproc 的薄封装。用了 Darwin 的 proc_pidpath/proc_pidinfo，
    //    它们对同用户的进程可读，不需要任何特权。 ──

    private static func parentPID(of pid: pid_t) -> pid_t {
        var info = proc_bsdinfo()
        let size = MemoryLayout<proc_bsdinfo>.size
        let got = withUnsafeMutablePointer(to: &info) {
            proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, $0, Int32(size))
        }
        guard got == Int32(size) else { return 0 }
        return pid_t(info.pbi_ppid)
    }

    private static func executablePath(of pid: pid_t) -> String? {
        var buf = [CChar](repeating: 0, count: Int(MAXPATHLEN) * 2)
        let n = proc_pidpath(pid, &buf, UInt32(buf.count))
        guard n > 0 else { return nil }
        return String(cString: buf)
    }

    /// 进程的可执行文件路径里是否含 `/Xxx.app/Contents/MacOS/`。含的话返回那个 .app 路径。
    private static func appBundlePath(containing pid: pid_t) -> String? {
        guard let path = executablePath(of: pid) else { return nil }
        guard let r = path.range(of: ".app/Contents/MacOS/") else { return nil }
        return String(path[path.startIndex..<r.lowerBound]) + ".app"
    }

    private static func bundleHasMotionUsage(_ bundle: String) -> Bool {
        let plist = bundle + "/Contents/Info.plist"
        guard let dict = NSDictionary(contentsOfFile: plist),
              let s = dict["NSMotionUsageDescription"] as? String else { return false }
        return !s.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    private func startUnchecked() {
        // 参考姿态清零：start() 的瞬间就是"正前方"。
        refLock.lock(); referenceAttitude = nil; refLock.unlock()

        manager.startDeviceMotionUpdates(to: OperationQueue()) { [weak self] motion, error in
            guard let self else { return }

            if let error {
                // 设备中途断开、被拿走，会从这里来。
                let d = "CoreMotion 报错：\(error.localizedDescription)"
                self.setStatus(.failed, d)
                return
            }
            guard let m = motion else { return }

            // ⚠️ **必须挡掉非有限值。** 实测：CoreMotion 刚启动的头几帧姿态可能是 NaN。
            // 一旦放下去，任何 `Int(NaN)` 都会直接触发 Swift 运行时陷阱
            // （SIGABRT，stderr 什么都不打，极难定位）；`String(format:)` 拿到 NaN 也只会
            // 打出 "nan" 污染下游。
            // 传感器数据永远要先做有限性检查，不能假设它干净。
            guard m.attitude.yaw.isFinite,
                  m.attitude.pitch.isFinite,
                  m.attitude.roll.isFinite else { return }

            // 留一份**原始**值给 LL_DEBUG_RAW 用。必须在翻符号之前记 ——
            // 它存在的唯一用途就是"核对翻符号翻得对不对"。
            self.stateLock.lock()
            self._raw = (m.attitude.yaw, m.attitude.pitch, m.attitude.roll,
                         Date().timeIntervalSince1970)
            self.stateLock.unlock()

            self.refLock.lock()
            if self.referenceAttitude == nil {
                // 以"第一帧有效姿态"为零点：要的是**相对转动量**，不是绝对方位。
                // 绝对方位依赖地磁，室内干扰大，对"用头控制鼠标"也毫无意义 ——
                // 玩家关心的是"我比刚才转了多少"，不是"我在地球上的哪个方向"。
                self.referenceAttitude = m.attitude.copy() as? CMAttitude
            }
            let ref = self.referenceAttitude
            self.refLock.unlock()
            guard let ref else { return }

            // 相对姿态 = 当前 × 参考⁻¹。用四元数乘法而不是三个欧拉角各自相减 ——
            // 欧拉角相减在跨 ±180°、或 pitch 接近 ±90°（万向节死锁）时会算错。
            // 乘法版本是刚体旋转的严格组合，没有这个问题。
            // 注意：这里用的是**右乘逆**（body-frame 增量）。对头部这种小角度转动，
            // 它与左乘（world-frame 增量）数值上几乎一致，右乘的优点是
            // "转 30° 然后归零再转 30°"始终给出 30°，不会累积。
            let cur = m.attitude.copy() as? CMAttitude ?? m.attitude
            cur.multiply(byInverseOf: ref)

            let toDeg = 180.0 / Double.pi
            // ───────── 符号约定（ListenLook Pose v1）─────────
            // 目标（PROTOCOL.md 那张表，接收端只认这个，不做任何猜测）：
            //   yaw   +  = 头向**右**转
            //   pitch +  = **抬头**向上
            //   roll  +  = 头向**右**歪
            //
            // CoreMotion 的 CMAttitude 是右手系，旋转正方向按右手定则：
            //   yaw（绕 z，z 向上）正值在俯视图里是**逆时针** = 头向**左**转。
            //   → 与协议**相反**，必须翻符号。
            //   这条不是我从文档推的：原 HeadTracker 的 HeadPlane 注释里写着
            //   "Core Motion 的 yaw 在当前 AirPods 坐标约定里正值对应左转；
            //    面板采用常见俯视图坐标（+X=右），因此先翻转符号。否则用户左转时
            //    指针会向右。" —— 那是用户实测过的结论，这里沿用。
            //
            //   pitch（绕 y）：CMAttitude 的设备系是 x 朝右、y 朝前、z 朝**下**
            //   （右手系：z = x × y；UIKit/SwiftUI 的 x 朝右、y 朝下是同一套）。
            //   绕 +y 的右手旋转把 +z 转向 +x：z 是"下"，所以是**下方转向右侧**
            //   = 机头下沉 = **低头**。
            //   → CoreMotion 的 pitch 正值 = 低头，与协议**相反**，也要翻符号。
            //
            //   roll（绕 x，x 朝右）：绕 +x 的右手旋转把 +y（前）转向 +z（下），
            //   即**前方向下** = 左侧下沉 = 头向**左**歪。
            //   → CoreMotion 的 roll 正值 = 左歪，与协议**相反**，同样翻符号。
            //
            // 结论：三个轴都要取负。三者一致不是巧合：两个坐标系的关系是一次镜像
            // —— 协议描述的是**用户视角**（往右转、抬头，按人的直觉定正方向），
            // CoreMotion 是数学右手系。一次镜像同时翻转三个轴，所以"三行同号"
            // 本身就是自洽性检查：若实测只有一个轴反了，那是别处错了，
            // 不是"约定不同"能解释的。
            //
            // ⚠️ 诚实边界：截止到本文件写完，**没有在真人头上做过受控的方向验证**
            // （需要一个人戴着头套机读原始值并刻意左转/抬头/右歪）。上面的推导
            // 是几何 + 原仓库实测结论的组合，自洽但未实测。用 `--json` 跑起来后
            // 做一次"刻意向右转 → yaw 应变大"的检查即可确认；
            // 三个轴若**全部**反了，是整体镜像问题（换 multiply 的左右乘）；
            // 若只有某一个轴反了，改对应的那一行负号。接收端也有
            // --invert-yaw / --invert-pitch 可以现场纠正，不用改这里。
            var r = PoseReading()
            r.yawDeg   = -cur.yaw   * toDeg
            r.pitchDeg = -cur.pitch * toDeg
            r.rollDeg  = -cur.roll  * toDeg
            // 双保险：翻符号不会算出非有限值，但这两个值要进入 String(format:)
            // 和 Int()，所以在这里再挡一次。宁可为 0，不可为 nan。
            guard r.yawDeg.isFinite, r.pitchDeg.isFinite, r.rollDeg.isFinite else { return }
            r.available = true
            r.deviceName = "AirPods（Core Motion 姿态）"

            self.stateLock.lock()
            self._reading = r
            if self._status != .streaming {
                self._status = .streaming
                self._message = "AirPods IMU 已就绪，正在广播"
            }
            self.stateLock.unlock()

            self.onSample?(r)
        }

        started = true
        setStatus(.starting, "已向 CoreMotion 申请数据，等待第一帧有效姿态…")
    }

    public func stop() {
        if started { manager.stopDeviceMotionUpdates() }
        started = false
        refLock.lock(); referenceAttitude = nil; refLock.unlock()
    }

    /// 归零：把**当前**姿态当作新的"正前方"（面板上按 `r`）。
    /// 用 manager.deviceMotion（最近一帧）而不是等下一个 callback ——
    /// 这样按键的反馈是即时的，不用等最多一帧。
    public func recenter() {
        guard let m = manager.deviceMotion else { return }
        guard m.attitude.yaw.isFinite, m.attitude.pitch.isFinite, m.attitude.roll.isFinite else { return }
        refLock.lock()
        referenceAttitude = m.attitude.copy() as? CMAttitude
        refLock.unlock()
    }

    // MARK: - 内部

    private func setStatus(_ s: Status, _ msg: String) {
        stateLock.lock()
        _status = s
        _message = msg
        if s != .streaming { _reading.available = false }
        stateLock.unlock()
    }
}
