// ListenLook —— macOS 传感器端入口
//
//   AirPods IMU ──> HeadTracker ──> PoseSender ──(UDP, ListenLook Pose v1)──> receiver
//
// 本文件只做四件事：解析参数、决定输出形态（面板 / JSON / 静默）、
// 把采样与心跳接起来、处理 r/q 两个按键。
//
// **不做的事**：鼠标注入、映射引擎、音频、麦克风、摄像头。
// 那是 receiver 的活，不在本仓库。这是个纯输入端。

import Foundation
import Darwin

// MARK: - 常量

let kVersion = "1.0.0"
/// 协议默认端口，见 PROTOCOL.md。
let kDefaultPort: UInt16 = 8420
/// hz 上限。协议要求"发送速率跟随 CoreMotion 回调率，且**不超过 200 Hz**"。
let kMaxHz = 200
/// 采样判定为"陈旧"的时限。超过它，即便传感器还说 available，
/// 也认为这一帧不该再当有效数据广播了（AirPods 摘下来时回调会停，
/// 但 isDeviceMotionAvailable 和最后一次 reading 都不会立刻变）。
let kStaleAfter: TimeInterval = 0.35

// MARK: - 输出形态

enum OutputMode {
    case panel     // 实时三轴面板（stdout 必须是 TTY）
    case json      // 每个更新打印一行协议 JSON
    case quiet     // 什么都不打印
}

// MARK: - 参数

struct Options {
    var host = "127.0.0.1"
    var port = kDefaultPort
    var quiet = false
    var json = false
    var hz = 0            // 0 = 不额外限速（跟随 CoreMotion 回调）
    var debugRaw = false  // LL_DEBUG_RAW=1，面板上显示 CoreMotion 原始值
    var outPath: String?  // 把 JSON 行写进文件而不是 stdout
    var seconds = 0       // >0 时跑够这么多秒自动退出（自检/脚本用）
    /// 演示/自检模式：**完全不碰 CoreMotion**，用合成姿态驱动面板与发送端。
    /// 存在的理由有两个，都不是好玩：
    ///   1. 面板与协议链路可以在任何机器上验证（CI 里没有 AirPods，也可能没有运动权限）；
    ///   2. 没有 AirPods 的人也能先看清这个工具长什么样、会发什么数据。
    var demo = false
}

let usage = """
ListenLook —— AirPods 头部姿态传感器端（macOS）

把 AirPods 的头部姿态以 ListenLook Pose v1 协议（UDP JSON）广播出去。
本程序只采集、只发送；鼠标映射与注入在 receiver 里，不在本程序。

用法：
  ./run.sh [选项]

选项：
  --host <ip>     目标地址，默认 127.0.0.1（只喂本机）
  --port <n>      目标端口，默认 8420
  --quiet         不画面板，也不打印姿态
  --json          每个更新打印一行协议 JSON（喂给管道/日志用）
  --hz <n>        发送速率上限，默认 0 = 不额外限速（跟随 CoreMotion 回调）
  --out <path>    把 JSON 行写进文件（便于用 open 启动时留证据）
  --seconds <n>   跑够 n 秒自动退出，默认 0 = 一直跑（自检/脚本用）
  --demo          演示模式：发合成姿态，**不读取任何真实传感器、不需要权限**
  -h, --help      显示这段帮助
  -v, --version   显示版本

交互（面板模式下）：
  r  归零 —— 把当前姿态当作新的正前方
  q  退出

环境变量：
  LL_DEBUG_RAW=1   在面板上额外显示 CoreMotion 原始角度，用于核对符号约定

stdout 不是终端时，自动按 --json 行为运行（绝不往管道里写光标控制码）。
"""

