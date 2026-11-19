import Cocoa
import Carbon.HIToolbox
import SwiftUI
import ServiceManagement

// MARK: - 数据模型

struct ClipItem: Codable, Identifiable, Equatable {
    var id = UUID()
    var text: String
    var date: Date
    var isPinned: Bool = false
}

// MARK: - 数据存储（本地 SQLite 风格：JSON 文件，纯本地不上云）

final class ClipStore: ObservableObject {
    static let shared = ClipStore()

    @Published var items: [ClipItem] = []

    let dataDir: URL
    private let fileURL: URL
    private let ioQueue = DispatchQueue(label: "clipstack.store")
    private var savePending = false

    init() {
        let base = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/ClipStack", isDirectory: true)
        dataDir = base
        fileURL = base.appendingPathComponent("history.json")
        try? FileManager.default.createDirectory(at: base, withIntermediateDirectories: true)
        load()
    }

    private func load() {
        guard let data = try? Data(contentsOf: fileURL),
              let list = try? JSONDecoder().decode([ClipItem].self, from: data) else { return }
        items = list
    }

    func save() {
        let snapshot = items
        ioQueue.async { [fileURL] in
            let enc = JSONEncoder()
            enc.outputFormatting = [.prettyPrinted, .sortedKeys]
            guard let data = try? enc.encode(snapshot) else { return }
            // 原子写，避免崩溃时留下半截文件
            try? data.write(to: fileURL, options: .atomic)
            try? FileManager.default.setAttributes([.posixPermissions: 0o600],
                                                   ofItemAtPath: fileURL.path)
            NSLog("ClipStack: 已保存 \(snapshot.count) 条")
        }
    }

    func add(_ text: String) {
        guard !isPaused else { return }
        DispatchQueue.main.async {
            if let idx = self.items.firstIndex(where: { $0.text == text }) {
                var moved = self.items.remove(at: idx)
                moved.date = Date()
                // 位置不变（不改变历史顺序的其他条目），只更新时间戳
                self.items.insert(moved, at: min(idx, self.items.count))
            } else {
                self.items.insert(ClipItem(text: text, date: Date()), at: 0)
            }
            self.trim()
            self.save()
        }
    }

    /// 超过上限时从最旧的未置顶条目开始删。
    /// limit < 0 表示无限，不做裁剪；limit == 0 表示暂停记录，也不裁剪（暂停时压根不写入）
    private func trim() {
        let limit = currentMax
        guard limit > 0, items.count > limit else { return }
        var idx = items.count - 1
        while items.count > limit, idx >= 0 {
            if idx < items.count, !items[idx].isPinned { items.remove(at: idx) }
            idx -= 1
        }
        if items.count > limit { items = Array(items.prefix(limit)) }
    }

    func clearAll(keepingPinned: Bool = false) {
        if keepingPinned {
            items.removeAll { !$0.isPinned }
        } else {
            items.removeAll()
        }
        save()
    }

    func remove(_ item: ClipItem) {
        items = items.filter { $0.id != item.id }
        save()
    }

    func togglePin(_ item: ClipItem) {
        guard let idx = items.firstIndex(where: { $0.id == item.id }) else { return }
        items[idx].isPinned.toggle()
        save()
    }

    func moveToTop(_ text: String) {
        guard let idx = items.firstIndex(where: { $0.text == text }) else { return }
        var it = items.remove(at: idx)
        it.date = Date()
        items.insert(it, at: 0)
        save()
    }

    /// 搜索：不区分大小写，标题命中优先
    func search(_ q: String) -> [ClipItem] {
        let key = q.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard !key.isEmpty else { return items }
        let hits = items.enumerated().filter { $0.element.text.lowercased().contains(key) }
        return hits.sorted { a, b in
            let al = a.element.text.lowercased().hasPrefix(key) ? 0 : 1
            let bl = b.element.text.lowercased().hasPrefix(key) ? 0 : 1
            if al != bl { return al < bl }
            return a.offset < b.offset
        }.map { $0.element }
    }

    /// 历史上限。-1 = 无限；0 = 暂停记录；其余为条数上限
    var currentMax: Int {
        UserDefaults.standard.object(forKey: "maxItems") as? Int ?? 200
    }

    /// 是否暂停记录
    var isPaused: Bool { currentMax == 0 }
}

// MARK: - 全局快捷键注册（Carbon，稳定且不需要辅助功能权限）

final class HotKeyCenter {
    static let shared = HotKeyCenter()
    private var handlers: [UInt32: () -> Void] = [:]
    private var refs: [EventHotKeyRef] = []
    private var nextID: UInt32 = 1
    private var installed = false

    func register(keyCode: UInt32, modifiers: UInt32, handler: @escaping () -> Void) -> UInt32 {
        installHandlerIfNeeded()
        let id = nextID
        nextID += 1
        var ref: EventHotKeyRef?
        let hkID = EventHotKeyID(signature: OSType(0x434C4950), id: id) // 'CLIP'
        let status = RegisterEventHotKey(keyCode, modifiers, hkID, GetApplicationEventTarget(), 0, &ref)
        if status == noErr, let r = ref {
            refs.append(r)
            handlers[id] = handler
            return id
        }
        NSLog("ClipStack: 注册快捷键失败 id=\(id) status=\(status)")
        return 0
    }

