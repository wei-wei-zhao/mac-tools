# ClipStack — 极简剪贴板历史（本地版）

自研替代 Maccy。纯本地存储、无网络、无 Dock 图标。

## 快捷键

| 操作 | 快捷键 |
|---|---|
| 粘贴最近一条（系统原生） | `Cmd + V` |
| 唤起 / 关闭历史面板（toggle） | `Shift + Cmd + C` |
| 上下选择 | `↑` `↓` |
| 选中并粘贴 | `Enter` |
| 快速选第 N 条 | `Cmd + 1..9` |
| 删除选中条目 | `Option + Delete` |
| 关闭面板 | `Esc` / `Shift + Cmd + C` / 右上角 ✕ |

## 关键设计（按需求实现）

1. **每次复制都入库**：0.3 秒轮询 `NSPasteboard.changeCount`，文本类内容全部记录。
2. **`Cmd+V` 永远是最近一条**：系统原生剪贴板通道，不做任何拦截。
3. **`Shift+Cmd+C` 显示历史**（再按一次收起；`Shift+Cmd+V` 作为兼容键也保留）：Carbon 全局热键，不占系统辅助功能权限即生效。
4. **选中即粘贴到原焦点位置**：通过辅助功能 API（`AXSelectedText`）直接写入目标输入框，
   完全不经过剪贴板，所以不会污染「最近一条」。失败时回落到剪贴板通道 + 合成 `⌘V`。
5. **不改变历史顺序**：同一个文本再次复制只更新时间戳，不新增条目；
   选中历史粘贴**不会**把它顶到列表首位，也不会污染"最近一条"——粘贴完成后自动还原原剪贴板内容。
6. **置顶**：目前 UI 未暴露入口，数据结构已支持（`isPinned`），清空时保留置顶项。

## 权限与签名（重要）

ClipStack 需要「辅助功能」权限才能往其他 App 输入框写入文字。

⚠️ **签名方式必须是固定证书**，不能是 ad-hoc：

```bash
# 正确：绑定证书，重新编译也不会失效
codesign --force --sign "ClipStack Local Signing" --identifier com.tal.ClipStack ClipStack.app

# 错误：ad-hoc 签名，每次重新编译 CDHash 都变，权限立刻失效
codesign --force --sign - --identifier com.tal.ClipStack ClipStack.app
```

验证绑定的是证书而非文件哈希：

```bash
codesign -dr - /Applications/ClipStack.app
# 应输出：designated => identifier "com.tal.ClipStack" and certificate leaf = H"..."
```

## 面板位置

固定显示在**非主屏**的右上角，避免遮挡主屏办公区。单屏时回落到主屏。

## 数据

- 路径：`~/Library/Application Support/ClipStack/history.json`
- 权限：`0600`（仅本人可读）
- 容量：默认 200 条，设置里可改（0 = 暂停记录）
- 崩溃日志：同目录 `crash.log`

## 卸载 / 清理

```bash
pkill -x ClipStack
rm -rf /Applications/ClipStack.app
rm -rf ~/Library/Application\ Support/ClipStack
rm -f ~/Library/LaunchAgents/com.tal.ClipStack.plist
```

## 源码与构建

`ClipStack.swift` 单文件，无需 Xcode 工程：

```bash
swiftc -O -swift-version 5 -target arm64-apple-macosx14.0 -o ClipStack ClipStack.swift
cp ClipStack ClipStack.app/Contents/MacOS/ClipStack
codesign --force --sign - --identifier com.tal.ClipStack ClipStack.app
cp -R ClipStack.app /Applications/
open -a /Applications/ClipStack.app
```

## 已知限制

- 只记录**文本**（不存图片、文件、富文本）。需要可以扩展 `enabledPasteboardTypes`。
- macOS 26 上某些 App（如 VS Code 的终端）会拦截合成的 `Cmd+V`，此时会退化为"内容留在剪贴板，手动按 `Cmd+V`"。
- 面板定位跟随鼠标：优先显示在鼠标上方，空间不足时移到下方。
