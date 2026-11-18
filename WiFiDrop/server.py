#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WiFiDrop - macOS 局域网快速传输工具
手机浏览器打开网页，与 Mac 双向传文件 / 同步文字剪贴板

用法:
    python3 server.py                 # 默认端口 8765
    python3 server.py --port 9000     # 指定端口
    python3 server.py --dir ~/下载    # 指定接收目录
"""

import argparse
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import unicodedata
from pathlib import Path

from flask import (
    Flask,
    jsonify,
    render_template,
    request,
    send_file,
    send_from_directory,
    abort,
)

# ---------------------------------------------------------------- 配置

APP_VERSION = "1.4.5"
DEFAULT_PORT = 8765


def _base_dir() -> Path:
    """兼容 PyInstaller：打包后资源在 sys._MEIPASS"""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return Path(__file__).resolve().parent


BASE_DIR = _base_dir()

app = Flask(
    __name__,
    template_folder=str(BASE_DIR / "templates"),
    static_folder=str(BASE_DIR / "static") if (BASE_DIR / "static").exists() else None,
)
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024 * 1024  # 单文件最大 4GB
# 模板改动后立即生效，避免改了 HTML 还要重启服务
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True
# 静态资源不缓存，手机上刷新就能拿到新版
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

CONFIG = {
    "recv_dir": None,      # 当前接收目录
    "port": DEFAULT_PORT,
    "config_file": None,   # 配置文件路径（记住用户选择）
}

# 剪贴板：进程内保存 + 同步到 Mac 系统剪贴板
CLIP = {"text": "", "ts": 0, "source": ""}

# 按类型自动分目录的开关（内存态 + 持久化）
AUTO_SORT = {"on": False}          # 是否按类型自动分到子目录

# ---------------------------------------------------------------- 连接设备跟踪
# 结构: {device_id: {ip, ua, os, browser, first_seen, last_seen, requests, name}}
DEVICES = {}
DEV_LOCK = threading.Lock()
ACTIVE_WINDOW = 120          # 秒：这么久内有请求就算「在线」

# 配置持久化用的锁
CFG_LOCK = threading.Lock()

# 图片类扩展名（用于「按类型自动分目录」）
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif",
              ".bmp", ".tiff", ".tif", ".avif", ".raw", ".dng"}

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".3gp"}


# ---------------------------------------------------------------- 工具函数

def safe_name(name: str) -> str:
    """清洗文件名，防止路径穿越与非法字符"""
    name = os.path.basename(name or "")
    name = unicodedata.normalize("NFC", name)
    name = re.sub(r'[/\\:\*\?"<>\|\x00-\x1f]', "_", name)
    name = name.strip(". ")
    return name or "未命名文件"


# ---------------------------------------------------------------- 目录管理

def default_recv_dir() -> Path:
    return Path.home() / "Downloads" / "WiFiDrop"


def preset_dirs() -> dict:
    """可快速选择的常用目录"""
    home = Path.home()
    return {
        "wifidrop": home / "Downloads" / "WiFiDrop",
        "downloads": home / "Downloads",
        "desktop": home / "Desktop",
        "pictures": home / "Pictures",
        "documents": home / "Documents",
        "movies": home / "Movies",
    }


def load_config():
    """从磁盘读取上次选择的目录"""
    cfg_path = CONFIG.get("config_file")
    if not cfg_path:
        return
    try:
        if cfg_path.exists():
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
            saved = data.get("recv_dir")
            if saved:
                p = Path(os.path.expanduser(saved)).resolve()
                if p.is_dir():
                    CONFIG["recv_dir"] = p
    except Exception:
        pass


def save_config():
    """把当前目录写回磁盘，下次启动自动恢复（保留其它字段）"""
    cfg_path = CONFIG.get("config_file")
    if not cfg_path:
        return
    try:
        with CFG_LOCK:
            cfg_path.parent.mkdir(parents=True, exist_ok=True)
            data = {}
            # 先读旧内容，避免丢掉 auto_sort 等字段
            if cfg_path.exists():
                try:
                    data = json.loads(cfg_path.read_text(encoding="utf-8"))
                except Exception:
                    data = {}
            data["recv_dir"] = str(CONFIG["recv_dir"])
            data["auto_sort"] = AUTO_SORT["on"]
            data["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
            cfg_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
    except Exception:
        pass


def resolve_target_dir(raw: str):
    """
    校验并规范化用户输入的目录。
    返回 (Path, error)。路径不存在且无法创建时返回 error。
    """
    raw = (raw or "").strip()
    if not raw:
        return None, "路径不能为空"

    # 支持 ~ 展开；相对路径基于用户主目录
    if raw.startswith("~"):
        p = Path(os.path.expanduser(raw))
    elif raw.startswith("/"):
        p = Path(raw)
    else:
        # 允许用户只写子目录名，自动挂到「下载」下
        p = Path.home() / "Downloads" / raw

    try:
        p = p.resolve()
    except Exception:
        return None, "路径无效"

    # 安全边界：必须位于用户主目录内，禁止指向系统目录
    home = Path.home().resolve()
    try:
        p.relative_to(home)
    except ValueError:
        return None, "只能选择你主目录下的文件夹"

    if not p.exists():
        try:
            p.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            return None, f"无法创建目录：{e}"

    if not p.is_dir():
        return None, "这不是一个文件夹"

    if not os.access(p, os.W_OK):
        return None, "该目录没有写入权限"

    return p, None


def dir_info(p: Path) -> dict:
    """目录的描述信息（用于前端展示）"""
    home = Path.home()
    try:
        rel = p.relative_to(home)
        display = "~/" + str(rel) if str(rel) != "." else "~"
    except ValueError:
        display = str(p)

    # 该目录下已接收的文件数
    try:
        count = sum(1 for f in p.iterdir() if f.is_file() and not f.name.startswith("."))
    except Exception:
        count = 0

    return {
        "path": str(p),
        "display": display,
        "name": p.name or str(p),
        "file_count": count,
        "exists": p.exists(),
        "writable": os.access(p, os.W_OK) if p.exists() else False,
    }


def unique_path(folder: Path, filename: str) -> Path:
    """同名文件自动加序号，避免覆盖"""
    dest = folder / filename
    if not dest.exists():
        return dest
    stem, suffix = dest.stem, dest.suffix
    i = 1
    while True:
        cand = folder / f"{stem} ({i}){suffix}"
        if not cand.exists():
            return cand
        i += 1


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"


def get_local_ip() -> str:
    """获取本机内网 IP（优先 WiFi 网卡）"""
    for iface in ("en0", "en1"):
        try:
            ip = subprocess.run(
                ["ipconfig", "getifaddr", iface],
                capture_output=True, text=True, timeout=2,
            ).stdout.strip()
            if ip:
                return ip
        except Exception:
            pass
    # 兜底：UDP 探测出口地址
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


# ---------------------------------------------------------------- 网络环境识别
# 常见手机热点的网段，用于判断「当前是不是走热点」
HOTSPOT_NETS = (
    ("172.20.10.", "iPhone 热点"),
    ("172.20.11.", "iPhone 热点"),
    ("172.20.12.", "iPhone 热点"),
    ("192.168.43.", "安卓热点"),
    ("192.168.42.", "安卓热点"),
    ("192.168.44.", "安卓热点"),
    ("192.168.137.", "Windows 热点"),
    ("192.168.2.1", "热点"),
)


def detect_network() -> dict:
    """
    判断当前网络类型，给出连手机的可靠建议。
    返回 {type, label, icon, risk, advice, iface, ssid, gateway}
    """
    ip = ""
    iface = ""
    # 找出有实际 IP 的网卡
    try:
        out = subprocess.run(
            ["networksetup", "-listallhardwareports"],
            capture_output=True, text=True, timeout=3,
        ).stdout
    except Exception:
        out = ""

    for cand in ("en0", "en1", "en2"):
        try:
            r = subprocess.run(
                ["ipconfig", "getifaddr", cand],
                capture_output=True, text=True, timeout=2,
            ).stdout.strip()
            if r:
                ip, iface = r, cand
                break
        except Exception:
            pass

    if not ip:
        return {
            "type": "none", "label": "未联网", "icon": "🔌",
            "risk": "error", "ip": "", "iface": "",
            "advice": "Mac 没有联网，请先连 WiFi 或打开手机热点",
        }

    # WiFi 名称
    ssid = ""
    try:
        r = subprocess.run(
            ["/usr/sbin/networksetup", "-getairportnetwork", iface],
            capture_output=True, text=True, timeout=3,
        ).stdout.strip()
        if "Current Wi-Fi Network:" in r:
            ssid = r.split("Current Wi-Fi Network:")[-1].strip()
        elif "Current AirPort Network:" in r:
            ssid = r.split("Current AirPort Network:")[-1].strip()
    except Exception:
        pass

    # 网关
    gateway = ""
    try:
        r = subprocess.run(
            ["netstat", "-rn"], capture_output=True, text=True, timeout=3,
        ).stdout
        for line in r.splitlines():
            parts = line.split()
            if parts and parts[0] == "default" and len(parts) > 1:
                gateway = parts[1]
                break
    except Exception:
        pass

    # 判断是否热点
    is_hotspot = False
    hotspot_name = ""
    for prefix, name in HOTSPOT_NETS:
        if ip.startswith(prefix) or gateway.startswith(prefix.rstrip(".")):
            is_hotspot = True
            hotspot_name = name
            break

    base = {"ip": ip, "iface": iface, "ssid": ssid, "gateway": gateway}

    if is_hotspot:
        return {
            **base,
            "type": "hotspot",
            "label": f"{hotspot_name}",
            "icon": "📶",
            "risk": "good",
            "advice": "当前走手机热点，手机和 Mac 直连，一定能通",
        }

    # 内网 / 公司网段
    if ip.startswith("10."):
        return {
            **base,
            "type": "enterprise",
            "label": "企业/校园网络",
            "icon": "🏢",
            "risk": "warn",
            "advice": "这类网络常开启设备隔离，手机可能连不上。"
                      "如果打不开，请打开手机热点让 Mac 连接",
        }

    if ip.startswith("192.168.") or ip.startswith("172.1") or ip.startswith("172.2") or ip.startswith("172.3"):
        return {
            **base,
            "type": "home",
            "label": "家庭/普通 WiFi",
            "icon": "🏠",
            "risk": "good",
            "advice": "普通 WiFi，手机连同一个即可访问",
        }

    return {
        **base,
        "type": "other",
        "label": "其他网络",
        "icon": "🌐",
        "risk": "warn",
        "advice": "未知网络类型，如果手机连不上请改用手机热点",
    }


# ---------------------------------------------------------------- 设备识别

def parse_ua(ua: str) -> dict:
    """从 User-Agent 猜出系统、机型类别和浏览器"""
    ua_l = ua.lower()

    # --- 系统 ---
    if "harmonyos" in ua_l or "openharmony" in ua_l:
        os_name = "鸿蒙"
    elif "iphone" in ua_l:
        os_name = "iPhone"
    elif "ipad" in ua_l:
        os_name = "iPad"
    elif "android" in ua_l:
        os_name = "安卓"
    elif "macintosh" in ua_l or "mac os x" in ua_l:
        os_name = "Mac"
    elif "windows" in ua_l:
        os_name = "Windows"
    elif "linux" in ua_l:
        os_name = "Linux"
    else:
        os_name = "未知"

    # --- 浏览器 ---
    if "micromessenger" in ua_l:
        browser = "微信"
    elif "dingtalk" in ua_l:
        browser = "钉钉"
    elif "lark" in ua_l or "feishu" in ua_l:
        browser = "飞书"
    elif "edgios" in ua_l or "edga" in ua_l or "edge" in ua_l:
        browser = "Edge"
    elif "firefox" in ua_l or "fxios" in ua_l:
        browser = "Firefox"
    elif "chrome" in ua_l or "crios" in ua_l:
        browser = "Chrome"
    elif "safari" in ua_l:
        browser = "Safari"
    elif "huaweibrowser" in ua_l:
        browser = "华为浏览器"
    elif "miuibrowser" in ua_l:
        browser = "小米浏览器"
    else:
        browser = "浏览器"

    is_mobile = os_name in ("iPhone", "iPad", "安卓", "鸿蒙")

    icon = {
        "iPhone": "📱", "iPad": "📲", "安卓": "📱", "鸿蒙": "📱",
        "Mac": "💻", "Windows": "🖥", "Linux": "🐧",
    }.get(os_name, "❓")

    return {
        "os": os_name,
        "browser": browser,
        "icon": icon,
        "is_mobile": is_mobile,
    }


def client_ip() -> str:
    """取真实客户端 IP（兼容代理头）"""
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.remote_addr or "未知"


def touch_device():
    """每次请求更新设备活跃时间（跳过静态资源噪音）"""
    path = request.path or ""
    # 预览、下载这类会连续发很多请求，用较轻的处理
    if path.startswith("/api/preview"):
        return
    if path in ("/favicon.ico",):
        return

    ip = client_ip()
    ua = request.headers.get("User-Agent", "")
    info = parse_ua(ua)
    now = time.time()

    # 设备指纹：IP + 系统 + 浏览器（同一 WiFi 下同型号手机可能重复，够用）
    dev_id = f"{ip}|{info['os']}|{info['browser']}"

    with DEV_LOCK:
        d = DEVICES.get(dev_id)
        if d is None:
            DEVICES[dev_id] = {
                "id": dev_id,
                "ip": ip,
                "ua": ua[:200],
                "os": info["os"],
                "browser": info["browser"],
                "icon": info["icon"],
                "is_mobile": info["is_mobile"],
                "first_seen": now,
                "last_seen": now,
                "requests": 1,
            }
        else:
            d["last_seen"] = now
            d["requests"] += 1
            d["ip"] = ip


def human_idle(sec: float) -> str:
    s = int(sec)
    if s < 60:
        return "刚刚活跃"
    if s < 3600:
        return f"{s // 60} 分钟前"
    if s < 86400:
        return f"{s // 3600} 小时前"
    return f"{s // 86400} 天前"


def device_list() -> dict:
    """整理设备列表，标记在线状态"""
    now = time.time()
    with DEV_LOCK:
        items = []
        for d in DEVICES.values():
            idle = now - d["last_seen"]
            online = idle < ACTIVE_WINDOW
            items.append({
                "id": d["id"],
                "ip": d["ip"],
                "os": d["os"],
                "browser": d["browser"],
                "icon": d["icon"],
                "is_mobile": d["is_mobile"],
                "online": online,
                "idle": int(idle),
                "idle_h": human_idle(idle),
                "first_seen_h": time.strftime(
                    "%H:%M", time.localtime(d["first_seen"])),
                "last_seen_h": time.strftime(
                    "%H:%M", time.localtime(d["last_seen"])),
                "requests": d["requests"],
                "ua": d["ua"],
            })

    # 在线优先，其次按最近活跃
    items.sort(key=lambda x: (not x["online"], x["idle"]))
    return {
        "total": len(items),
        "online": sum(1 for i in items if i["online"]),
        "devices": items,
    }


def set_mac_clipboard(text: str) -> tuple:
    """
    把文字写入 Mac 系统剪贴板，写完回读校验。
    返回 (成功与否, 使用的方法/错误信息)。
    多路径回退：pbcopy -> osascript(带重试) -> tkinter
    """
    errors = []
    want = text

    def verify() -> bool:
        """回读确认真的写进去了"""
        try:
            got = get_mac_clipboard()
            # 允许尾部换行差异
            return got.rstrip("\n") == want.rstrip("\n")
        except Exception:
            return False

    # 方式 1：pbcopy（最轻量）
    try:
        p = subprocess.Popen(
            ["pbcopy"], stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        _, err = p.communicate(want.encode("utf-8"), timeout=5)
        if p.returncode == 0:
            if verify():
                return True, "pbcopy"
            errors.append("pbcopy 返回成功但回读不一致")
        else:
            errors.append(f"pbcopy rc={p.returncode} {err.decode('utf-8', 'replace')[:80]}")
    except Exception as e:
        errors.append(f"pbcopy: {e}")

    # 方式 2：osascript（不受部分沙箱限制），多行用 heredoc 更稳
    try:
        import tempfile
        # 把文本写到临时文件，用 AppleScript 读文件内容设剪贴板，
        # 避免转义把换行/引号搞坏
        with tempfile.NamedTemporaryFile(
            "w", suffix=".txt", delete=False, encoding="utf-8"
        ) as tf:
            tf.write(want)
            tmp_path = tf.name
        script = (
            'set f to POSIX file "%s"\n'
            'set theText to (read f as «class utf8»)\n'
            'set the clipboard to theText' % tmp_path
        )
        r = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, timeout=10,
        )
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        if r.returncode == 0 and verify():
            return True, "osascript-file"
        errors.append(
            f"osascript rc={r.returncode} {r.stderr.decode('utf-8','replace')[:80]}"
        )
    except Exception as e:
        errors.append(f"osascript: {e}")

    # 方式 3：简单 osascript 转义版（兜底）
    try:
        escaped = want.replace("\\", "\\\\").replace('"', '\\"')
        r = subprocess.run(
            ["osascript", "-e", f'set the clipboard to "{escaped}"'],
            capture_output=True, timeout=8,
        )
        if r.returncode == 0 and verify():
            return True, "osascript-escape"
        errors.append("osascript-escape 未生效")
    except Exception as e:
        errors.append(f"osascript-escape: {e}")

    # 方式 4：tkinter（进程内 GUI 剪贴板）
    try:
        import tkinter
        r = tkinter.Tk()
        r.withdraw()
        r.clipboard_clear()
        r.clipboard_append(want)
        r.update()
        time.sleep(0.15)      # tkinter 需要事件循环把内容推出
        r.update()
        r.destroy()
        if verify():
            return True, "tkinter"
        errors.append("tkinter 未生效")
    except Exception as e:
        errors.append(f"tkinter: {e}")

    return False, " | ".join(errors)


def _clean_text(s: str) -> str:
    """
    清理文本里的异常字符：
    - 去掉零宽字符（微信/网页复制常带）
    - 去掉 BOM
    - 把各种「不换行空格」统一成普通空格
    - 去掉控制字符（保留 \n \r \t）
    """
    if not s:
        return ""
    out = []
    for ch in s:
        o = ord(ch)
        if ch in ("\n", "\r", "\t"):
            out.append(ch)
            continue
        # 零宽字符
        if o in (0x200B, 0x200C, 0x200D, 0xFEFF, 0x2060):
            continue
        # 各种特殊空格 → 普通空格
        if o in (0x00A0, 0x2007, 0x202F, 0x205F, 0x3000):
            out.append(" ")
            continue
        # 控制字符（除 tab/lf/cr）
        if o < 0x20 or o == 0x7F:
            continue
        out.append(ch)
    return "".join(out)


def get_mac_clipboard() -> str:
    """
    读取 Mac 系统剪贴板的纯文本，尽量保证中文不乱码。

    微信/网页复制的剪贴板往往同时有多种格式，且可能带零宽字符、
    或只有 UTF-16 而没有 UTF-8。这里依次尝试多种方式，取第一个解出正常中文的结果。
    """
    candidates = []

    # --- 方式 1：pbpaste 默认（utf8 优先）---
    for prefs in (["utf8"], ["txt"], [], ["string"], ["Unicode", "text"]):
        try:
            r = subprocess.run(
                ["pbpaste"] + (["-Prefer"] + prefs if prefs else []),
                capture_output=True, timeout=3,
            )
            if r.stdout:
                candidates.append(r.stdout)
        except Exception:
            pass

    # --- 方式 2：AppleScript 直接取 Unicode text（跳过格式层）---
    try:
        r = subprocess.run(
            ["osascript", "-e",
             'set t to the clipboard as text\nreturn t'],
            capture_output=True, timeout=5,
        )
        if r.returncode == 0 and r.stdout:
            candidates.append(r.stdout.rstrip(b"\n"))
    except Exception:
        pass

    if not candidates:
        return ""

    # ---- 解码策略：UTF-8 严格优先，避免把 UTF-8 误判成 GBK 得到「伪汉字」 ----
    # 这是关键：GB18030 几乎能「成功」解码任何字节序列，但结果是乱码汉字，
    # 所以绝不能让评分机制在 UTF-8 已成功的情况下选其它编码。

    def is_clean(txt: str) -> bool:
        """判断解码结果是否像正常文本"""
        if not txt:
            return False
        if "\ufffd" in txt:
            return False
        # 私有区字符（U+E000-U+F8FF）通常是乱码迹象
        for ch in txt:
            o = ord(ch)
            if 0xE000 <= o <= 0xF8FF:
                return False
            if o < 0x20 and ch not in "\n\r\t":
                return False
        return True

    for raw in candidates:
        if not raw:
            continue

        # 1) UTF-8 严格解码（最常见、最可信）
        try:
            txt = _clean_text(raw.decode("utf-8"))
            if is_clean(txt):
                return txt
        except UnicodeDecodeError:
            pass

        # 2) UTF-8 with BOM
        try:
            txt = _clean_text(raw.decode("utf-8-sig"))
            if is_clean(txt):
                return txt
        except UnicodeDecodeError:
            pass

        # 3) UTF-16（有明显特征：大量 \x00 字节）
        if raw.count(b"\x00") > len(raw) * 0.2:
            for enc in ("utf-16-le", "utf-16-be"):
                try:
                    txt = _clean_text(raw.decode(enc))
                    if is_clean(txt):
                        return txt
                except UnicodeDecodeError:
                    pass

        # 4) 中文传统编码（只在 UTF-8 全失败后尝试）
        for enc in ("gb18030", "big5"):
            try:
                txt = _clean_text(raw.decode(enc))
                if is_clean(txt):
                    return txt
            except (UnicodeDecodeError, LookupError):
                pass

    # 5) 全都严格解不出来，才用 replace 兜底（会有 �）
    if candidates:
        return _clean_text(candidates[0].decode("utf-8", errors="replace"))

    return ""


def get_mac_clipboard_rich() -> dict:
    """
    读取剪贴板的详细信息，供调试和更好的兼容。
    返回 {text, plain, html, rtf, types}
    """
    result = {"text": "", "plain": "", "html": "", "rtf": "", "types": []}

    # 纯文本
    result["plain"] = get_mac_clipboard()

    # 剪贴板里有哪些类型
    try:
        r = subprocess.run(
            ["osascript", "-e", "clipboard info"],
            capture_output=True, timeout=3, text=True,
        )
        result["types"] = r.stdout.strip()
    except Exception:
        pass

    # HTML 片段（从网页/富文本复制时存在）
    try:
        r = subprocess.run(
            ["osascript", "-e", "the clipboard as «class HTML»"],
            capture_output=True, timeout=3,
        )
        if r.returncode == 0 and r.stdout:
            # AppleScript 返回的是 hex+数据，能取到就用
            pass
    except Exception:
        pass

    result["text"] = result["plain"]
    return result


def file_kind(name: str) -> str:
    ext = Path(name).suffix.lower()
    if ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".bmp", ".tiff"):
        return "image"
    if ext in (".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"):
        return "video"
    if ext in (".mp3", ".m4a", ".wav", ".flac", ".aac"):
        return "audio"
    if ext in (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".md", ".csv"):
        return "doc"
    if ext in (".zip", ".rar", ".7z", ".tar", ".gz"):
        return "archive"
    return "file"


# ---------------------------------------------------------------- 路由：页面

@app.before_request
def _track_device():
    """每个请求都记录一次设备活跃（只对页面/接口，不干扰静态）"""
    try:
        touch_device()
    except Exception:
        pass


@app.route("/api/devices")
def api_devices():
    """当前连着的设备列表"""
    data = device_list()
    data["ok"] = True
    data["active_window"] = ACTIVE_WINDOW
    data["self_ip"] = client_ip()
    return jsonify(data)


@app.route("/api/devices/clear", methods=["POST"])
def api_devices_clear():
    """清空离线设备记录"""
    with DEV_LOCK:
        now = time.time()
        gone = [k for k, v in DEVICES.items()
                if now - v["last_seen"] >= ACTIVE_WINDOW]
        for k in gone:
            DEVICES.pop(k, None)
    return jsonify({"ok": True, "removed": len(gone)})


@app.route("/")
def index():
    resp = app.make_response(render_template("index.html", version=APP_VERSION))
    # 页面本身不缓存，保证改了之后手机刷新就能看到新版
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


# ---------------------------------------------------------------- 路由：文件

@app.route("/api/info")
def api_info():
    host = request.host
    cur = CONFIG["recv_dir"]
    info = dir_info(cur)
    return jsonify({
        "ok": True,
        "version": APP_VERSION,
        "recv_dir": str(cur),
        "recv_dir_short": cur.name,
        "recv_dir_display": info["display"],
        "host": host,
        "auto_sort": AUTO_SORT["on"],
    })


@app.route("/api/autosort", methods=["POST"])
def api_autosort():
    """切换「按文件类型自动分子目录」"""
    data = request.get_json(silent=True) or {}
    AUTO_SORT["on"] = bool(data.get("on"))
    save_config()
    return jsonify({"ok": True, "auto_sort": AUTO_SORT["on"]})


@app.route("/api/network")
def api_network():
    """当前网络环境 + 手机该怎么连的建议"""
    try:
        info = detect_network()
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    info["ok"] = True
    info["port"] = CONFIG.get("port", DEFAULT_PORT)
    info["url"] = f"http://{info.get('ip','127.0.0.1')}:{CONFIG.get('port', DEFAULT_PORT)}"
    return jsonify(info)


@app.route("/api/upload", methods=["POST"])
def api_upload():
    """接收手机上传的文件（支持多文件）
    可选参数 auto_sort=1 时，图片/视频/其他自动分到子目录
    """
    files = request.files.getlist("files")
    if not files:
        return jsonify({"ok": False, "error": "没有收到文件"}), 400

    base = CONFIG["recv_dir"]
    auto_sort = request.form.get("auto_sort") in ("1", "true", "on")

    saved = []
    for f in files:
        if not f or not f.filename:
            continue
        name = safe_name(f.filename)
        ext = Path(name).suffix.lower()

        target_dir = base
        if auto_sort:
            if ext in IMAGE_EXTS:
                target_dir = base / "图片"
            elif ext in VIDEO_EXTS:
                target_dir = base / "视频"
            else:
                target_dir = base / "其他"
            target_dir.mkdir(parents=True, exist_ok=True)

        dest = unique_path(target_dir, name)
        f.save(str(dest))
        st = dest.stat()
        saved.append({
            "name": dest.name,
            "size": st.st_size,
            "size_h": human_size(st.st_size),
            "kind": file_kind(dest.name),
            "dir": str(target_dir),
        })

    if not saved:
        return jsonify({"ok": False, "error": "文件为空"}), 400

    return jsonify({
        "ok": True,
        "files": saved,
        "saved_to": str(base),
        "saved_to_display": dir_info(base)["display"],
    })


# ---------------------------------------------------------------- 路由：目录切换

@app.route("/api/dirs", methods=["GET"])
def api_dirs():
    """列出当前目录 + 可选的常用目录"""
    cur = CONFIG["recv_dir"]
    presets = preset_dirs()

    items = []
    for key, p in presets.items():
        if not p.exists():
            continue
        info = dir_info(p)
        info["key"] = key
        info["current"] = (p == cur)
        items.append(info)

    # 当前目录若不在预设里，补一个自定义项
    if cur and not any(Path(i["path"]) == cur for i in items):
        info = dir_info(cur)
        info["key"] = "custom"
        info["current"] = True
        items.insert(0, info)

    return jsonify({
        "ok": True,
        "current": dir_info(cur),
        "presets": items,
    })


@app.route("/api/dirs/set", methods=["POST"])
def api_dirs_set():
    """动态切换接收目录"""
    data = request.get_json(silent=True) or {}
    raw = data.get("path", "")

    # 支持传预设 key
    if raw in preset_dirs():
        raw = str(preset_dirs()[raw])

    target, err = resolve_target_dir(raw)
    if err:
        return jsonify({"ok": False, "error": err}), 400

    old = CONFIG["recv_dir"]
    CONFIG["recv_dir"] = target
    save_config()

    return jsonify({
        "ok": True,
        "current": dir_info(target),
        "previous": dir_info(old) if old else None,
    })


@app.route("/api/dirs/mkdir", methods=["POST"])
def api_dirs_mkdir():
    """
    在指定目录下新建文件夹。
    body: {"parent": "/path/to/parent", "name": "新文件夹"}
    """
    data = request.get_json(silent=True) or {}
    parent_raw = data.get("parent", "")
    name = (data.get("name") or "").strip()

    if not name:
        return jsonify({"ok": False, "error": "文件夹名不能为空"}), 400

    # 名字合法性校验
    name = unicodedata.normalize("NFC", name)
    if name in (".", ".."):
        return jsonify({"ok": False, "error": "这个名字不能用"}), 400
    if "/" in name or "\\" in name:
        return jsonify({"ok": False, "error": "名字里不能含斜杠"}), 400
    if "\x00" in name:
        return jsonify({"ok": False, "error": "名字含非法字符"}), 400
    if len(name) > 120:
        return jsonify({"ok": False, "error": "名字太长了（最多 120 字符）"}), 400
    # 去掉首尾空格和点
    name = name.strip(" .")
    if not name:
        return jsonify({"ok": False, "error": "文件夹名不能为空"}), 400

    # 父目录校验（必须在主目录内）
    if parent_raw:
        parent, err = resolve_target_dir(parent_raw)
        if err:
            return jsonify({"ok": False, "error": err}), 400
    else:
        parent = Path.home()

    target = parent / name

    if target.exists():
        return jsonify({
            "ok": False,
            "error": "这个文件夹已经存在了",
            "exists": True,
            "path": str(target),
        }), 409

    try:
        target.mkdir(parents=False, exist_ok=False)
    except FileExistsError:
        return jsonify({"ok": False, "error": "这个文件夹已经存在了", "exists": True}), 409
    except PermissionError:
        return jsonify({"ok": False, "error": "这个位置没有写入权限"}), 403
    except OSError as e:
        return jsonify({"ok": False, "error": f"创建失败：{e}"}), 500

    return jsonify({
        "ok": True,
        "name": name,
        "path": str(target),
        "display": dir_info(target)["display"],
        "parent": str(parent),
    })


@app.route("/api/dirs/browse")
def api_dirs_browse():
    """
    浏览主目录下的文件夹，供手机端点选。
    只允许主目录树内，隐藏隐藏文件夹。
    """
    raw = request.args.get("path", "")
    if raw:
        base, err = resolve_target_dir(raw)
        if err:
            return jsonify({"ok": False, "error": err}), 400
    else:
        base = Path.home()

    subdirs = []
    try:
        entries = sorted(
            base.iterdir(),
            key=lambda p: p.name.lower(),
        )
    except PermissionError:
        return jsonify({"ok": False, "error": "没有权限读取"}), 403

    for p in entries:
        if p.name.startswith("."):
            continue
        try:
            if not p.is_dir():
                continue
        except OSError:
            continue
        # 跳过无权限的
        if not os.access(p, os.R_OK):
            continue
        subdirs.append({
            "name": p.name,
            "path": str(p),
            "is_current": (p == CONFIG["recv_dir"]),
        })

    parent = None
    home = Path.home().resolve()
    if base.resolve() != home:
        try:
            base.resolve().relative_to(home)
            parent = str(base.parent)
        except ValueError:
            parent = None

    return jsonify({
        "ok": True,
        "path": str(base),
        "display": dir_info(base)["display"],
        "parent": parent,
        "subdirs": subdirs[:200],
        "is_current": (base == CONFIG["recv_dir"]),
    })


# 能直接在手机浏览器里预览的类型
PREVIEW_IMAGE = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg", ".avif", ".heic", ".heif"}
PREVIEW_VIDEO = {".mp4", ".mov", ".m4v", ".webm"}
PREVIEW_AUDIO = {".mp3", ".m4a", ".wav", ".aac", ".flac"}
PREVIEW_TEXT = {".txt", ".md", ".csv", ".json", ".log", ".py", ".js", ".html",
                ".css", ".xml", ".yml", ".yaml", ".sh", ".ini", ".conf", ".srt"}


def preview_type(name: str) -> str:
    ext = Path(name).suffix.lower()
    if ext in PREVIEW_IMAGE:
        return "image"
    if ext in PREVIEW_VIDEO:
        return "video"
    if ext in PREVIEW_AUDIO:
        return "audio"
    if ext in PREVIEW_TEXT:
        return "text"
    return "none"


def describe_file(p: Path) -> dict:
    """文件的展示信息（含传入时间、预览能力）"""
    st = p.stat()
    with_preview = preview_type(p.name)
    return {
        "name": p.name,
        "size": st.st_size,
        "size_h": human_size(st.st_size),
        "mtime": int(st.st_mtime),
        "mtime_h": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)),
        "ctime": int(st.st_birthtime) if hasattr(st, "st_birthtime") else int(st.st_ctime),
        "is_new": (time.time() - st.st_mtime) < 86400,
        "kind": file_kind(p.name),
        "preview": with_preview,
        "path": str(p),
    }


@app.route("/api/files")
def api_files():
    """
    列出文件。
    - 不传参数：返回接收目录 + 常用目录的分组
    - ?path=xxx：返回该目录下的文件与子目录（真正的文件浏览器）
    - ?sort=name|time|size
    """
    raw_path = request.args.get("path", "")
    sort_by = request.args.get("sort", "time")

    # ---- 单目录浏览模式 ----
    if raw_path:
        folder, err = resolve_target_dir(raw_path)
        if err:
            return jsonify({"ok": False, "error": err}), 400

        try:
            entries = list(folder.iterdir())
        except PermissionError:
            return jsonify({"ok": False, "error": "没有权限读取此目录"}), 403

        files, subdirs = [], []
        for p in entries:
            if p.name.startswith("."):
                continue
            try:
                if p.is_dir():
                    subdirs.append({
                        "name": p.name,
                        "path": str(p),
                        "mtime": int(p.stat().st_mtime),
                    })
                elif p.is_file():
                    files.append(describe_file(p))
            except (OSError, ValueError):
                continue

        # 排序
        if sort_by == "name":
            files.sort(key=lambda f: f["name"].lower())
            subdirs.sort(key=lambda d: d["name"].lower())
        elif sort_by == "size":
            files.sort(key=lambda f: f["size"], reverse=True)
            subdirs.sort(key=lambda d: d["name"].lower())
        else:  # time：最新的在前
            files.sort(key=lambda f: f["mtime"], reverse=True)
            subdirs.sort(key=lambda d: d["mtime"], reverse=True)

        info = dir_info(folder)

        # 上级目录
        parent = None
        home = Path.home().resolve()
        if folder.resolve() != home:
            try:
                folder.resolve().relative_to(home)
                parent = str(folder.parent)
            except ValueError:
                parent = None

        return jsonify({
            "ok": True,
            "mode": "browse",
            "path": str(folder),
            "display": info["display"],
            "name": folder.name or info["display"],
            "parent": parent,
            "is_current": (folder == CONFIG["recv_dir"]),
            "subdirs": subdirs[:100],
            "files": files[:300],
            "total": len(files),
            "sort": sort_by,
        })

    # ---- 分组模式（默认）----
    presets = {
        "recv": CONFIG["recv_dir"],
        "desktop": Path.home() / "Desktop",
        "downloads": Path.home() / "Downloads",
        "pictures": Path.home() / "Pictures",
    }

    labels = {
        "recv": "📥 接收目录",
        "desktop": "🖥 桌面",
        "downloads": "⬇️ 下载",
        "pictures": "🖼 图片",
    }

    result = []
    seen_paths = set()
    for key, folder in presets.items():
        if not folder or not folder.exists():
            continue
        # 避免重复（接收目录可能就在下载里）
        try:
            rp = folder.resolve()
        except Exception:
            continue
        if key != "recv" and rp in seen_paths:
            continue
        seen_paths.add(rp)

        items = []
        try:
            entries = list(folder.iterdir())
        except PermissionError:
            continue
        entries.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
        for p in entries:
            if len(items) >= 120:
                break
            if not p.is_file() or p.name.startswith("."):
                continue
            try:
                items.append(describe_file(p))
            except (OSError, ValueError):
                continue

        info = dir_info(folder)
        result.append({
            "key": key,
            "label": labels.get(key, key),
            "sublabel": info["display"],
            "path": str(folder),
            "files": items,
            "total": info["file_count"],
        })

    return jsonify({"ok": True, "mode": "group", "groups": result})


@app.route("/api/preview")
def api_preview():
    """
    文件内联预览：
    - 图片/视频/音频：直接流式返回（浏览器原生渲染）
    - 文本：返回前 N 字符，前端自己渲染
    - 其他：返回 415，前端提示不支持预览
    """
    raw_path = request.args.get("path", "")
    folder, err = resolve_target_dir(raw_path)
    if err:
        folder = Path(os.path.expanduser(raw_path))
        if not folder.exists():
            abort(404)

    if not folder.is_file():
        abort(404)

    ptype = preview_type(folder.name)

    # 文本：直接给内容
    if ptype == "text":
        try:
            if folder.stat().st_size > 2 * 1024 * 1024:
                return jsonify({"ok": False, "error": "文本太大，请下载查看"}), 413
            text = folder.read_text(encoding="utf-8", errors="replace")
            return jsonify({
                "ok": True,
                "type": "text",
                "name": folder.name,
                "text": text[:200000],
                "truncated": len(text) > 200000,
            })
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    if ptype == "none":
        return jsonify({"ok": False, "error": "这种文件不支持预览，请下载"}), 415

    # 图片/视频/音频：inline 返回，支持 Range（视频拖动进度条要用）
    return send_file(
        str(folder),
        as_attachment=False,
        download_name=folder.name,
        conditional=True,
    )


@app.route("/api/download")
def api_download():
    # 优先按真实路径下载（新文件浏览器用）
    raw_path = request.args.get("path", "")
    if raw_path:
        target = Path(os.path.expanduser(raw_path))
        try:
            target = target.resolve()
        except Exception:
            abort(404)
        # 安全：必须位于主目录内
        try:
            target.relative_to(Path.home().resolve())
        except ValueError:
            abort(403)
        if not target.is_file():
            abort(404)
        return send_from_directory(
            str(target.parent), target.name, as_attachment=True
        )

    # 兼容旧接口
    dir_key = request.args.get("dir", "recv")
    name = safe_name(request.args.get("name", ""))
    presets = {
        "recv": CONFIG["recv_dir"],
        "desktop": Path.home() / "Desktop",
        "downloads": Path.home() / "Downloads",
        "pictures": Path.home() / "Pictures",
    }
    folder = presets.get(dir_key)
    if not folder:
        abort(404)
    target = folder / name
    if not target.is_file():
        abort(404)
    return send_from_directory(str(folder), name, as_attachment=True)


@app.route("/api/open_folder", methods=["POST"])
def api_open_folder():
    """在 Finder 中打开接收目录"""
    try:
        subprocess.Popen(["open", str(CONFIG["recv_dir"])])
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ---------------------------------------------------------------- 路由：剪贴板

@app.route("/api/clip", methods=["GET", "POST"])
def api_clip():
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        text = data.get("text", "")
        CLIP["text"] = text
        CLIP["ts"] = time.time()
        CLIP["source"] = "phone"
        ok, how = set_mac_clipboard(text) if text else (False, "empty")

        # 写入后回读，把实际读到的内容也返回，方便用户自己核对
        actual = ""
        if ok:
            try:
                actual = get_mac_clipboard()
            except Exception:
                actual = ""

        return jsonify({
            "ok": True,
            "pushed_to_mac": ok,
            "method": how if ok else None,
            "error": None if ok else how,
            "matched": (actual.rstrip("\n") == text.rstrip("\n")) if ok else False,
            "actual_len": len(actual) if actual else 0,
            "sent_len": len(text),
            "ts": CLIP["ts"],
        })

    # GET：返回手机写入的内容；带 from_mac=1 时读取 Mac 剪贴板
    if request.args.get("from_mac") == "1":
        mac_text = get_mac_clipboard()
        # 检测是否存在异常字符
        has_replacement = "\ufffd" in mac_text
        return jsonify({
            "ok": True,
            "text": mac_text,
            "ts": int(time.time() * 1000),
            "source": "mac",
            "length": len(mac_text),
            "has_replacement": has_replacement,
        })

    return jsonify({
        "ok": True,
        "text": CLIP["text"],
        "ts": int(CLIP["ts"] * 1000),
        "source": CLIP["source"],
    })


@app.route("/api/clip/debug")
def api_clip_debug():
    """
    剪贴板诊断：看看 Mac 剪贴板里到底有什么格式、读出来是什么。
    排查「显示乱码」时很有用。
    """
    info = {"ok": True}

    # 剪贴板里有哪些类型
    try:
        r = subprocess.run(
            ["osascript", "-e", "clipboard info"],
            capture_output=True, timeout=3, text=True,
        )
        info["types"] = r.stdout.strip()
    except Exception as e:
        info["types"] = f"读取失败: {e}"

    # 默认 pbpaste 的原始字节（前 200 字节的十六进制）
    try:
        r = subprocess.run(["pbpaste"], capture_output=True, timeout=3)
        raw = r.stdout
        info["raw_len"] = len(raw)
        info["raw_hex_head"] = raw[:120].hex()
        # 各编码解码结果
        attempts = {}
        for enc in ("utf-8", "gb18030", "big5", "utf-16-le", "utf-16-be"):
            try:
                attempts[enc] = raw.decode(enc)[:80]
            except Exception:
                attempts[enc] = None
        info["decodings"] = attempts
    except Exception as e:
        info["raw_len"] = -1
        info["raw_error"] = str(e)

    # 最终结果
    text = get_mac_clipboard()
    info["final_text"] = text
    info["final_len"] = len(text)
    info["has_replacement"] = "\ufffd" in text
    info["has_zero_width"] = any(
        c in text for c in ("\u200b", "\u200c", "\u200d", "\ufeff")
    )

    return jsonify(info)


@app.route("/api/clip/debug/raw")
def api_clip_debug_raw():
    """返回剪贴板原始字节，用于深度排查"""
    try:
        r = subprocess.run(["pbpaste"], capture_output=True, timeout=3)
        return r.stdout, 200, {"Content-Type": "application/octet-stream"}
    except Exception as e:
        return str(e), 500


# ---------------------------------------------------------------- 启动

def wait_for_network(timeout=5.0):
    """等待 WiFi 就绪"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        ip = get_local_ip()
        if ip and ip != "127.0.0.1":
            return ip
        time.sleep(0.3)
    return get_local_ip()