    private func installHandlerIfNeeded() {
        guard !installed else { return }
        installed = true
        var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard),
                                 eventKind: UInt32(kEventHotKeyPressed))
        InstallEventHandler(GetApplicationEventTarget(), { _, event, _ -> OSStatus in
            var hkID = EventHotKeyID()
            GetEventParameter(event, EventParamName(kEventParamDirectObject),
                              EventParamType(typeEventHotKeyID), nil,
                              MemoryLayout<EventHotKeyID>.size, nil, &hkID)
            HotKeyCenter.shared.handlers[hkID.id]?()
            return noErr
        }, 1, &spec, nil, nil)
    }
}

// MARK: - 剪贴板监听

final class ClipMonitor {
    private var timer: Timer?
    private var lastChangeCount: Int = NSPasteboard.general.changeCount
    /// 粘贴我们自己写入的内容时，避免把它当成"新复制"再存一条
    private var suppressCount: Int = -1

    func start() {
        lastChangeCount = NSPasteboard.general.changeCount
        timer = Timer.scheduledTimer(withTimeInterval: 0.30, repeats: true) { [weak self] _ in
            self?.poll()
        }
        RunLoop.main.add(timer!, forMode: .common)
    }

    private func poll() {
        let pb = NSPasteboard.general
        let cc = pb.changeCount
        guard cc != lastChangeCount else { return }
        lastChangeCount = cc
        if cc == suppressCount { return }
        guard let s = pb.string(forType: .string), !s.isEmpty else { return }
        ClipStore.shared.add(s)
    }

    /// 把内容写到剪贴板，并标记这不是用户的新复制
    func writeToPasteboard(_ text: String) {
        let pb = NSPasteboard.general
        pb.clearContents()
        pb.setString(text, forType: .string)
        suppressCount = pb.changeCount
        lastChangeCount = pb.changeCount
    }
}

// MARK: - 粘贴到当前焦点位置

final class Paster {
    /// 向系统投递 ⌘V（HID 层，实测 TextEdit/原生 App 可正常接收）
    @discardableResult
    /// 向系统投递 ⌘V
    static func pasteFromClipboard() -> Bool {
        guard let src = CGEventSource(stateID: .hidSystemState),
              let down = CGEvent(keyboardEventSource: src, virtualKey: 9, keyDown: true),
              let up = CGEvent(keyboardEventSource: src, virtualKey: 9, keyDown: false) else {
            return false
        }
        down.flags = .maskCommand
        up.flags = .maskCommand
        down.post(tap: .cghidEventTap)
        usleep(30_000)
        up.post(tap: .cghidEventTap)
        return true
    }

    /// 直接往目标 App 的焦点控件里插入文本（不依赖键盘事件，最可靠）
    /// 优先用 kAXSelectedText 插入；失败再回落 AXValue 追加
    static func insertViaAX(_ text: String, into app: NSRunningApplication) -> Bool {
        func trace(_ m: String) {
            NSLog("ClipStack[AX]: %@", m)
        }
        let axApp = AXUIElementCreateApplication(app.processIdentifier)
        var focused: CFTypeRef?
        let ev = AXUIElementCopyAttributeValue(axApp, kAXFocusedUIElementAttribute as CFString, &focused)
        guard ev == .success, let el = focused else {
            trace("取焦点元素失败 err=\(ev.rawValue) pid=\(app.processIdentifier) name=\(app.localizedName ?? "?")")
            return false
        }
        let element = el as! AXUIElement
        var role: CFTypeRef?
        AXUIElementCopyAttributeValue(element, kAXRoleAttribute as CFString, &role)
        trace("焦点元素 role=\(role ?? "nil" as CFTypeRef)")

        let e1 = AXUIElementSetAttributeValue(element, kAXSelectedTextAttribute as CFString, text as CFTypeRef)
        trace("SetSelectedText err=\(e1.rawValue)")
        if e1 == .success { return true }

        var cur: CFTypeRef?
        let rv = AXUIElementCopyAttributeValue(element, kAXValueAttribute as CFString, &cur)
        trace("读 AXValue err=\(rv.rawValue) 现有长度=\((cur as? String)?.count ?? -1)")
        if rv == .success {
            let old = (cur as? String) ?? ""
            let e2 = AXUIElementSetAttributeValue(element, kAXValueAttribute as CFString, (old + text) as CFTypeRef)
            trace("SetAXValue err=\(e2.rawValue)")
            if e2 == .success { return true }
        }
        return false
    }

    /// 通过剪贴板通道粘贴：写剪贴板 → 等目标回前台 → 投 ⌘V → 等目标读完 → 还原原剪贴板
    static func paste(_ text: String, monitor: ClipMonitor, target: NSRunningApplication?, attempt: Int = 0) {
        let original = originalClipboard
        func trace(_ m: String) {
            NSLog("ClipStack[paste]: %@", m)
        }
        trace("paste 开始 text=\(text.prefix(16)) front=\(NSWorkspace.shared.frontmostApplication?.localizedName ?? "nil") target=\(target?.localizedName ?? "nil")")

        // 确认目标 App 已回到前台（最多重试 20 次 ≈ 1 秒）
        let front = NSWorkspace.shared.frontmostApplication
        let ours = Bundle.main.bundleIdentifier
        if let target = target, front?.bundleIdentifier != target.bundleIdentifier,
           front?.bundleIdentifier == ours, attempt < 20 {
            target.activate(options: [])
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) {
                paste(text, monitor: monitor, target: target, attempt: attempt + 1)
            }
            return
        }

