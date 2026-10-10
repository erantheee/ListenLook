// ListenLook —— UDP 姿态发送端（ListenLook Pose v1 线格式）
//
// 一个数据报一个 UTF-8 JSON 对象，**没有换行结尾**（UDP 的边界就是数据报边界）。
// 手工拼 JSON 而不是用 JSONSerialization：字段是固定的七个、全部是数字/布尔，
// 手拼能精确控制浮点格式（%.4f，避免 1.2000000000000002 这种脏值），
// 也免掉 Codable 在 200 Hz 下的开销。**前提是绝不把非有限值格式化进去**，
// 见 frame() 里那道闸门。
//
// 与 PROTOCOL.md 的对应关系写在 frame() 的注释里，改协议前先读那段。

import Foundation
import Darwin

public final class PoseSender {

    /// 一帧要发的东西。角度单位：度，符号约定见 HeadTracker。
    public struct Frame {
        public var yaw: Double = 0
        public var pitch: Double = 0
        public var roll: Double = 0
        public var ok: Bool = false
        public init() {}
    }

    public let host: String
    public let port: UInt16

    /// 自增序号。接收端用它统计丢包率（不是用来重排序）。从 1 开始。
    public private(set) var seq: UInt64 = 0
    /// 成功发出去的数据报数
    public private(set) var sentCount: UInt64 = 0
    /// 发送失败次数（一般只有网络栈临时抽风才会 >0，UDP 无连接不阻塞）
    public private(set) var errorCount: UInt64 = 0
    /// 最近一次错误的描述，给面板显示
    public private(set) var lastError: String = ""

    private var fd: Int32 = -1
    private var addr = sockaddr_in()
    private var addrLen: socklen_t = 0
    private var closed = false
    /// 保护 fd/addr/seq/计数器：send 会被采样线程和主线程同时调用。
    private let lock = NSLock()

    /// - Parameters:
    ///   - host: 目标 IP。默认 127.0.0.1（只喂本机）。
    ///   - port: 目标端口。默认 8420。
    public init(host: String, port: UInt16) {
        self.host = host
        self.port = port
    }

    /// 建 socket。失败**不抛错、不退出** —— 面板上显示原因，程序继续跑。
    /// 传感器端最常见的失败是把 IP 打错了（连不上是无声的，UDP 没有 connect 握手），
    /// 所以这里把解析结果也记下来，让用户能看见"到底发去哪了"。
    public func open() -> Bool {
        fd = socket(AF_INET, SOCK_DGRAM, 0)
        guard fd >= 0 else {
            lastError = "socket() 失败 errno=\(errno)"
            errorCount += 1
            return false
        }

        addr = sockaddr_in()
        addr.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = in_port_t(port).bigEndian
        // inet_addr 对非法输入返回 INADDR_NONE(0xffffffff)，那是个合法广播地址，
        // 照发会静默失败。显式挡掉，给一句人话。
        let parsed = inet_addr(host)
        if parsed == INADDR_NONE && host != "255.255.255.255" {
            lastError = "目标地址无法解析为 IPv4：\(host)（本版本只支持 IPv4 字面量）"
            errorCount += 1
            return false
        }
        addr.sin_addr.s_addr = parsed
        addrLen = socklen_t(MemoryLayout<sockaddr_in>.size)

        // 非阻塞：UDP sendto 本来就几乎不阻塞，但在缓冲区满时仍可能短暂阻塞。
        // 这是传感器采样回调里跑的代码，**绝不能因为网络卡住而丢采样**。
        _ = fcntl(fd, F_SETFL, O_NONBLOCK)
        return true
    }

    public func close() {
        guard !closed else { return }
        closed = true
        if fd >= 0 { Darwin.close(fd); fd = -1 }
    }