/// 手写参数解析。刻意不用 ArgumentParser：本机没有 Xcode、不引第三方依赖，
/// 而且这个参数集小到不值得为一个库付出"多一个包"的代价。
func parseArgs(_ argv: [String]) -> (Options?, Int32) {
    var o = Options()
    o.debugRaw = ProcessInfo.processInfo.environment["LL_DEBUG_RAW"] == "1"
    var i = 1
    func next(_ name: String) -> String? {
        i += 1
        guard i < argv.count else {
            FileHandle.standardError.write(Data("参数错误：\(name) 后面缺少值\n\n\(usage)\n".utf8))
            return nil
        }
        return argv[i]
    }
    while i < argv.count {
        let a = argv[i]
        switch a {
        case "--host":
            guard let v = next(a) else { return (nil, 2) }
            o.host = v
        case "--port":
            guard let v = next(a) else { return (nil, 2) }
            // 端口必须显式校验：UInt16("70000") 是 nil。不查的话会静默退回默认端口，
            // 用户以为改成功了、实际发去了 8420 —— 这种"看起来在工作"的错最难查。
            guard let n = UInt16(v), n > 0 else {
                FileHandle.standardError.write(Data("参数错误：--port 需要 1..65535 的整数，收到 '\(v)'\n".utf8))
                return (nil, 2)
            }
            o.port = n
        case "--hz":
            guard let v = next(a) else { return (nil, 2) }
            guard let n = Int(v), n >= 0 else {
                FileHandle.standardError.write(Data("参数错误：--hz 需要 >=0 的整数，收到 '\(v)'\n".utf8))
                return (nil, 2)
            }
            // 超上限不报错退出（现场不该因为一个多余参数跑不起来），但**要明说被夹住了**，
            // 否则用户以为自己真在 500 Hz 发。
            if n > kMaxHz {
                FileHandle.standardError.write(Data("注意：--hz \(n) 超过协议上限，已夹到 \(kMaxHz) Hz\n".utf8))
                o.hz = kMaxHz
            } else {
                o.hz = n
            }
        case "--demo": o.demo = true
        case "--quiet": o.quiet = true
        case "--json": o.json = true
        case "--out":
            guard let v = next(a) else { return (nil, 2) }
            o.outPath = v
        case "--seconds":
            guard let v = next(a) else { return (nil, 2) }
            guard let n = Int(v), n >= 0 else {
                FileHandle.standardError.write(Data("参数错误：--seconds 需要 >=0 的整数，收到 '\(v)'\n".utf8))
                return (nil, 2)
            }
            o.seconds = n
        case "-h", "--help":
            print(usage); return (nil, 0)
        case "-v", "--version":
            print("ListenLook airpods source \(kVersion)  (protocol v1)"); return (nil, 0)
        default:
            FileHandle.standardError.write(Data("未知参数：\(a)\n\n\(usage)\n".utf8))
            return (nil, 2)
        }
        i += 1
    }
    return (o, 0)
}

func err(_ s: String) {
    // FileHandle.standardError.write 直写 fd 2，不经过 stdio 缓冲 ——
    // 面板模式下 stdout 是行缓冲的，用 print() 写 stderr 会和面板交错。
    FileHandle.standardError.write(Data((s + "\n").utf8))
}

// MARK: - 应用

final class App {
    let opts: Options
    let sender: PoseSender
    let tracker = HeadTracker()
    let mode: OutputMode
    let panel: Panel?

    private var lastSendAt: TimeInterval = 0
    private var lastSampleAt: TimeInterval = 0
    private let startedAt = Date().timeIntervalSince1970
    private var sampleWindow: [TimeInterval] = []
    private var hz: Double = 0
    private var authText = ""
    private var authPollAt: TimeInterval = 0
    private var stopping = false
    /// 读取到的按键。在 tick 里消费 —— 按键回调里不碰网络、不碰 UI。
    private var lastKey: Character?
    /// 心跳节流：无有效数据时也**绝不超过 4 Hz**（协议要求）。
    private var lastHeartbeatAt: TimeInterval = 0
    /// --out 指定的 JSON 行输出文件。用 `open` 启动 app 时 stdout 是 /dev/null
    /// （或者一个不可见的控制台），所以想留证据必须落到文件里。
    private var outFile: FileHandle?