        // 目标已就位
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.02) {
            // 方案 A：AX 直接插入 —— 完全不碰剪贴板，天然不污染"最近一条"
            let dest = target ?? NSWorkspace.shared.frontmostApplication
            if let d = dest, insertViaAX(text, into: d) {
                trace("✅ AX 插入成功，剪贴板未改动")
                originalClipboard = nil
                return
            }

            // 方案 B：走剪贴板 + 合成 ⌘V，之后再还原
            trace("AX 失败，改用剪贴板通道")
            monitor.writeToPasteboard(text)
            pasteFromClipboard()
            // 关键：给目标 App 足够时间去读剪贴板，再还原（实测 <1s 会被抢先）
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.2) {
                if let original = original, !original.isEmpty {
                    monitor.writeToPasteboard(original)
                }
                originalClipboard = nil
            }
        }
    }

    /// 用户原来的剪贴板内容，粘贴前记下来
    static var originalClipboard: String?
}

// MARK: - 面板（NSPanel + SwiftUI）

/// 默认的 NSPanel 里 SwiftUI 的 TextField 拿不到键盘焦点，这里放开 canBecomeKey
final class KeyPanel: NSPanel {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }

    var onEsc: (() -> Void)?

    /// borderless 面板不会派发 cancelOperation，这里保证 Esc 总能关掉
    override func cancelOperation(_ sender: Any?) {
        onEsc?()
    }

    override func keyDown(with event: NSEvent) {
        if PanelController.shared.handleKey(event) { return }
        super.keyDown(with: event)
    }
}

final class PanelController {
    static let shared = PanelController()
    private var panel: KeyPanel?
    private var previousApp: NSRunningApplication?
    private var activeMonitor: ClipMonitor?

    var isVisible: Bool { panel?.isVisible ?? false }

    private var keyMonitor: Any?
    /// 由 ClipListView 注入的操作句柄
    var moveSelection: (Int) -> Void = { _ in }
    var confirmSelection: () -> Void = {}
    var deleteSelection: () -> Void = {}
    var selectAndPaste: (Int) -> Void = { _ in }
    var searchFocused: Bool = false

    func toggle() {
        if isVisible { hide() } else { show() }
    }

    /// 面板可见时用本地事件监听器接管键盘（borderless 面板 firstResponder 不可靠）
    private func installKeyMonitor() {
        removeKeyMonitor()
        keyMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
            guard let self = self, self.isVisible else { return event }
            // 搜索框有焦点时不拦截，让用户正常打字
            if self.searchFocused { return event }
            return self.handleKey(event) ? nil : event
        }
    }

    private func removeKeyMonitor() {
        if let m = keyMonitor { NSEvent.removeMonitor(m); keyMonitor = nil }
    }

    /// 返回 true 表示已消费该按键
    func handleKey(_ event: NSEvent) -> Bool {
        guard let p = panel, p.isVisible else { return false }
        let flags = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
        switch event.keyCode {
        case 53: hide(); return true                       // Esc
        case 126: moveSelection(-1); return true         // ↑
        case 125: moveSelection(1); return true          // ↓
        case 36, 76: confirmSelection(); return true     // ⏎
        case 51:
            if flags.contains(.option) { deleteSelection(); return true }
        default: break
        }
        if flags.isEmpty, let ch = event.charactersIgnoringModifiers,
           let n = Int(ch), n >= 1, n <= 9 {
            selectAndPaste(n - 1)
            return true
        }
        return false
    }

    func show() {
        activeMonitor = MonitorRef.shared.monitor
        previousApp = NSWorkspace.shared.frontmostApplication
        // 记下用户原来的剪贴板，粘贴后还原
        Paster.originalClipboard = NSPasteboard.general.string(forType: .string)

        let view = ClipListView(onPick: { [weak self] text in
            NSLog("ClipStack[onPick-TEST]: 收到 \(text.prefix(20))")
            if ProcessInfo.processInfo.environment["CS_TEST_MODE"] != "1" {
                self?.hide(restoreFocus: true)
                guard let mon = self?.activeMonitor else { return }
                let target = self?.previousApp
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) {
                    Paster.paste(text, monitor: mon, target: target)
                }
            }
        }, onClose: { [weak self] in self?.hide() })

        let hosting = NSHostingView(rootView: view)
        hosting.layoutSubtreeIfNeeded()

        let size = NSSize(width: 440, height: 340)
        let p: KeyPanel
        if let existing = panel {
            existing.contentView = hosting
            p = existing
        } else {
            let rect = NSRect(origin: .zero, size: size)
            // 用 borderless 才能让 SwiftUI 的圆角真正生效，也不会多出一条标题栏
            p = KeyPanel(contentRect: rect,
                         styleMask: [.borderless],
                         backing: .buffered, defer: false)
            p.isFloatingPanel = true
            p.level = .screenSaver // 置顶显示，浮在所有普通窗口之上
            p.hidesOnDeactivate = false
            p.isReleasedWhenClosed = false
            p.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
            p.backgroundColor = .clear
            p.isOpaque = false
            p.hasShadow = true
            p.onEsc = { [weak self] in self?.hide() }
            p.contentView = hosting
            panel = p
        }

        // 固定显示在「非主屏」的右上角，避免遮挡主屏办公区
        let targetScreen = NSScreen.screens.first { $0 != NSScreen.main } ?? NSScreen.main!
        let sf = targetScreen.visibleFrame
        let x = sf.maxX - size.width - 16
        let y = sf.maxY - size.height - 16
        p.setFrame(NSRect(x: x.rounded(), y: y.rounded(),
                          width: size.width, height: size.height), display: true)

        // 用 NSRunningApplication 激活自己（比 NSApp.activate 在 accessory 模式下更可靠）
        NSRunningApplication.current.activate(options: [.activateAllWindows])
        installKeyMonitor()
        // 顺序很重要：先激活 App，再 makeKeyAndOrderFront，面板才能成为 key window
        NSApp.setActivationPolicy(.regular)
        NSRunningApplication.current.activate(options: [.activateAllWindows])
        DispatchQueue.main.async {
            p.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
        }

        // 强制重绘一帧，避免首次显示空白
        hosting.needsLayout = true
        hosting.needsDisplay = true
    }

    func hide(restoreFocus: Bool = false) {
        removeKeyMonitor()
        panel?.orderOut(nil)
        guard restoreFocus else { return }
        // 关键：把自己的激活状态让出去，否则 ⌘V 会发到 ClipStack 自己身上
        if let app = previousApp, app.bundleIdentifier != Bundle.main.bundleIdentifier {
            app.activate(options: [])
            NSApp.deactivate()
        }
    }
}