def print_banner(url: str, recv: Path, auto_sort: bool = False):
    try:
        import qrcode
        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.make(fit=True)
        buf = io.StringIO()
        qr.print_ascii(out=buf, invert=True)
        art = buf.getvalue()
    except Exception:
        art = "  (二维码生成失败，请手动输入地址)\n"

    info = dir_info(recv)
    line = "─" * 52
    print(f"\n\033[36m┌{line}┐\033[0m")
    print("\033[36m│\033[0m  \033[1mWiFiDrop · 局域网传输\033[0m                              \033[36m│\033[0m")
    print(f"\033[36m├{line}┤\033[0m")
    print("\033[36m│\033[0m  📱 手机扫码，或浏览器打开：")
    print(f"\033[36m│\033[0m     \033[1;33m{url}\033[0m")
    print(f"\033[36m├{line}┤\033[0m")
    for ln in art.rstrip("\n").split("\n"):
        print(f"\033[36m│\033[0m  {ln}")
    print(f"\033[36m├{line}┤\033[0m")
    print(f"\033[36m│\033[0m  📥 保存到：\033[1m{info['display']}\033[0m")
    print(f"\033[36m│\033[0m     完整路径：{recv}")
    print(f"\033[36m│\033[0m     已有 {info['file_count']} 个文件")
    print(f"\033[36m│\033[0m  🗂  自动分类：{'开（图片/视频/其他 分子目录）' if auto_sort else '关'}")
    print(f"\033[36m├{line}┤\033[0m")
    print("\033[36m│\033[0m  💡 保存位置可在手机页面里随时切换")
    print("\033[36m│\033[0m  ⏹  按 Ctrl+C 停止服务")
    print(f"\033[36m└{line}┘\033[0m\n")


