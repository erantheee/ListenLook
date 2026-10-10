// ListenLook —— 终端实时面板（三轴条形图）
//
// 设计约束（都是硬的）：
//   · **绝不往非 TTY 写 ANSI 转义**。上层已经保证只有 stdout 是 TTY 才会构造 Panel，
//     这里再挡一次（构造时 + 每次渲染时都查 isatty），因为一旦漏了，
//     `./run.sh | tee log` 的日志里就会混进光标控制码，grep 全废。
//   · **绝不让非有限值进 String(format:)**。面板收到的值在进渲染前统一过一遍
//     finite()。String(format:"%.1f", .nan) 不崩但会打出 "nan"，
//     而 Int(nan) 是运行时陷阱（SIGABRT、无 stderr）—— 统一在入口挡，后面都不必再想。
//   · 增量重绘（逐行 \e[K）而不是 \e[2J 清屏：清屏会闪。
//   · 退出时一定把光标还回去、把 termios 改回来。三条退出路径都覆盖：
//     正常退出（finish）、Ctrl-C（Panel 自己装的 SIGINT handler）、
//     以及进程以任何方式结束（atexit）。三条都是幂等的。

import Foundation
import Darwin

/// 只在面板模式下实例化。构造即接管终端（藏光标 + stdin 非规范模式），
/// 所以**不要**在 --json / --quiet 模式下构造它。
public final class Panel {

    /// 面板要显示的全部状态。故意做成纯数据 —— Panel 不做任何采集/网络。
    public struct Status {
        public var target: String = "127.0.0.1:8420"
        public var authText: String = ""
        public var statusText: String = ""
        public var healthy: Bool = false
        public var deviceName: String = ""
        public var seq: UInt64 = 0
        public var sentCount: UInt64 = 0
        public var errorCount: UInt64 = 0
        public var sendError: String = ""
        /// 最近 1 秒内的有效采样数 —— 面板上显示成 "xx.x Hz"
        public var hz: Double = 0
        public var uptime: Double = 0
        /// 额外诊断（CoreMotion 原始角度），只在 LL_DEBUG_RAW=1 时非空。
        /// 用来现场核对符号约定 —— 这是本面板唯一的"多余"字段，值得留着。
        public var debugLine: String = ""
        public init() {}
    }

    private var width: Int = 58
    private var drawnLines = 0
    private var cursorHidden = false
    private var savedTermios: termios?
    private var termiosSaved = false
    private var finished = false

    private static let maxWidth = 62
    private static let minWidth = 40
    /// 条形图的满量程（度）。±90° 覆盖正常人头的可用转动范围；
    /// 超过就顶格显示，但**数字仍然是真实值**（不截断）。
    private static let fullScaleDeg: Double = 90

    public init() {
        width = min(Self.maxWidth, max(Self.minWidth, Self.terminalWidth()))
        setupTerminal()
    }

    // MARK: - 终端接管与归还

    private static func terminalWidth() -> Int {
        guard isatty(STDOUT_FILENO) == 1 else { return maxWidth }
        var ws = winsize()
        if ioctl(STDOUT_FILENO, UInt(TIOCGWINSZ), &ws) == 0, ws.ws_col > 0 {
            return Int(ws.ws_col)
        }
        // 拿不到 winsize（少见）时的兜底。别无脑当 80 —— 那会让布局在 60 列的窗口里换行。
        return maxWidth
    }

    private var effectiveWidth: Int {
        // 每帧重算：用户中途拉窗口也能跟上。留 1 列余量，避免正好顶到右边界触发自动换行。
        min(width, Self.terminalWidth() - 1)
    }