    init(opts: Options, mode: OutputMode) {
        self.opts = opts
        self.mode = mode
        self.sender = PoseSender(host: opts.host, port: opts.port)
        self.panel = (mode == .panel) ? Panel() : nil
    }

    private func openOutFileIfNeeded() {
        guard let path = opts.outPath else { return }
        let fm = FileManager.default
        if !fm.fileExists(atPath: path) { fm.createFile(atPath: path, contents: nil) }
        guard let fh = FileHandle(forWritingAtPath: path) else {
            err("[listenlook] 打不开 --out 指定的文件：\(path)")
            return
        }
        fh.seekToEndOfFile()
        outFile = fh
    }

    /// JSON 行唯一的出口：有 --out 就写文件，否则写 stdout。
    private func writeJSONLine(_ line: String) {
        if let fh = outFile {
            fh.write(Data((line + "\n").utf8))
        } else {
            print(line)
        }
    }

    // MARK: 启动

    func run() -> Int32 {
        let target = "\(opts.host):\(opts.port)"
        authText = HeadTracker.authorizationDescription()

        // 先建 socket。失败也**不退出** —— 面板上显示原因，让用户能改参数重跑，
        // 而不是对着一个"启动即退出"的程序猜哪里错了。
        let opened = sender.open()

        // 有 --out 就打开它。app 是在自己的进程里写文件，不受宿主沙箱约束，
        // 但目录可能不可写 —— 打不开就明说，绝不静默丢数据。
        openOutFileIfNeeded()

        if mode != .quiet {
            err("[listenlook] source=airpods protocol=v1 target=\(target) "
                + "auth=\(authText) socket=\(opened ? "ok" : sender.lastError)")
        }

        tracker.onSample = { [weak self] reading in
            // ⚠️ 这在 CoreMotion 的后台队列上。只做"取锁 + sendto"，
            // 不碰面板、不碰 stdout（面板由主线程的 tick 画）。
            self?.emit(reading, unavailable: false)
        }

        if opts.demo {
            // 演示模式刻意**不调用** tracker.start()：它一碰 CoreMotion 就会走 TCC，
            // 而在没有权限的宿主里那一步会直接打死进程。面板验证不需要真传感器。
            if mode != .quiet {
                err("[listenlook] 演示模式：发送合成姿态，未读取任何真实传感器")
            }
        } else {
            let result = tracker.start()
            if result != .ok, mode != .quiet {
                err("[listenlook] \(tracker.statusMessage)")
            }
        }

        if mode == .panel {
            KeyReader.start { [weak self] ch in self?.lastKey = ch }
        }

        // 定时器走主 RunLoop：面板重绘、心跳、按键处理都在主线程，
        // 与 CoreMotion 的后台队列天然分开了，不需要额外同步 stdout。
        let t = Timer(timeInterval: 1.0 / 8.0, repeats: true) { [weak self] _ in
            self?.tick()
        }
        RunLoop.main.add(t, forMode: .common)

        // SIGTERM：把面板收干净再退。
        // SIGINT（Ctrl-C）由 Panel 自己接管 —— 它必须比这里更早装上，
        // 因为只有它知道怎么把光标和 termios 恢复回去。这里只管 SIGTERM。
        // ⚠️ 信号处理里只做 async-signal-safe 的事：write 几个字节 + _exit。
        // 不能在信号处理里触发 Swift 运行时的分配/加锁 —— 会死锁。
        signal(SIGTERM) { _ in
            let msg = "\u{1B}[?25h\n[listenlook] 收到 SIGTERM，已停止\n"
            _ = msg.withCString { p in write(STDOUT_FILENO, p, strlen(p)) }
            _exit(0)
        }

        tick()   // 先画一帧，避免启动后有半秒空白
        RunLoop.main.run()
        return 0
    }

    // MARK: 发送