def main():
    parser = argparse.ArgumentParser(description="WiFiDrop - 局域网快速传输")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="端口号")
    parser.add_argument("--dir", default=None, help="接收目录（留空则使用上次保存的目录）")
    parser.add_argument("--reset", action="store_true", help="重置为默认接收目录")
    args = parser.parse_args()

    # 配置文件：打包成 .app 时 .app 内部只读，放到用户数据目录
    if getattr(sys, "_MEIPASS", None):
        cfg_dir = Path.home() / "Library" / "Application Support" / "WiFiDrop"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        cfg_file = cfg_dir / "config.json"
    else:
        cfg_file = Path(__file__).resolve().parent / ".wifidrop.json"
    CONFIG["config_file"] = cfg_file
    CONFIG["port"] = args.port

    # 目录优先级：命令行 --dir > 上次保存 > 默认
    if args.dir:
        recv, err = resolve_target_dir(args.dir)
        if err:
            print(f"\033[31m目录无效：{err}\033[0m")
            sys.exit(1)
    else:
        if args.reset and cfg_file.exists():
            try:
                cfg_file.unlink()
            except Exception:
                pass
        recv = default_recv_dir()
        recv.mkdir(parents=True, exist_ok=True)
        CONFIG["recv_dir"] = recv
        load_config()          # 若上次保存过，会覆盖 recv
        # 恢复自动分类开关
        try:
            if cfg_file.exists():
                d = json.loads(cfg_file.read_text(encoding="utf-8"))
                AUTO_SORT["on"] = bool(d.get("auto_sort", False))
        except Exception:
            pass
        recv = CONFIG["recv_dir"]

    CONFIG["recv_dir"] = recv
    save_config()

    ip = wait_for_network()
    url = f"http://{ip}:{args.port}"

    print_banner(url, recv, AUTO_SORT["on"])

    try:
        app.run(
            host="0.0.0.0",
            port=args.port,
            debug=False,
            threaded=True,
            use_reloader=False,
        )
    except OSError as e:
        print(f"\n\033[31m启动失败：{e}\033[0m")
        print("端口可能被占用，换一个：python3 server.py --port 9000\n")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n\033[36m已停止，再见 👋\033[0m\n")


if __name__ == "__main__":
    main()