// MARK: - SwiftUI 列表

final class MonitorRef: ObservableObject {
    static let shared = MonitorRef()
    let monitor = ClipMonitor()
}

struct ClipListView: View {
    @ObservedObject var store = ClipStore.shared
    @State private var query: String = ""
    @State private var sel: Int = 0
    /// 多选集合（存条目 id，避免删除后下标错位）
    @State private var multiSel: Set<UUID> = []
    @FocusState private var searchFocused: Bool

    let onPick: (String) -> Void
    let onClose: () -> Void

    private var results: [ClipItem] { store.search(query) }

    /// 带序号的快照，供列表渲染与点击使用
    private var rows: [(index: Int, item: ClipItem)] {
        results.enumerated().map { (index: $0.offset, item: $0.element) }
    }

    var body: some View {
        VStack(spacing: 0) {
            header
            listBody
            Divider()
            footer
        }
        .frame(width: 440, height: 340)
        .background(VisualEffectView(material: .popover, blending: .behindWindow))
        .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .strokeBorder(Color.primary.opacity(0.10), lineWidth: 1)
        )
        .background(
            KeyCatcher(
                onUp: { move(-1) },
                onDown: { move(1) },
                onEnter: { if multiSel.isEmpty { pick(sel) } else { pasteMulti() } },
                onEsc: { onClose() },
                onCmd: { n in pick(n) },
                onDelete: { deleteSelected() }
            )
        )
        .onAppear {
            query = ""
            sel = 0
            // 把操作句柄交给 PanelController，供本地事件监听器调用
            let c = PanelController.shared
            c.moveSelection = { d in sel = (sel + d + max(results.count, 1)) % max(results.count, 1) }
            c.confirmSelection = { if multiSel.isEmpty { pick(sel) } else { pasteMulti() } }
            c.deleteSelection = { deleteSelected() }
            c.selectAndPaste = { n in multiSel.removeAll(); pick(n) }
            // 不自动聚焦搜索框：让数字键/方向键直接作用于列表。
            // 用户手动点击搜索框时才开始输入并筛选。
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.02) { searchFocused = false }
        }
    }

    private var header: some View {
        HStack(spacing: 8) {
            Image(systemName: "magnifyingglass")
                .font(.system(size: 12))
                .foregroundStyle(.secondary)
            TextField("搜索历史…", text: $query)
                .textFieldStyle(.plain)
                .onTapGesture { searchFocused = true }
                .font(.system(size: 13.5))
                .focused($searchFocused)
                .onSubmit { pick(sel) }
                .onChange(of: query) { _, _ in sel = 0 }
                .onChange(of: searchFocused) { _, v in PanelController.shared.searchFocused = v }
            if !query.isEmpty {
                Button {
                    query = ""
                    searchFocused = true
                } label: {
                    Image(systemName: "xmark.circle.fill")
                        .font(.system(size: 11))
                        .foregroundStyle(.tertiary)
                }
                .buttonStyle(.plain)
            }
            Divider().frame(height: 14)
            CloseButton(action: onClose)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 9)
    }

    @ViewBuilder
    private var listBody: some View {
        if results.isEmpty {
            VStack(spacing: 6) {
                Image(systemName: "doc.on.clipboard")
                    .font(.system(size: 28))
                    .foregroundStyle(.tertiary)
                Text(query.isEmpty ? "还没有复制记录" : "没有匹配的内容")
                    .foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        } else {
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(spacing: 2) {
                        ForEach(rows, id: \.item.id) { row in
                            ClipRow(item: row.item,
                                    index: row.index,
                                    selected: row.index == sel,
                                    checked: multiSel.contains(row.item.id),
                                    multiMode: !multiSel.isEmpty)
                                .contentShape(Rectangle())
                                .onTapGesture { handleTap(row) }
                                .onHover { h in if h { sel = row.index } }
                                .id(row.item.id)
                        }
                    }
                    .padding(.vertical, 6)
                    .padding(.horizontal, 8)
                }
                .onChange(of: sel) { _, new in
                    let items = rows
                    if items.indices.contains(new) {
                        withAnimation(.easeOut(duration: 0.1)) {
                            proxy.scrollTo(items[new].item.id, anchor: .center)
                        }
                    }
                }

            }
        }
    }

    private var footer: some View {
        HStack(spacing: 12) {
            if multiSel.isEmpty {
                Text("\(store.items.count) 条")
                Text("↑↓ 选择 · ⏎ 粘贴 · 1-9 快选 · ⌘点击多选 · ⌥⌫ 删除")
                    .lineLimit(1)
            } else {
                Text("已选 \(multiSel.count) 条")
                    .foregroundStyle(Color.accentColor)
                Text("⏎ 合并粘贴 · ⌥⌫ 批量删除 · Esc 取消")
                    .lineLimit(1)
            }
            Spacer(minLength: 4)
            Button("清空") { store.clearAll(keepingPinned: true) }
                .buttonStyle(.plain)
            Button("设置") { openSettings() }
                .buttonStyle(.plain)
        }
        .font(.system(size: 10.5))
        .foregroundStyle(.secondary)
        .padding(.horizontal, 12)
        .padding(.vertical, 6)
    }

    // MARK: 操作

    private func move(_ d: Int) {
        guard !results.isEmpty else { return }
        sel = (sel + d + results.count) % results.count
    }

    /// 直接用文本触发，不依赖数组下标
    private func pickText(_ text: String) {
        NSLog("ClipStack[pickText]: \(text.prefix(24))")
        DispatchQueue.main.async { [onPick] in
            onPick(text)
        }
    }

    private func pick(_ index: Int) {
        let snapshot = results
        guard snapshot.indices.contains(index) else { return }
        let text = snapshot[index].text
        // 关键：不能在 SwiftUI 的事件处理内同步 orderOut 面板，
        // 否则视图树在渲染中途被销毁 → AttributeGraph 环 + 崩溃。
        // 统一推到下一轮 runloop 再执行。
        NSLog("ClipStack[pick]: 选中 \(text.prefix(20))")
        DispatchQueue.main.async {
            NSLog("ClipStack[pick]: 调用 onPick")
            onPick(text)
        }
    }

    private func deleteSelected() {
        // 多选模式：批量删除
        if !multiSel.isEmpty {
            for item in store.items where multiSel.contains(item.id) {
                store.remove(item)
            }
            multiSel.removeAll()
            sel = 0
            return
        }
        guard results.indices.contains(sel) else { return }
        store.remove(results[sel])
        sel = max(0, min(sel, results.count - 1))
    }

    /// 点击行为：普通点击 = 直接粘贴；⌘/⇧ 点击 = 多选
    private func handleTap(_ row: (index: Int, item: ClipItem)) {
        let flags = NSEvent.modifierFlags
        if flags.contains(.command) || flags.contains(.shift) {
            // 切换勾选
            if multiSel.contains(row.item.id) {
                multiSel.remove(row.item.id)
            } else {
                multiSel.insert(row.item.id)
            }
            sel = row.index
            return
        }
        // 多选模式下的普通点击也算勾选（避免误粘贴）
        if !multiSel.isEmpty {
            if multiSel.contains(row.item.id) {
                multiSel.remove(row.item.id)
            } else {
                multiSel.insert(row.item.id)
            }
            return
        }
        pickText(row.item.text)
    }

    /// 批量粘贴：按列表顺序，用换行拼接
    private func pasteMulti() {
        let ordered = results.filter { multiSel.contains($0.id) }.map(\.text)
        guard !ordered.isEmpty else { return }
        pickText(ordered.joined(separator: "\n"))
    }

    private func openSettings() {
        SettingsWindowController.shared.show()
    }
}

