#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WiFiDrop 菜单栏入口
双击 .app 后，菜单栏出现图标，服务在后台运行，不占终端窗口。
"""

import os
import sys
import threading
import time
import subprocess
import webbrowser
import socket
from pathlib import Path

# ---------------- 定位资源目录（PyInstaller 打包后路径会变） ----------------
def resource_dir() -> Path:
    """打包后资源在 sys._MEIPASS，开发时在脚本目录"""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base)
    return Path(__file__).resolve().parent


def data_dir() -> Path:
    """用户数据目录：配置和接收的文件都在这里"""
    d = Path.home() / "Library" / "Application Support" / "WiFiDrop"
    d.mkdir(parents=True, exist_ok=True)
    return d


RES = resource_dir()

# 把资源目录加入模块搜索路径，确保能 import server
sys.path.insert(0, str(RES))

# ---------------- 自动更新：启动最早期先应用待安装的包 ----------------
try:
    import updater
    updater.apply_pending_on_launch()
except Exception:
    updater = None

# ---------------- 启动 Flask 服务（后台线程） ----------------
import server as wd            # noqa: E402

PORT = 8765
_server_thread = None
_started = False
_ip = "127.0.0.1"


def get_ip() -> str:
    return wd.get_local_ip()


def start_server():
    """在后台线程里跑 Flask"""
    global _started, _ip
    if _started:
        return

    # 选一个可用端口
    port = PORT
    for p in range(PORT, PORT + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", p)) != 0:
                port = p
                break

    # 初始化目录与配置
    cfg_dir = data_dir()
    cfg_file = cfg_dir / "config.json"
    wd.CONFIG["config_file"] = cfg_file
    wd.CONFIG["port"] = port

    d = Path.home() / "Downloads" / "WiFiDrop"
    d.mkdir(parents=True, exist_ok=True)
    wd.CONFIG["recv_dir"] = d

    # 读上次保存的配置
    wd.load_config()
    try:
        import json
        if cfg_file.exists():
            raw = json.loads(cfg_file.read_text(encoding="utf-8"))
            wd.AUTO_SORT["on"] = bool(raw.get("auto_sort", False))
    except Exception:
        pass

    _ip = get_ip()

    _server_thread = threading.Thread(
        target=lambda: wd.app.run(
            host="0.0.0.0", port=port, debug=False,
            threaded=True, use_reloader=False,
        ),
        daemon=True,
    )
    _server_thread.start()
    _started = True
    time.sleep(0.6)

    # 定期检查 IP 是否变了（公司 DHCP 会换地址）
    threading.Thread(target=_ip_watch_loop, daemon=True).start()


def _ip_watch_loop():
    """
    每 60 秒看一次 IP。
    变了就更新缓存，并在菜单栏图标上提示一下。
    """
    global _ip
    time.sleep(15)
    while True:
        try:
            fresh = get_ip()
            if fresh and fresh != "127.0.0.1" and fresh != _ip:
                old = _ip
                _ip = fresh
                gate = None
                try:
                    gate = wd.detect_network()
                except Exception:
                    pass
                if gate:
                    _ip = gate.get("ip") or fresh
                print(f"[WiFiDrop] IP 变了: {old} → {_ip}")
                _notify_ip_changed(old, _ip)
        except Exception:
            pass
        time.sleep(60)


def _notify_ip_changed(old: str, new: str):
    """IP 变化时用系统通知提醒（不打扰，可忽略）"""
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "新地址 {new}" with title "WiFiDrop 地址已更新" '
             f'subtitle "原来的 {old} 已失效，请重新扫码"'],
            capture_output=True, timeout=5,
        )
    except Exception:
        pass


def stop_server():
    """进程退出时服务随之结束（daemon 线程）"""
    global _started
    _started = False


# ---------------- 内置更新分发服务 ----------------
# 让 WiFiDrop 自己兼做更新源：同事的 App 从这台 Mac 拉新版本，
# 不用额外开服务器、不用配开机自启。

_update_srv = {"port": None, "thread": None, "dir": None}
UPDATE_PORT = 8790


def start_update_server() -> int | None:
    """
    在后台起一个静态文件服务，共享更新包目录。
    返回实际端口，失败返回 None。
    """
    global _update_srv
    if _update_srv["thread"] and _update_srv["thread"].is_alive():
        return _update_srv["port"]

    # 找更新包目录：优先用 App 同级的 release，其次用户目录
    candidates = [
        Path("/Applications/WiFiDrop.app/Contents/Resources/release"),
        Path.home() / "Library/Application Support/WiFiDrop/release",
        Path(__file__).resolve().parent / "dist" / "release" if not getattr(sys, "_MEIPASS", None) else None,
    ]
    src = None
    for c in candidates:
        if c and c.is_dir() and any(c.glob("version.json")):
            src = c
            break
    if src is None:
        return None

    # 选一个空闲端口
    port = UPDATE_PORT
    for p in range(UPDATE_PORT, UPDATE_PORT + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", p)) != 0:
                port = p
                break

    def _serve():
        import http.server as hs
        import functools
        handler = functools.partial(
            hs.SimpleHTTPRequestHandler, directory=str(src)
        )
        # 静默日志，别刷屏
        def log_message(self, *a):
            pass
        handler.log_message = log_message
        try:
            httpd = hs.ThreadingHTTPServer(("0.0.0.0", port), handler)
            httpd.serve_forever()
        except Exception:
            pass

    t = threading.Thread(target=_serve, daemon=True)
    t.start()
    time.sleep(0.4)

    _update_srv = {"port": port, "thread": t, "dir": str(src)}
    return port


def update_server_url() -> str:
    p = _update_srv.get("port")
    if not p:
        return ""
    return f"http://{_ip}:{p}/version.json"


def refresh_ip():
    """重新探测并更新缓存的 IP（换网络后调用）"""
    global _ip
    try:
        fresh = get_ip()
        if fresh and fresh != "127.0.0.1":
            _ip = fresh
    except Exception:
        pass
    return _ip


def url() -> str:
    """
    当前手机该访问的地址。
    每次调用都重新读一次 IP —— 公司 DHCP 会续租换地址，
    缓存的旧 IP 会让二维码扫不开。
    """
    global _ip
    try:
        fresh = get_ip()
        if fresh and fresh != "127.0.0.1":
            _ip = fresh
    except Exception:
        pass
    return f"http://{_ip}:{wd.CONFIG.get('port', PORT)}"


# ---------------- 二维码窗口 ----------------

_qr_window = None
_delegate = None


def _get_delegate():
    """懒创建按钮事件委托"""
    global _delegate
    if _delegate is not None:
        return _delegate
    try:
        import AppKit
        import objc

        class QRDelegate(AppKit.NSObject):
            def copyLink_(self, sender):
                try:
                    p = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
                    p.communicate(url().encode("utf-8"))
                    AppKit.NSBeep()
                except Exception:
                    pass

            def refreshLink_(self, sender):
                # 重新检测 IP 并重建窗口
                global _ip
                new_ip = get_ip()
                if new_ip != _ip:
                    _ip = new_ip
                show_qr_window()

        _delegate = QRDelegate.alloc().init()
    except Exception:
        _delegate = None
    return _delegate


def make_qr_image(target_url: str, size=320):
    """生成二维码 PIL 图片；失败返回 None"""
    try:
        import qrcode
        qr = qrcode.QRCode(
            error_correction=qrcode.constants.ERROR_CORRECT_H,
            box_size=10, border=2,
        )
        qr.add_data(target_url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="#1d1d1f", back_color="white").convert("RGB")
        return img.resize((size, size))
    except Exception:
        return None


def show_qr_window():
    """弹出一个带二维码的窗口（不抢占焦点到 Dock）"""
    global _qr_window
    try:
        import AppKit
        import Foundation
    except ImportError:
        # 没有 AppKit 时退回文本提示
        rumps_alert_fallback()
        return

    # 已有窗口就先关掉，避免堆叠
    if _qr_window is not None:
        try:
            _qr_window.close()
        except Exception:
            pass
        _qr_window = None

    target = url()

    # ============ 布局：自顶向下流式计算 ============
    # 先算出需要多高，再建窗口，避免手写坐标算错导致被裁
    W = 460
    PAD = 24                     # 左右留白
    inner_w = W - PAD * 2

    try:
        net = wd.detect_network()
    except Exception:
        net = {}

    risk = net.get("risk", "warn") if net else "warn"
    advice_text = net.get("advice", "") if net else ""
    show_advice = bool(advice_text) and risk != "good"

    QR = 300                     # 二维码边长

    # 各块高度
    H_TITLE = 30 + 10            # 标题 + 间距
    H_NET = 22 + 6               # 网络状态行 + 间距
    H_ADVICE = (44 + 8) if show_advice else 0
    H_QR = QR + 18
    H_ADDR = 24 + 8
    H_TIP = 18 + 12
    H_BTN = 34 + 18
    H_TOP = 16                   # 顶部留白

    H = int(H_TOP + H_TITLE + H_NET + H_ADVICE + H_QR + H_ADDR + H_TIP + H_BTN)

    style = (
        AppKit.NSWindowStyleMaskTitled
        | AppKit.NSWindowStyleMaskClosable
    )
    win = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        Foundation.NSMakeRect(0, 0, W, H),
        style,
        AppKit.NSBackingStoreBuffered,
        False,
    )
    win.setTitle_("WiFiDrop · 手机扫码")
    win.center()
    win.setReleasedWhenClosed_(False)

    content = win.contentView()

    # 光标：从顶部往下走（AppKit 原点在左下，所以用 y 递减）
    cy = H - H_TOP

    def label(text, height, font_size, color=None, bold=False,
              wrap=False, center=True):
        nonlocal cy
        cy -= height
        f = AppKit.NSTextField.alloc().initWithFrame_(
            Foundation.NSMakeRect(PAD, cy, inner_w, height)
        )
        f.setStringValue_(text)
        f.setAlignment_(AppKit.NSTextAlignmentCenter if center
                        else AppKit.NSTextAlignmentLeft)
        f.setFont_(AppKit.NSFont.boldSystemFontOfSize_(font_size) if bold
                   else AppKit.NSFont.systemFontOfSize_(font_size))
        if color is not None:
            f.setTextColor_(color)
        f.setBezeled_(False)
        f.setDrawsBackground_(False)
        f.setEditable_(False)
        f.setSelectable_(False)
        if wrap:
            f.setLineBreakMode_(AppKit.NSLineBreakByWordWrapping)
            f.cell().setWraps_(True)
        content.addSubview_(f)
        return f

    def gap(h):
        nonlocal cy
        cy -= h

    # ---- 标题 ----
    label("手机扫这个码", 26, 18, bold=True)
    gap(10)

    # ---- 网络状态 ----
    if net:
        color = {
            "good": (0.20, 0.78, 0.35),
            "warn": (1.0, 0.58, 0.0),
            "error": (1.0, 0.23, 0.19),
        }.get(risk, (0.6, 0.6, 0.6))
        label(
            f"{net.get('icon','')}  {net.get('label','')}   ·   {net.get('ip','')}",
            20, 12,
            color=AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(*color, 1.0),
        )
        gap(6)

    if show_advice:
        label(advice_text, 42, 11,
              color=AppKit.NSColor.secondaryLabelColor(), wrap=True)
        gap(8)

    # ---- 二维码 ----
    img = make_qr_image(target, QR)
    qr_bottom = cy - QR
    if img is not None:
        try:
            import io
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            data = Foundation.NSData.dataWithBytes_length_(
                buf.getvalue(), len(buf.getvalue())
            )
            nsimg = AppKit.NSImage.alloc().initWithData_(data)
            img_view = AppKit.NSImageView.alloc().initWithFrame_(
                Foundation.NSMakeRect((W - QR) / 2, qr_bottom, QR, QR)
            )
            img_view.setImage_(nsimg)
            img_view.setImageScaling_(AppKit.NSImageScaleProportionallyUpOrDown)
            img_view.setEditable_(False)
            img_view.setImageFrameStyle_(AppKit.NSImageFrameNone)
            content.addSubview_(img_view)
        except Exception:
            img = None

    if img is None:
        label("二维码生成失败，请手动输入下方地址", 40, 12)
    else:
        gap(QR)
    gap(18)

    # ---- 地址 ----
    addr = label(target, 22, 15,
                 color=AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(
                     0.0, 0.44, 0.89, 1.0))
    addr.setSelectable_(True)
    gap(8)

    # ---- 提示 ----
    label("手机连同一个 WiFi 后扫码，或手动输入上面的地址", 17, 11.5,
          color=AppKit.NSColor.secondaryLabelColor())
    gap(14)

    # ---- 按钮 ----
    btn_w = 150
    gap(34)
    btn_y = cy
    for text, x, action in (
        ("复制地址", (W - btn_w * 2 - 14) / 2, "copyLink:"),
        ("刷新地址", (W - btn_w * 2 - 14) / 2 + btn_w + 14, "refreshLink:"),
    ):
        btn = AppKit.NSButton.alloc().initWithFrame_(
            Foundation.NSMakeRect(x, btn_y, btn_w, 34)
        )
        btn.setTitle_(text)
        btn.setBezelStyle_(AppKit.NSBezelStyleRounded)
        d = _get_delegate()
        if d is not None:
            btn.setTarget_(d)
            btn.setAction_(Foundation.NSSelectorFromString(action))
        content.addSubview_(btn)

    win.makeKeyAndOrderFront_(None)
    AppKit.NSApp.activateIgnoringOtherApps_(True)
    _qr_window = win


def rumps_alert_fallback():
    try:
        import rumps
        rumps.alert("手机访问地址", f"手机连同一个 WiFi 后打开：\n\n{url()}", ok="知道了")
    except Exception:
        print(url())


# ---------------- 菜单栏 ----------------
def run_menu():
    try:
        import rumps
    except ImportError:
        # 没有 rumps 时退化为纯后台进程
        start_server()
        print(f"WiFiDrop 运行中：{url()}")
        print("按 Ctrl+C 退出")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        return

    class WiFiDropApp(rumps.App):
        def __init__(self):
            super().__init__(
                "WiFiDrop",
                title="⚡",
                quit_button=None,
            )
            ver = updater.current_version() if updater else wd.APP_VERSION
            self._ver = ver
            self.menu = [
                rumps.MenuItem("📱 显示二维码（手机扫我）",
                               callback=self._show_qr_item),
                "打开上传页面",
                "查看访问地址",
                "网络检测 / 用热点连接…",
                None,
                "在 Finder 打开接收目录",
                "更换保存位置…",
                None,
                "复制地址",
                "重启服务",
                None,
                rumps.MenuItem("检查更新", callback=self._check_update_item),
                rumps.MenuItem("更新设置…", callback=self._update_settings),
                rumps.MenuItem(f"版本 v{ver}", callback=None),
                None,
                "退出 WiFiDrop",
            ]
            self._timer = rumps.Timer(self._tick, 30)
            self._timer.start()

        # ---- 状态刷新 ----
        def _tick(self, _):
            if _started:
                self.title = "⚡"
            else:
                self.title = "⚠️"

            # 有待安装的更新就换个图标提示
            if updater:
                try:
                    st = updater.load_state()
                    if st.get("pending_install"):
                        self.title = "⬆️"
                except Exception:
                    pass

        # ---- 菜单项 ----
        def _show_qr_item(self, _):
            show_qr_window()

        @rumps.clicked("打开上传页面")
        def open_page(self, _):
            webbrowser.open(url())

        @rumps.clicked("显示二维码")
        def show_qr(self, _):
            show_qr_window()

        @rumps.clicked("查看访问地址")
        def show_url(self, _):
            try:
                net = wd.detect_network()
            except Exception:
                net = {}
            extra = ""
            if net:
                extra = (f"\n\n{net.get('icon','')} {net.get('label','')}"
                         f"\n{net.get('advice','')}")
            rumps.alert(
                title="手机访问地址",
                message=f"{url()}\n\n手机连同一个 WiFi，浏览器打开这个地址。"
                        f"{extra}\n\n"
                        f"接收目录：{wd.dir_info(wd.CONFIG['recv_dir'])['display']}",
                ok="知道了",
            )

        @rumps.clicked("网络检测 / 用热点连接…")
        def net_help(self, _):
            try:
                net = wd.detect_network()
            except Exception as e:
                rumps.alert("检测失败", str(e), ok="好")
                return

            risk = net.get("risk")
            if risk == "good":
                rumps.alert(
                    "网络畅通 ✅",
                    f"{net.get('icon','')} {net.get('label','')}\n\n"
                    f"IP：{net.get('ip','')}\n"
                    f"网关：{net.get('gateway','') or '—'}\n"
                    f"WiFi：{net.get('ssid','') or '—'}\n\n"
                    f"{net.get('advice','')}",
                    ok="好",
                )
            else:
                # 非畅通网络，给出热点操作指引
                rumps.alert(
                    f"{net.get('icon','')} {net.get('label','')} —— 可能连不上",
                    f"当前 IP：{net.get('ip','')}\n\n"
                    f"{net.get('advice','')}\n\n"
                    "──────────────\n"
                    "改用手机热点（更可靠）：\n"
                    "1. 手机上打开「个人热点」\n"
                    "2. Mac 点右上角 WiFi 图标，连你的热点\n"
                    "3. 回到本菜单点「显示二维码」，重新扫码\n\n"
                    "热点下手机和 Mac 直连，一定能通。",
                    ok="知道了",
                )

        @rumps.clicked("在 Finder 打开接收目录")
        def open_folder(self, _):
            subprocess.Popen(["open", str(wd.CONFIG["recv_dir"])])

        @rumps.clicked("更换保存位置…")
        def change_dir(self, _):
            # 调用系统选择文件夹对话框
            script = '''
                set theFolder to choose folder with prompt "选择 WiFiDrop 的保存位置："
                return POSIX path of theFolder
            '''
            try:
                r = subprocess.run(
                    ["osascript", "-e", script],
                    capture_output=True, timeout=120,
                )
                if r.returncode != 0:
                    return
                picked = r.stdout.decode("utf-8", "replace").strip()
                if not picked:
                    return
                target, err = wd.resolve_target_dir(picked)
                if err:
                    rumps.alert("无法使用这个位置", err, ok="好")
                    return
                wd.CONFIG["recv_dir"] = target
                wd.save_config()
                rumps.notification(
                    "WiFiDrop",
                    "保存位置已切换",
                    wd.dir_info(target)["display"],
                )
            except Exception as e:
                rumps.alert("出错了", str(e), ok="好")

        @rumps.clicked("复制地址")
        def copy_url(self, _):
            try:
                p = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
                p.communicate(url().encode("utf-8"))
                rumps.notification("WiFiDrop", "地址已复制", url())
            except Exception:
                pass

        @rumps.clicked("重启服务")
        def restart(self, _):
            global _started
            _started = False
            time.sleep(0.4)
            start_server()
            rumps.notification("WiFiDrop", "服务已重启", url())

        # ---- 更新相关 ----
        def _check_update_item(self, _):
            if not updater:
                rumps.alert("不可用", "更新模块未加载", ok="好")
                return
            cfg = updater.load_cfg()
            if not cfg.get("update_url", "").strip():
                rumps.alert(
                    "还没配置更新地址",
                    "请先点「更新设置…」填入 version.json 的地址。\n\n"
                    "示例：\nhttps://your-server.com/wifidrop/version.json",
                    ok="好",
                )
                return

            r = updater.check_and_update(silent=False)
            if r.get("updated"):
                rumps.alert(
                    "新版本已就绪",
                    f"v{r.get('current')} → v{r.get('remote')}\n\n"
                    "已静默下载完成，下次启动自动生效。\n"
                    "现在重启吗？",
                    ok="稍后",
                )
            elif r.get("ok"):
                rumps.alert(
                    "已是最新版本",
                    f"当前 v{r.get('current')}\n远端 v{r.get('remote')}",
                    ok="好",
                )
            else:
                rumps.alert("检查失败", r.get("reason", "未知原因"), ok="好")

        @rumps.clicked("更新设置…")
        def _update_settings_unused(self, _):
            pass

        def _update_settings(self, _):
            if not updater:
                return
            cfg = updater.load_cfg()
            # 用 AppleScript 弹输入框，省掉额外依赖
            script = f'''
                set r to display dialog "版本清单地址（version.json）:" default answer "{cfg.get('update_url','')}" buttons {{"取消","保存"}} default button "保存" with title "WiFiDrop 更新设置"
                return text returned of r
            '''
            try:
                r = subprocess.run(
                    ["osascript", "-e", script],
                    capture_output=True, timeout=120,
                )
                if r.returncode != 0:
                    return
                new_url = r.stdout.decode("utf-8", "replace").strip()
                if not new_url:
                    return
                cfg["update_url"] = new_url
                cfg["auto_update"] = True
                updater.save_cfg(cfg)
                rumps.notification("WiFiDrop", "更新地址已保存", new_url[:60])
            except Exception as e:
                rumps.alert("保存失败", str(e), ok="好")

        @rumps.clicked("退出 WiFiDrop")
        def quit_app(self, _):
            stop_server()
            rumps.quit_application()

    # 先起服务，再显示菜单栏
    start_server()

    # 内置更新分发服务（让同事能自动更新）
    try:
        up = start_update_server()
        if up:
            print(f"更新分发服务已启动: {update_server_url()}")
    except Exception:
        pass

    # 更新：健康看门狗 + 后台定时检查 + 菜单绑定
    if updater:
        try:
            updater.load_cfg()          # 首次运行会写出默认配置
        except Exception:
            pass
        try:
            updater.start_health_watchdog()
        except Exception:
            pass
        try:
            updater.start_background_checker()
        except Exception:
            pass

    app = WiFiDropApp()
    app.run()


def main():
    # 单实例检查：已有实例在跑就只打开页面
    lock_file = data_dir() / "app.lock"
    try:
        if lock_file.exists():
            pid = int(lock_file.read_text().strip() or "0")
            if pid and pid != os.getpid():
                try:
                    os.kill(pid, 0)          # 探测进程是否活着
                    webbrowser.open(url())
                    return
                except OSError:
                    pass                     # 旧进程已死，继续启动
        lock_file.write_text(str(os.getpid()))
    except Exception:
        pass

    try:
        run_menu()
    finally:
        try:
            lock_file = data_dir() / "app.lock"
            if lock_file.exists() and lock_file.read_text().strip() == str(os.getpid()):
                lock_file.unlink()
        except Exception:
            pass


if __name__ == "__main__":
    main()