    /// 组装并发出一个数据报。
    ///
    /// 线格式（PROTOCOL.md，**逐字对应，改这里等于改协议**）：
    ///   {"v":1,"src":"airpods","seq":<int>,"t":<unix_ms_int>,
    ///    "yaw":<deg>,"pitch":<deg>,"roll":<deg>,"ok":<bool>}
    ///
    /// · 协议里 seq/t 标为可选，我们**永远发** —— 接收端要靠 t 判掉线（>500ms）、
    ///   靠 seq 统计丢包，省掉它们等于让接收端瞎一半。
    /// · 键顺序固定，方便 `nc -ul 8420` 肉眼读、也方便 diff。
    ///
    /// 返回是否真的发出去了。
    @discardableResult
    public func send(_ f: Frame) -> Bool {
        // ⚠️ 串行化。采样来自 CoreMotion 的后台队列，心跳来自主线程的 tick ——
        // 两个线程同时进来会让 seq 乱序（接收端统计丢包率时会看到"负丢包"），
        // 也会让两个数据报交错。取锁的代价是几十纳秒，远小于 10ms 的帧间隔。
        lock.lock()
        defer { lock.unlock() }

        seq &+= 1
        let payload = frameLocked(f)
        guard let data = payload.data(using: .utf8) else {
            errorCount += 1; lastError = "UTF-8 编码失败"; return false
        }
        guard fd >= 0 else {
            errorCount += 1; lastError = "socket 未打开"; return false
        }
        let n = data.withUnsafeBytes { raw -> Int in
            guard let base = raw.baseAddress else { return -1 }
            return withUnsafePointer(to: &addr) { p in
                p.withMemoryRebound(to: sockaddr.self, capacity: 1) { sa in
                    sendto(fd, base, data.count, 0, sa, addrLen)
                }
            }
        }
        if n < 0 {
            errorCount += 1
            lastError = "sendto 失败 errno=\(errno)"
            return false
        }
        sentCount += 1
        return true
    }

    /// 取当前 seq 下这一帧会是什么样，用于自检/日志。加锁版。
    public func frame(_ f: Frame) -> String {
        lock.lock(); defer { lock.unlock() }
        return frameLocked(f)
    }

    /// 调用方**必须已经持锁**。NSLock 不是递归锁，send() 里再调 frame() 会死锁，
    /// 所以内部只走这个不带锁的实现。
    private func frameLocked(_ f: Frame) -> String {
        let t = Int(Date().timeIntervalSince1970 * 1000)   // unix 毫秒
        // ⚠️ 有限性闸门。Int(NaN) 会直接 trap（SIGABRT，无 stderr），
        // String(format: "%.4f", NaN) 会打出 "nan" 让接收端整帧丢弃。
        // 传感器数据不可信，**在这里统一兜底**，调用方不必各自记得检查。
        let yaw   = f.yaw.isFinite   ? f.yaw   : 0
        let pitch = f.pitch.isFinite ? f.pitch : 0
        let roll  = f.roll.isFinite  ? f.roll  : 0
        let ok = f.ok && f.yaw.isFinite && f.pitch.isFinite && f.roll.isFinite
        // ok:false 时角度必须补 0，不得省略（协议硬性要求）。
        let y = ok ? yaw : 0, p = ok ? pitch : 0, r = ok ? roll : 0
        return #"{"v":1,"src":"airpods","seq":\#(seq),"t":\#(t),"yaw":\#(fmt(y)),"pitch":\#(fmt(p)),"roll":\#(fmt(r)),"ok":\#(ok ? "true" : "false")}"#
    }

    /// 固定 4 位小数。**先过有限性检查**（由 frame() 保证），此处只负责格式。
    /// 用 %.4f 而不是 %g：%g 会输出 1e-05 这种，JSON 里合法但人眼难读，
    /// 也让 `nc -ul` 的肉眼调试变难。0.0001° 远小于 IMU 噪声，精度足够。
    private func fmt(_ v: Double) -> String {
        String(format: "%.4f", v)
    }
}