struct ClipRow: View {
    let item: ClipItem
    let index: Int
    let selected: Bool
    var checked: Bool = false
    var multiMode: Bool = false

    private var preview: String {
        let t = item.text.replacingOccurrences(of: "\r", with: " ")
        let flat = t.replacingOccurrences(of: "\n", with: " ⏎ ")
        return flat.count > 160 ? String(flat.prefix(160)) + "…" : flat
    }

    private var isLink: Bool {
        let t = item.text.trimmingCharacters(in: .whitespacesAndNewlines)
        return t.hasPrefix("http://") || t.hasPrefix("https://")
    }

    var body: some View {
        HStack(spacing: 10) {
            if multiMode {
                Image(systemName: checked ? "checkmark.circle.fill" : "circle")
                    .font(.system(size: 13))
                    .foregroundStyle(checked ? Color.accentColor : Color.secondary.opacity(0.5))
                    .frame(width: 16)
            } else {
                Text("\(index + 1)")
                    .font(.system(size: 10, weight: .medium, design: .monospaced))
                    .foregroundStyle(selected ? Color.white.opacity(0.75) : Color.secondary.opacity(0.55))
                    .frame(width: 18, alignment: .trailing)
            }

            Image(systemName: isLink ? "link" : "doc.text")
                .font(.system(size: 11))
                .foregroundStyle(selected ? Color.white.opacity(0.8) : Color.secondary.opacity(0.6))
                .frame(width: 14)

            Text(preview)
                .font(.system(size: 12.5))
                .foregroundStyle(selected ? Color.white : Color.primary)
                .lineLimit(2)
                .multilineTextAlignment(.leading)
                .frame(maxWidth: .infinity, alignment: .leading)

            if item.isPinned {
                Image(systemName: "pin.fill")
                    .font(.system(size: 9))
                    .foregroundStyle(selected ? Color.white.opacity(0.8) : Color.orange)
            }
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 7)
        .background(
            RoundedRectangle(cornerRadius: 6, style: .continuous)
                .fill(checked ? Color.accentColor.opacity(0.18)
                      : selected ? Color.accentColor : Color.clear)
        )
    }
}