    /// 唯一的发送出口。采样回调与心跳都走这里 —— 限速与状态判断只有一份实现。
    private func emit(_ reading: PoseReading, unavailable: Bool) {
        let now = Date().timeIntervalSince1970

        // rate cap。--hz 0 表示只受 200 Hz 协议上限约束。
        let capHz = opts.hz > 0 ? min(opts.hz, kMaxHz) : kMaxHz
        if lastSendAt > 0, now - lastSendAt < (1.0 / Double(capHz)) - 1e-9 { return }

        var f = PoseSender.Frame()
        if unavailable || !reading.available {
            // 协议硬性要求：ok:false 时角度字段必须补 0，不得省略。
            f.ok = false
        } else {
            f.yaw = reading.yawDeg
            f.pitch = reading.pitchDeg
            f.roll = reading.rollDeg
            f.ok = true
        }

        guard sender.send(f) else { return }   // 失败已记在 sender 里，面板会显示
        lastSendAt = now

        if mode == .json {
            // 一行一帧协议 JSON。stdout **只**出这个（或者 --out 的文件），
            // 别的都走 stderr，这样 `./run.sh --json > poses.jsonl`
            // 直接就是干净的数据集。
            writeJSONLine(sender.frame(f))
        }
    }

    // MARK: 主循环

    private func tick() {
        let now = Date().timeIntervalSince1970
        let reading: PoseReading
        if opts.demo {
            // 合成一个缓慢画 8 字的姿态：三条轴同时动，便于肉眼确认面板三根条都在工作
            let t = now - startedAt
            var r = PoseReading()
            r.yawDeg = 18.0 * sin(t * 0.7)
            r.pitchDeg = 12.0 * sin(t * 1.1 + 0.6)
            r.rollDeg = 6.0 * sin(t * 0.35)
            r.available = true
            r.deviceName = "合成姿态（--demo，非实测）"
            reading = r
        } else {
            reading = tracker.reading
        }

        // ── 心跳 / 陈旧数据兜底 ──
        // 采样回调停了（AirPods 摘了、没连、没权限）也要继续说话：
        // 静默是最糟的行为 —— 接收端会分不清"玩家没动"和"传感器挂了"。
        // 节流到 4 Hz（协议上限）。tick 是 8 Hz，不节流会发成 8 Hz。
        if opts.demo {
            // 演示模式没有 CoreMotion 回调，样本由 tick 自己产生（8Hz）。
            // 必须先更新 lastSampleAt —— 否则下面那段"陈旧数据兜底"会认为采样停了，
            // 按协议把角度清零成 ok:false，于是演示模式发出去的全是零。
            lastSampleAt = now
            sampleWindow.append(now)
            emit(reading, unavailable: false)
        } else if now - lastSampleAt >= 0.25, now - lastHeartbeatAt >= 0.25 {
            lastHeartbeatAt = now
            let stale = now - lastSampleAt >= kStaleAfter
            var r = reading
            if stale { r.available = false }   // 陈旧数据不得再当有效帧发出去
            emit(r, unavailable: stale)
        }

        // ── 有效采样率（最近 1 秒）──
        sampleWindow.removeAll { now - $0 > 1.0 }
        hz = Double(sampleWindow.count)

        // ── 权限状态：每秒查一次 ──
        // 用户在程序跑起来之后才去系统设置里打开开关的话，面板应当自己变过来，
        // 而不是必须重启。查这个 API 是纯读取、无副作用，不会被 TCC 记一笔。
        if now - authPollAt > 1.0 {
            authPollAt = now
            authText = HeadTracker.authorizationDescription()
        }

        // ── 按键 ──
        if let k = lastKey {
            lastKey = nil
            switch k {
            case "r", "R": tracker.recenter()
            case "q", "Q": stopping = true
            default: break   // 其它键无害忽略。**永远不要因为按错键退出。**
            }
        }

        // ── 输出 ──
        switch mode {
        case .panel:
            guard isatty(STDOUT_FILENO) == 1 else {
                // stdout 中途被重定向到管道了。此刻再画就会把 ANSI 写进日志。
                // 这是编程错误级别的意外，明确报出来并退出，而不是偷偷继续画。
                panel?.finish(note: "[listenlook] stdout 不再是终端，已停止面板输出")
                err("[listenlook] stdout 不再是终端 —— 面板模式中止。要写日志请用 --json。")
                exit(1)
            }
            var st = Panel.Status()
            st.target = "\(opts.host):\(opts.port)"
            st.authText = authText
            st.statusText = opts.demo
                ? "演示模式（--demo）：合成姿态，未读取任何真实传感器"
                : tracker.statusMessage
            st.healthy = reading.available
            st.deviceName = reading.deviceName
            st.seq = sender.seq
            st.sentCount = sender.sentCount
            st.errorCount = sender.errorCount
            st.sendError = sender.lastError
            st.hz = hz
            st.uptime = now - startedAt
            if opts.debugRaw { st.debugLine = tracker.rawDebug }
            panel?.render(reading, status: st)
        case .json, .quiet:
            // --json 的行由 emit() 打印（每个更新一行）；
            // --quiet 什么都不打印。两者都不需要 tick 做任何事。
            break
        }

        if stopping { shutdown() }

        // --seconds：跑够就自己退。存在的理由是**用 `open` 启动时没法在外面
        // Ctrl-C**（进程的 stdout 不在你的终端里），自检必须能自己收尾。
        if opts.seconds > 0, now - startedAt >= Double(opts.seconds) {
            shutdown()
        }
    }