    private func setupTerminal() {
        // 1) 光标归还的保险丝，**第一个**注册 —— 后面任何一步失败，
        //    终端也不会留下"光标不见了"的状态。
        atexit { FileHandle.standardOutput.write(Data("\u{1B}[?25h\n".utf8)) }

        // 2) 只在这个进程真的连着终端时才动 termios。
        var orig = termios()
        if isatty(STDIN_FILENO) == 1, tcgetattr(STDIN_FILENO, &orig) == 0 {
            savedTermios = orig                // 留一份原始设置，退出时还原
            termiosSaved = true
            var t = orig
            t.c_lflag &= ~UInt(ICANON | ECHO)   // 不等回车、不回显
            t.c_cc.16 = 0   // VMIN  = 0
            t.c_cc.17 = 1   // VTIME = 0.1s → read 没数据就返回 0，不阻塞
            _ = tcsetattr(STDIN_FILENO, TCSANOW, &t)
        }

        // 3) Ctrl-C：Panel 自己接管。不这么做的话 SIGINT 走默认动作，
        //    进程直接死 —— atexit 不跑（那是 exit() 的钩子，不是信号钩子），
        //    用户的终端会永远留在"没有光标 + 不回显"的状态里。
        //    ⚠️ 信号处理里只做 async-signal-safe 的事：write + _exit。
        signal(SIGINT) { _ in
            let msg = "\u{1B}[?25h\n[listenlook] Ctrl-C 退出\n"
            _ = msg.withCString { p in write(STDOUT_FILENO, p, strlen(p)) }
            _exit(0)
        }

        // 4) 藏光标（结束时恢复）。这是唯一"好看"所必需的 ANSI 动作，
        //    也是最容易忘记恢复的那个。
        FileHandle.standardOutput.write(Data("\u{1B}[?25l".utf8))
        cursorHidden = true
        print("")   // 占一行，之后所有定位都相对于这一行
    }

    deinit { restoreTerminalOnce() }

    /// 幂等：正常退出、deinit、atexit 都可能调它，第二次起是空操作。
    private func restoreTerminalOnce() {
        guard cursorHidden else { return }
        cursorHidden = false
        if termiosSaved, var t = savedTermios {
            _ = tcsetattr(STDIN_FILENO, TCSANOW, &t)
            termiosSaved = false
        }
        FileHandle.standardOutput.write(Data("\u{1B}[?25h\n".utf8))
    }

    /// 优雅退出：把光标显出来、面板留在原地（不清屏 —— 用户可能想回看最后几帧）。
    public func finish(note: String) {
        guard !finished else { return }
        finished = true
        restoreTerminalOnce()
        print(note)
    }

    // MARK: - 渲染

    public func render(_ p: PoseReading, status: Status) {
        // 每帧都查 isatty：用户中途把 stdout 重定向成管道的话，
        // 从这里开始**什么都不能画**（上层会在下一次 tick 发现并中止）。
        guard isatty(STDOUT_FILENO) == 1 else { return }

        let w = effectiveWidth
        var out = "\u{1B}[H"          // 回到左上角（不清屏，避免闪烁）
        out.reserveCapacity(1200)

        let okTag = status.healthy ? "STREAMING 数据有效" : "NO DATA 无有效数据"
        let color = status.healthy ? "\u{1B}[32m" : "\u{1B}[33m"   // 绿 / 黄
        let dim = "\u{1B}[2m"
        let bold = "\u{1B}[1m"
        let reset = "\u{1B}[0m"

        /// 追加一行。`\e[K` = 清到行尾 —— 上一帧这一行更长时不会留残影。
        func add(_ s: String) { out += s + "\u{1B}[K\n" }

        add("\(bold)ListenLook\(reset) source=airpods protocol=v1")
        add("target \(status.target)  \(okTag)\(reset)")
        add("auth \(status.authText)")
        add("state \(color)\(status.statusText)\(reset)")
        if !status.deviceName.isEmpty { add("device \(status.deviceName)") }
        var stat = String(format: "rate %.1f Hz", finite(status.hz))
        stat += "   sent \(status.sentCount)   seq \(status.seq)"
        stat += String(format: "   up %.0fs", finite(status.uptime))
        if status.errorCount > 0 { stat += "   sendErr \(status.errorCount)" }
        add(stat)
        if !status.sendError.isEmpty { add("\(color)send: \(status.sendError)\(reset)") }
        add("")
        add("        value(deg)      -90 (left/down)   0   +90 (right/up)")
        for line in barLines(p, color: color, dim: dim, reset: reset, width: w) { add(line) }
        add("")
        if !status.debugLine.isEmpty { add("\(dim)raw \(truncate(status.debugLine, w - 5))\(reset)") }
        add("\(bold)[r]\(reset) 归零 recenter      \(bold)[q]\(reset) 退出 quit")

        // 面板变短时清掉上一帧多余的行，否则下面会残留旧内容。
        let lines = out.reduce(into: 0) { n, ch in if ch == "\n" { n += 1 } }
        var extra = drawnLines - lines
        while extra > 0 { out += "\u{1B}[K\n"; extra -= 1 }
        drawnLines = max(lines, drawnLines)

        FileHandle.standardOutput.write(Data(out.utf8))
    }