// MARK: - 键盘事件桥（SwiftUI 里可靠捕获方向键）

struct KeyCatcher: NSViewRepresentable {
    var onUp: () -> Void
    var onDown: () -> Void
    var onEnter: () -> Void
    var onEsc: () -> Void
    var onCmd: (Int) -> Void
    var onDelete: () -> Void

    final class CatcherView: NSView {
        var onUp: () -> Void = {}
        var onDown: () -> Void = {}
        var onEnter: () -> Void = {}
        var onEsc: () -> Void = {}
        var onCmd: (Int) -> Void = { _ in }
        var onDelete: () -> Void = {}

        override var acceptsFirstResponder: Bool { true }

        override func viewDidMoveToWindow() {
            super.viewDidMoveToWindow()
            grabFocus()
        }

        /// 延后多次尝试抢焦点，确保面板激活后按键能进来
        func grabFocus(retry: Int = 0) {
            guard let w = window else { return }
            if w.firstResponder !== self {
                w.makeFirstResponder(self)
            }
            if retry < 5 {
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.04) { [weak self] in
                    self?.grabFocus(retry: retry + 1)
                }
            }
        }

        /// borderless 面板不会自动派发 cancelOperation，这里手动接住 Esc
        override func cancelOperation(_ sender: Any?) {
            onEsc()
        }

        override func keyDown(with event: NSEvent) {
            let flags = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
            // Esc 不论焦点在搜索框还是列表都能关
            if event.keyCode == 53 { onEsc(); return }
            switch event.keyCode {
            case 126: onUp(); return           // ↑
            case 125: onDown(); return         // ↓
            case 36, 76: onEnter(); return     // Return / Enter
            case 51:                            // Delete
                if flags.contains(.option) { onDelete(); return }
            default: break
            }
            // 直接按数字键 = 选中并粘贴第 N 条（无需 Cmd）
            if flags.isEmpty || flags == .numericPad,
               let ch = event.charactersIgnoringModifiers,
               let n = Int(ch), n >= 1, n <= 9 {
                onCmd(n - 1)
                return
            }
            super.keyDown(with: event)
        }
    }

    func makeNSView(context: Context) -> CatcherView {
        let v = CatcherView()
        v.onUp = onUp; v.onDown = onDown; v.onEnter = onEnter
        v.onEsc = onEsc; v.onCmd = onCmd; v.onDelete = onDelete
        DispatchQueue.main.async {
            v.window?.makeFirstResponder(v)
        }
        return v
    }

    func updateNSView(_ v: CatcherView, context: Context) {
        v.onUp = onUp; v.onDown = onDown; v.onEnter = onEnter
        v.onEsc = onEsc; v.onCmd = onCmd; v.onDelete = onDelete
    }
}

// MARK: - 关闭按钮

struct CloseButton: View {
    let action: () -> Void
    @State private var hovering = false

    var body: some View {
        Button(action: action) {
            Image(systemName: "xmark")
                .font(.system(size: 9, weight: .bold))
                .foregroundStyle(hovering ? Color.white : Color.secondary)
                .frame(width: 17, height: 17)
                .background(
                    Circle().fill(hovering ? Color.red.opacity(0.85) : Color.primary.opacity(0.09))
                )
                .contentShape(Circle())
        }
        .buttonStyle(.plain)
        .keyboardShortcut(.cancelAction) // Esc 也能关
        .onHover { hovering = $0 }
        .help("关闭 (Esc)")
    }
}

/// 毛玻璃背景，比 SwiftUI 的 .regularMaterial 更贴近系统面板
struct VisualEffectView: NSViewRepresentable {
    var material: NSVisualEffectView.Material = .popover
    var blending: NSVisualEffectView.BlendingMode = .behindWindow

    func makeNSView(context: Context) -> NSVisualEffectView {
        let v = NSVisualEffectView()
        v.material = material
        v.blendingMode = blending
        v.state = .active
        return v
    }

    func updateNSView(_ v: NSVisualEffectView, context: Context) {
        v.material = material
        v.blendingMode = blending
    }
}

// MARK: - 设置窗口

