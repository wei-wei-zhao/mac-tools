# tal-tools

老赵的 macOS 小工具合集。纯本地运行，不依赖云服务。

## 工具列表

| 工具 | 语言 | 用途 | 状态 |
|---|---|---|---|
| [ClipStack](ClipStack/) | Swift | 剪贴板历史：`⌘V` 粘贴最近一条，`⇧⌘C` 唤起/关闭历史面板，选中即粘到原位置 | 可用 |
| [WiFiDrop](WiFiDrop/) | Python | 局域网互传：手机浏览器和 Mac 双向传文件、同步文字 | 可用 |

## ClipStack

自研替代 Maccy。单文件 Swift，无需 Xcode 工程。

**快捷键**

| 操作 | 快捷键 |
|---|---|
| 粘贴最近一条 | `⌘V`（系统原生，不做拦截） |
| 唤起 / 关闭历史面板（再按一次收起） | `⇧⌘C` |
| 上下选择 | `↑` `↓` |
| 选中并粘贴 | `⏎` |
| 快速选第 N 条 | `⌘1`–`⌘9` |
| 删除选中条目 | `⌥⌫` |
| 关闭面板 | `Esc` / `⇧⌘C` / 右上角 ✕ |

**设计要点**

1. 每 0.3 秒轮询 `NSPasteboard.changeCount`，文本内容全部入库
2. `⌘V` 走系统原生通道，永远是最新一条
3. 选中历史粘贴后**自动还原你原来的剪贴板内容**，不会污染"最近一条"
4. 重复复制同一段文本只更新时间戳，不新增条目，历史顺序不变
5. 数据纯本地：`~/Library/Application Support/ClipStack/history.json`，权限 600

**构建**

```bash
cd ClipStack
swiftc -O -swift-version 5 -target arm64-apple-macosx14.0 -o ClipStack ClipStack.swift
# 打包 .app 见 ClipStack/README.md
```

## WiFiDrop

手机浏览器打开网页，和 Mac 双向传文件、同步文字。零安装、零登录、纯局域网。

- 📤 手机 → Mac：点选或多选文件，带进度条
- 📥 Mac → 手机：文件浏览 + 图片缩略图，可逐层浏览目录
- 📋 文字：剪贴板双向同步

详见 [WiFiDrop/README.md](WiFiDrop/README.md)。

## 环境

- macOS 14+（ClipStack 面板使用了 SwiftUI 的 `onChange` 双参数签名）
- Python 3.13（WiFiDrop）
- Swift 6.3（Xcode Command Line Tools 即可）

## 注意

- 两个工具都只在本机运行，不上传任何数据到云端
- 剪贴板历史为明文存储，含密码、证件号等敏感信息，公用设备请勿长期开启