    /// 三条轴各自两行：数值行 + 条形行。
    /// 返回值而不是就地拼进 `out`：`out` 是 `inout`，闭包捕获它会把
    /// 独占访问检查搅乱（Swift 会报 overlapping access）。
    private func barLines(_ p: PoseReading, color: String, dim: String,
                          reset: String, width w: Int) -> [String] {
        let axes: [(String, String, Double)] = [
            ("yaw  偏航", "(+ = 向右转 turn right)",   p.yawDeg),
            ("pitch 俯仰", "(+ = 抬头 look up)",        p.pitchDeg),
            ("roll  翻滚", "(+ = 向右歪 tilt right)",  p.rollDeg),
        ]
        var lines: [String] = []
        for (label, sub, raw) in axes {
            let v = finite(raw)                       // ← 进 format 前唯一的闸门
            let head = "\(label) \(sub)"
            let valueText = p.available ? String(format: "%+8.1f", v) : "   --.--"
            lines.append("\(truncate(head, w - 11)) \(valueText)")
            let bar = barString(v, width: max(10, w - 3), valid: p.available)
            lines.append("  \(p.available ? color : dim)\(bar)\(reset)")
        }
        return lines
    }

    private func barString(_ value: Double, width: Int, valid: Bool) -> String {
        guard valid else { return String(repeating: "·", count: width) }
        var chars = Array(repeating: Character("-"), count: width)
        // -90° → 第 0 格，0° → 正中，+90° → 最后一格。
        let norm = max(-1.0, min(1.0, value / Self.fullScaleDeg))
        let center = (width - 1) / 2
        let pos = center + Int((norm * Double(center)).rounded())
        chars[center] = "|"
        chars[max(0, min(width - 1, pos))] = "#"
        return String(chars)
    }

    // MARK: - 小工具

    /// NaN / ±inf → 0。**所有**要格式化的数字都必须先过这里。
    private func finite(_ v: Double) -> Double { v.isFinite ? v : 0 }

    /// 按显示宽度截断。中文按 2 列算 —— 不这么算的话中文行会先把终端撑到换行，
    /// 之后所有行的相对定位就全错了。
    private func truncate(_ s: String, _ cols: Int) -> String {
        guard cols > 1 else { return "" }
        var used = 0
        var out = ""
        for ch in s {
            let cw = Self.displayWidth(ch)
            if used + cw > cols { break }
            out.append(ch); used += cw
        }
        return out
    }

    private static func displayWidth(_ ch: Character) -> Int {
        guard let scalar = ch.unicodeScalars.first else { return 1 }
        let v = scalar.value
        // CJK 统一表意文字、全角标点、CJK 符号、假名、谚文、emoji —— 终端里占两列。
        if (0x1100...0x115F).contains(v) || (0x2E80...0xA4CF).contains(v)
            || (0xAC00...0xD7A3).contains(v) || (0xF900...0xFAFF).contains(v)
            || (0xFE30...0xFE6F).contains(v) || (0xFF00...0xFF60).contains(v)
            || (0xFFE0...0xFFE6).contains(v) || (0x1F300...0x1FAFF).contains(v) {
            return 2
        }
        return 1
    }
}