final class SettingsWindowController {
    static let shared = SettingsWindowController()
    private var win: NSWindow?

    func show() {
        if let w = win {
            w.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
            return
        }
        let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 420, height: 330),
                         styleMask: [.titled, .closable],
                         backing: .buffered, defer: false)
        w.title = "ClipStack 设置"
        w.isReleasedWhenClosed = false
        w.center()
        w.contentView = NSHostingView(rootView: SettingsView())
        win = w
        w.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }
}

struct SettingsView: View {
    @AppStorage("maxItems") private var maxItems: Int = -1
    @AppStorage("pasteRestore") private var pasteRestore: Bool = true
    @State private var launchAtLogin: Bool = false
    @State private var saved = false

    var body: some View {
        Form {
            Section("最多保留条数") {
                Picker("", selection: $maxItems) {
                    Text("无限").tag(-1)
                    Text("100 条").tag(100)
                    Text("200 条").tag(200)
                    Text("500 条").tag(500)
                    Text("1000 条").tag(1000)
                    Text("暂停记录").tag(0)
                }
                .pickerStyle(.menu)
                .labelsHidden()
                Text(maxItems == -1
                     ? "不自动清理，历史会一直累积。磁盘占用很小（纯文本），但列表会变长。"
                     : maxItems == 0
                     ? "暂停记录新内容，已有历史保留。"
                     : "超出后自动删除最旧的未置顶条目。置顶条目不计入上限。")
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Section("粘贴行为") {
                Toggle("粘贴后还原剪贴板内容", isOn: $pasteRestore)
                Text("开启后，选取历史粘贴不会顶掉你 Cmd+V 用的最近一条。")
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
            }
            Section("启动") {
                Toggle("开机自动启动", isOn: $launchAtLogin)
                    .onChange(of: launchAtLogin) { _, v in LaunchAtLogin.set(v) }
                Text(LaunchAtLogin.statusText)
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
            }
            Section("快捷键") {
                Text("Shift + Command + C  唤起 / 关闭历史面板")
                Text("Command + V  最近一条（系统原生）")
                    .foregroundStyle(.secondary)
            }
            Section("数据位置") {
                HStack {
                    Text(ClipStore.shared.dataDir.path)
                        .font(.system(size: 10, design: .monospaced))
                        .textSelection(.enabled)
                        .foregroundStyle(.secondary)
                    Spacer()
                    Button("打开") {
                        NSWorkspace.shared.open(ClipStore.shared.dataDir)
                    }
                }
            }
        }
        .formStyle(.grouped)
        .onAppear { launchAtLogin = LaunchAtLogin.isEnabled }
    }
}

// MARK: - 开机自启

/// macOS 13+ 用官方 SMAppService；旧系统或注册失败时降级到 LaunchAgent。
/// 全部路径都无需管理员权限。
enum LaunchAtLogin {
    static let label = "com.tal.ClipStack"

    private static var agentsDir: URL {
        FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/LaunchAgents")
    }

    private static var agentsWritable: Bool {
        let probe = agentsDir.appendingPathComponent(".clipstack_probe")
        let ok = (try? Data([0]).write(to: probe)) != nil
        try? FileManager.default.removeItem(at: probe)
        return ok
    }

    static var plistURL: URL {
        agentsDir.appendingPathComponent("\(label).plist")
    }

    /// 以系统真实状态为准，不只看 UserDefaults
    static var isEnabled: Bool {
        if #available(macOS 13.0, *), SMAppService.mainApp.status == .enabled { return true }
        return FileManager.default.fileExists(atPath: plistURL.path)
    }

    static func set(_ on: Bool) {
        if on { install() } else { uninstall() }
        UserDefaults.standard.set(on, forKey: "launchAtLogin")
    }

    private static func install() {
        // 方式 1：SMAppService（macOS 13+ 官方登录项，最可靠）
        if #available(macOS 13.0, *) {
            let svc = SMAppService.mainApp
            if svc.status != .enabled {
                do {
                    try svc.register()
                    NSLog("ClipStack: SMAppService 注册成功")
                    return
                } catch {
                    NSLog("ClipStack: SMAppService 注册失败 -> \(error.localizedDescription)")
                }
            } else { return }
        }

        // 方式 2：LaunchAgent（旧系统 / SMAppService 失败时）
        guard agentsWritable else {
            NSLog("ClipStack: LaunchAgents 目录不可写，无法启用开机自启")
            return
        }
        let exe = Bundle.main.executablePath ?? "/Applications/ClipStack.app/Contents/MacOS/ClipStack"
        let dict: [String: Any] = [
            "Label": label,
            "ProgramArguments": [exe],
            "RunAtLoad": true,
            "KeepAlive": false,
            "ProcessType": "Interactive",
            "LimitLoadToSessionType": "Aqua"
        ]
        if let data = try? PropertyListSerialization.data(fromPropertyList: dict, format: .xml, options: 0) {
            try? data.write(to: plistURL, options: .atomic)
            run("/bin/launchctl", ["bootstrap", "gui/\(getuid())", plistURL.path])
        }
    }