    private func shutdown() {
        tracker.stop()
        if let fh = outFile { try? fh.synchronize(); try? fh.close() }
        sender.close()
        let note = "[listenlook] 已退出。sent=\(sender.sentCount) seq=\(sender.seq) err=\(sender.errorCount)"
        if let p = panel {
            p.finish(note: note)          // 恢复光标 + 换行，别把用户终端搞坏
        } else if mode != .quiet {
            err(note)
        }
        exit(0)
    }
}

// MARK: - 键盘

/// 非规范模式下的按键读取，独立线程。
/// ⚠️ 只在面板模式启用 —— `--json` / `--quiet` **绝不**改用户的终端设置。
enum KeyReader {
    static func start(_ handler: @escaping (Character) -> Void) {
        let t = Thread {
            var byte: UInt8 = 0
            while true {
                let n = read(STDIN_FILENO, &byte, 1)
                if n == 1 {
                    // 只认 ASCII 可打印字符。方向键等发的是多字节转义序列，
                    // 这里逐字节读会把它拆成几个"未知键" —— 无害，未知键被忽略。
                    if byte >= 0x20, byte < 0x7F, let scalar = UnicodeScalar(UInt32(byte)) {
                        handler(Character(scalar))
                    }
                } else if n == 0 {
                    // VMIN=0 + VTIME=0.1 → read 超时返回 0，这是**常态不是 EOF**。
                    // 不 sleep 的话这里会变成忙等。
                    usleep(50_000)
                } else if errno != EAGAIN && errno != EINTR {
                    return   // stdin 真的坏了，安静退出这个线程；主循环照跑。
                }
            }
        }
        t.name = "listenlook.keys"
        t.stackSize = 256 * 1024
        t.start()
    }
}

// MARK: - main

let (parsed, exitCode) = parseArgs(CommandLine.arguments)
if let opts = parsed {
    // ── 关键：非 TTY 自动降级 ──
    // 一旦 stdout 不是终端（管道、重定向、CI、被别的程序抓走），
    // 就**绝对不能**输出 ANSI 光标控制码 —— 否则日志里全是 \e[?25l / \e[2J，
    // grep 和 diff 全部作废。这里直接切成 --json 行为，与用户显式传 --json 等价。
    var mode: OutputMode
    if opts.quiet {
        mode = .quiet
    } else if opts.json || opts.outPath != nil {
        // --out 隐含 --json：它的用途就是"把 JSON 行落到文件里"。
        mode = .json
    } else if isatty(STDOUT_FILENO) == 1 {
        mode = .panel
    } else {
        mode = .json
        err("[listenlook] stdout 不是终端 → 自动按 --json 运行（不输出光标控制码）")
    }

    let app = App(opts: opts, mode: mode)
    exit(app.run())
} else {
    exit(exitCode)
}