    private static func uninstall() {
        if #available(macOS 13.0, *), SMAppService.mainApp.status == .enabled {
            try? SMAppService.mainApp.unregister()
        }
        run("/bin/launchctl", ["bootout", "gui/\(getuid())/\(label)"])
        try? FileManager.default.removeItem(at: plistURL)
    }

    @discardableResult
    private static func run(_ path: String, _ args: [String]) -> Int32 {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: path)
        p.arguments = args
        p.standardOutput = FileHandle.nullDevice
        p.standardError = FileHandle.nullDevice
        do { try p.run(); p.waitUntilExit(); return p.terminationStatus } catch { return -1 }
    }

    static var statusText: String {
        if #available(macOS 13.0, *) {
            switch SMAppService.mainApp.status {
            case .enabled: return "已开启（系统登录项）"
            case .requiresApproval: return "需在「系统设置 → 通用 → 登录项」里允许"
            case .notFound: break  // ad-hoc 签名的 app 无法注册，继续看降级路径
            case .notRegistered: break
            @unknown default: break
            }
        }
        if FileManager.default.fileExists(atPath: plistURL.path) { return "已开启（LaunchAgent）" }
        return "未开启 · 需手动加入「系统设置 → 通用 → 登录项」"
    }
}

// MARK: - App 入口

// MARK: - 崩溃捕获（本地日志，便于排查）

func installCrashLogger() {
    NSSetUncaughtExceptionHandler { ex in
        let msg = "ClipStack EXCEPTION: \(ex.name.rawValue) - \(ex.reason ?? "") \n\(ex.callStackSymbols.joined(separator: "\n"))"
        let path = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/ClipStack/crash.log")
        try? msg.write(to: path, atomically: true, encoding: .utf8)
        NSLog("ClipStack crash: %@", msg)
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var statusItem: NSStatusItem?
    private let monitor = MonitorRef.shared.monitor
    private var hotkeyID: UInt32 = 0

    func applicationDidFinishLaunching(_ notification: Notification) {
        installCrashLogger()
        NSApp.setActivationPolicy(.accessory) // 不占 Dock

        monitor.start()

        // 主热键：⇧⌘C —— 再按一次收起（toggle）
        _ = HotKeyCenter.shared.register(keyCode: UInt32(kVK_ANSI_C),
                                        modifiers: UInt32(cmdKey | shiftKey)) {
            DispatchQueue.main.async { PanelController.shared.toggle() }
        }

        // 兼容旧键：⇧⌘V 同样可以唤起/关闭
        hotkeyID = HotKeyCenter.shared.register(keyCode: UInt32(kVK_ANSI_V),
                                               modifiers: UInt32(cmdKey | shiftKey)) {
            DispatchQueue.main.async { PanelController.shared.toggle() }
        }

        // 兜底热键：⌃⌥⌘V（若系统抢走了 ⇧⌘C/⇧⌘V 可用这个）
        _ = HotKeyCenter.shared.register(keyCode: UInt32(kVK_ANSI_V),
                                         modifiers: UInt32(cmdKey | optionKey | controlKey)) {
            DispatchQueue.main.async { PanelController.shared.toggle() }
        }

        setupStatusItem()

        // 首次启动默认开启开机自启
        if !UserDefaults.standard.bool(forKey: "didInitLaunchAtLogin") {
            UserDefaults.standard.set(true, forKey: "didInitLaunchAtLogin")
            LaunchAtLogin.set(true)
            NSLog("ClipStack: 首次启动，已尝试开启开机自启 -> \(LaunchAtLogin.statusText)")
        }

        // v1.2 迁移：老版本 maxItems 只能是正数（默认 200），
        // 升级后统一切到「无限」，除非用户主动改过
        if !UserDefaults.standard.bool(forKey: "didMigrateUnlimited") {
            UserDefaults.standard.set(true, forKey: "didMigrateUnlimited")
            UserDefaults.standard.set(-1, forKey: "maxItems")
            NSLog("ClipStack: 已迁移到无限历史")
        }

        NSLog("ClipStack: 启动完成, hotkey id=\(hotkeyID)")
    }

    private func setupStatusItem() {
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        if let btn = item.button {
            btn.image = NSImage(systemSymbolName: "doc.on.clipboard", accessibilityDescription: "ClipStack")
            btn.image?.isTemplate = true
            btn.toolTip = "ClipStack · ⇧⌘C"
        }
        let menu = NSMenu()
        menu.addItem(withTitle: "唤起 / 关闭历史  ⇧⌘C", action: #selector(openPanel), keyEquivalent: "").target = self
        menu.addItem(.separator())
        menu.addItem(withTitle: "设置…", action: #selector(openSettings), keyEquivalent: ",").target = self
        menu.addItem(withTitle: "清空（保留置顶）", action: #selector(clear), keyEquivalent: "").target = self
        menu.addItem(.separator())
        menu.addItem(withTitle: "退出 ClipStack", action: #selector(quit), keyEquivalent: "q").target = self
        item.menu = menu
        statusItem = item
    }

    @objc private func openPanel() { PanelController.shared.toggle() }
    @objc private func openSettings() { SettingsWindowController.shared.show() }
    @objc private func clear() { ClipStore.shared.clearAll(keepingPinned: true) }
    @objc private func quit() { NSApp.terminate(nil) }

    func applicationSupportsSecureRestorableState(_ app: NSApplication) -> Bool { true }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()
