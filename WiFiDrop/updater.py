#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WiFiDrop 自动更新模块
静默下载新版本，下次启动生效；启动失败自动回退上一版。

设计要点：
- 更新包格式：zip，内含 WiFiDrop.app 或 PyInstaller 的 onedir 产物
- 版本清单：远端 version.json  {"version":"1.1.0","url":"...","sha256":"...","notes":"..."}
- 版本号比较：语义化版本 x.y.z
- 回退保护：启动时写入「待验证」标记，跑满 30 秒无异常才标记为健康，
           否则下次启动自动切回 previous 目录
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import zipfile
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import urlopen, Request, build_opener, ProxyHandler
from urllib.error import URLError, HTTPError

APP_VERSION = "1.4.5"
CHECK_INTERVAL = 6 * 3600      # 6 小时查一次
HEALTH_DELAY = 30              # 启动后 30 秒内无异常才算健康


# ---------------------------------------------------------------- 网络请求
# 企业环境常配 HTTP 代理，访问内网地址会 502。
# 内网/本机/私有网段一律绕过代理直连。

_NO_PROXY_HOSTS = (
    "localhost", "127.0.0.1", "::1",
    ".local",             # Bonjour 主机名
)


def _is_local_url(url: str) -> bool:
    """判断是否内网/本机地址，这类请求必须绕过代理"""
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    if not host:
        return False
    if host in ("localhost", "127.0.0.1", "::1"):
        return True
    if host.endswith(".local"):
        return True
    # 私有网段
    if host.startswith("10.") or host.startswith("192.168."):
        return True
    if host.startswith("172."):
        try:
            second = int(host.split(".")[1])
            if 16 <= second <= 31:
                return True
        except (IndexError, ValueError):
            pass
    return False


def _open(url: str, timeout: int = 8):
    """
    统一的请求入口：内网地址直连（绕过代理），外网地址用系统代理。
    """
    req = Request(url, headers={"User-Agent": f"WiFiDrop/{APP_VERSION}"})
    if _is_local_url(url):
        # 绕过所有代理直连
        opener = build_opener(ProxyHandler({}))
        return opener.open(req, timeout=timeout)
    return urlopen(req, timeout=timeout)


# ---------------------------------------------------------------- 路径

def is_bundled() -> bool:
    """是否运行在 PyInstaller 打包环境里"""
    return bool(getattr(sys, "frozen", False))


def app_bundle_path() -> Path | None:
    """当前 .app 的路径（打包后有效）"""
    if not is_bundled():
        return None
    # sys.executable = /path/WiFiDrop.app/Contents/MacOS/WiFiDrop
    exe = Path(sys.executable).resolve()
    for parent in exe.parents:
        if parent.suffix == ".app":
            return parent
    return None


def update_dir() -> Path:
    """更新工作目录"""
    d = Path.home() / "Library" / "Application Support" / "WiFiDrop"
    d.mkdir(parents=True, exist_ok=True)
    return d


def state_file() -> Path:
    return update_dir() / "update_state.json"


def config_file() -> Path:
    return update_dir() / "config.json"


# ---------------------------------------------------------------- 配置

# 发布时预置的更新地址（打包前由 release.py 自动填入）
# 用户端无需配置，开箱即用；用户也可在菜单里改成自己的地址
# 留空表示不预置：用户首次使用时在「更新设置…」里填自己的地址即可
BUILTIN_UPDATE_URL = ""
# 回退地址：主机名（.local）解析失败时改试这个（通常是 IP 版地址）
BUILTIN_UPDATE_URL_FALLBACK = ""

DEFAULT_CONFIG = {
    "update_url": "",            # 用户自定义的 version.json 地址，优先于此
    "update_url_fallback": "",   # 用户自定义的回退地址
    "auto_update": True,         # 是否静默自动更新
    "last_check": 0,
    "check_interval": 6 * 3600,  # 检查间隔（秒），默认 6 小时
}


def effective_update_url(cfg: dict | None = None) -> str:
    """
    实际使用的更新地址：用户配置 > 内置预置
    """
    if cfg is None:
        cfg = load_cfg()
    user = (cfg.get("update_url") or "").strip()
    if user:
        return user
    return (BUILTIN_UPDATE_URL or "").strip()


def effective_fallback_url(cfg: dict | None = None) -> str:
    if cfg is None:
        cfg = load_cfg()
    user = (cfg.get("update_url_fallback") or "").strip()
    if user:
        return user
    return (BUILTIN_UPDATE_URL_FALLBACK or "").strip()


def load_cfg() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        f = config_file()
        if f.exists():
            cfg.update(json.loads(f.read_text(encoding="utf-8")))
        else:
            # 首次运行：写出默认配置，方便用户直接编辑或后续写入
            f.write_text(
                json.dumps(cfg, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
    except Exception:
        pass
    return cfg


def save_cfg(cfg: dict):
    try:
        f = config_file()
        data = {}
        if f.exists():
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        data.update(cfg)
        f.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------- 版本工具

def parse_ver(v: str) -> tuple:
    """把 1.2.3 解析成 (1,2,3)，非数字部分忽略"""
    nums = []
    for part in str(v).strip().lstrip("v").split("."):
        digits = "".join(c for c in part if c.isdigit())
        nums.append(int(digits) if digits else 0)
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums[:3])


def is_newer(remote: str, current: str) -> bool:
    return parse_ver(remote) > parse_ver(current)


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- 状态

def load_state() -> dict:
    try:
        f = state_file()
        if f.exists():
            return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"pending_health": False, "pending_version": "", "previous_ok": False}


def save_state(st: dict):
    try:
        state_file().write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------- 检查与下载

def fetch_manifest(url: str, timeout=8) -> dict | None:
    """
    获取版本清单。如果地址里的主机名解析失败，自动尝试回退地址。
    回退地址存在 cfg 的 update_url_fallback 里（同一份清单的 IP 版本）。
    """
    def _try(u):
        try:
            with _open(u, timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
            if isinstance(data, dict) and "version" in data:
                return data
        except Exception:
            return None
        return None

    data = _try(url)
    if data is not None:
        return data

    # 主机名失败 → 试回退地址
    try:
        cfg = load_cfg()
        fb = effective_fallback_url(cfg)
        if fb and fb != url:
            return _try(fb)
    except Exception:
        pass

    return None


def download(url: str, dest: Path, timeout=180) -> bool:
    """下载到临时文件，成功后原子替换。主机名失败时自动回退到 IP 地址。"""
    tmp = dest.with_suffix(dest.suffix + ".part")

    def _fetch(u) -> bool:
        try:
            with _open(u, timeout=timeout) as r, open(tmp, "wb") as f:
                total = int(r.headers.get("Content-Length") or 0)
                got = 0
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
                    if total and got > total * 1.5:
                        raise IOError("下载体积异常")

            size = tmp.stat().st_size
            if size == 0:
                raise IOError("下载内容为空")

            # 避免把 404 页面当包下载
            if not (zipfile.is_zipfile(tmp) or tarfile.is_tarfile(tmp)):
                head = tmp.read_bytes()[:300].lower() if size < 8192 else b""
                if b"<html" in head or b"not found" in head or b"404" in head:
                    raise IOError("服务器返回的不是更新包")

            tmp.replace(dest)
            return True
        except Exception:
            return False

    if _fetch(url):
        return True

    # 清理失败的临时文件
    try:
        if tmp.exists():
            tmp.unlink()
    except Exception:
        pass

    # 主机名失败 → 用回退地址重试
    try:
        cfg = load_cfg()
        fb = effective_fallback_url(cfg)
        if fb:
            from urllib.parse import urlparse
            filename = url.rsplit("/", 1)[-1]
            base = fb.rsplit("/", 1)[0]
            alt = f"{base}/{filename}"
            if alt != url and _fetch(alt):
                return True
    except Exception:
        pass

    try:
        if tmp.exists():
            tmp.unlink()
    except Exception:
        pass
    return False


def extract_package(pkg: Path, out_dir: Path) -> bool:
    """解压 zip 到 out_dir，兼容 .app 和 onedir 两种结构"""
    if out_dir.exists():
        shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        if zipfile.is_zipfile(pkg):
            with zipfile.ZipFile(pkg) as z:
                z.extractall(out_dir)
        elif tarfile.is_tarfile(pkg):
            with tarfile.open(pkg) as t:
                t.extractall(out_dir)
        else:
            return False
        return True
    except Exception:
        return False


def find_app_in(dirpath: Path) -> Path | None:
    """在解压结果里找 .app；找不到就当成 onedir 目录返回"""
    apps = list(dirpath.rglob("*.app"))
    if apps:
        # 优先选和当前同名的
        cur = app_bundle_path()
        if cur:
            for a in apps:
                if a.name == cur.name:
                    return a
        return apps[0]
    # onedir：找可执行文件
    for f in dirpath.rglob("WiFiDrop"):
        if f.is_file() and os.access(f, os.X_OK):
            return f.parent
    return None


# ---------------------------------------------------------------- 安装（延迟生效）

def stage_update(manifest: dict) -> bool:
    """
    下载并解压到 staging 目录，写入状态，
    真正的替换放到下次启动前做（避免动正在运行的进程）
    """
    url = manifest.get("url", "")
    ver = manifest.get("version", "")
    if not url or not ver:
        return False

    ud = update_dir()
    pkg = ud / f"update-{ver}.pkg"

    if not download(url, pkg):
        return False

    # 校验
    expect = (manifest.get("sha256") or "").lower()
    if expect:
        actual = sha256_of(pkg).lower()
        if actual != expect:
            try:
                pkg.unlink()
            except Exception:
                pass
            return False

    staging = ud / "staging"
    if not extract_package(pkg, staging):
        return False

    target = find_app_in(staging)
    if not target:
        return False

    st = load_state()
    st.update({
        "pending_install": True,
        "pending_version": ver,
        "pending_app": str(target),
        "staged_at": int(time.time()),
    })
    save_state(st)
    return True


def apply_pending_on_launch() -> bool:
    """
    在应用启动最早期调用：如果有待安装的更新，
    先备份当前版本，再替换。失败则忽略，继续用旧版。
    """
    if not is_bundled():
        return False
    st = load_state()
    if not st.get("pending_install"):
        return False

    cur = app_bundle_path()
    if not cur:
        return False

    new_app = Path(st.get("pending_app", ""))
    if not new_app.exists():
        st["pending_install"] = False
        save_state(st)
        return False

    ud = update_dir()
    backup = ud / "previous"

    try:
        # 备份当前版本
        if backup.exists():
            shutil.rmtree(backup, ignore_errors=True)
        shutil.copytree(cur, backup, symlinks=True)

        # 替换
        shutil.rmtree(cur, ignore_errors=True)
        shutil.move(str(new_app), str(cur))

        # 清理 staging
        shutil.rmtree(ud / "staging", ignore_errors=True)

        st.update({
            "pending_install": False,
            "pending_health": True,        # 等待健康确认
            "installed_version": st.get("pending_version", ""),
            "previous_ok": True,
        })
        save_state(st)
        return True
    except Exception:
        # 替换失败：尝试还原
        try:
            if backup.exists() and not cur.exists():
                shutil.copytree(backup, cur, symlinks=True)
        except Exception:
            pass
        st["pending_install"] = False
        save_state(st)
        return False


def rollback() -> bool:
    """回退到上一版本"""
    cur = app_bundle_path()
    backup = update_dir() / "previous"
    if not cur or not backup.exists():
        return False
    try:
        old = cur.with_name(cur.name + ".bad")
        try:
            shutil.move(str(cur), str(old))
        except Exception:
            pass
        shutil.copytree(backup, cur, symlinks=True)
        st = load_state()
        st["pending_health"] = False
        st["rolled_back"] = True
        save_state(st)
        return True
    except Exception:
        return False


def start_health_watchdog():
    """
    启动后开一个看门狗：
    - 30 秒内没异常 → 标记健康
    - 若上次更新后没等到健康就再次启动（说明上次崩了）→ 回退
    """
    st = load_state()

    # 上次更新后未能确认健康 → 判定为启动失败，回退
    if st.get("pending_health"):
        # 还在窗口期内，继续观察；只有标记超过 2 分钟仍是 pending 才算失败
        started = st.get("health_started_at", 0)
        if started and (time.time() - started) > 120:
            rollback()
            return
        st["health_started_at"] = int(time.time())
        save_state(st)

    def _mark_healthy():
        time.sleep(HEALTH_DELAY)
        s = load_state()
        if s.get("pending_health"):
            s["pending_health"] = False
            s["healthy_at"] = int(time.time())
            save_state(s)

    threading.Thread(target=_mark_healthy, daemon=True).start()


# ---------------------------------------------------------------- 主流程

def resolve_url(base_manifest_url: str, file_url: str) -> str:
    """
    清单里的 url 可能是相对路径（例如同目录部署时只写了 xxx.zip），
    这里基于 version.json 的地址解析成绝对 URL。
    """
    from urllib.parse import urljoin, urlparse
    if not file_url:
        return ""
    if urlparse(file_url).scheme:      # 已经是 http(s):// 等绝对地址
        return file_url
    return urljoin(base_manifest_url, file_url)


def check_and_update(silent=True) -> dict:
    """
    检查更新并静默暂存。返回结果摘要，供菜单展示。
    """
    cfg = load_cfg()
    url = effective_update_url(cfg)
    if not url:
        return {"ok": False, "reason": "未配置更新地址"}

    if not cfg.get("auto_update", True) and silent:
        return {"ok": False, "reason": "自动更新已关闭"}

    manifest = fetch_manifest(url)
    if not manifest:
        return {"ok": False, "reason": "无法获取版本信息"}

    remote = manifest.get("version", "")
    cur = current_version()

    if not is_newer(remote, cur):
        cfg["last_check"] = int(time.time())
        save_cfg(cfg)
        return {"ok": True, "updated": False, "current": cur, "remote": remote}

    # 相对 URL 转绝对
    manifest = dict(manifest)
    manifest["url"] = resolve_url(url, manifest.get("url", ""))

    # 有新版本，静默下载
    ok = stage_update(manifest)
    cfg["last_check"] = int(time.time())
    save_cfg(cfg)

    return {
        "ok": ok,
        "updated": ok,
        "current": cur,
        "remote": remote,
        "notes": manifest.get("notes", ""),
        "reason": "" if ok else "下载或校验失败",
    }


def current_version() -> str:
    """读当前版本：优先取状态里记录的已安装版本"""
    st = load_state()
    v = st.get("installed_version")
    if v:
        return v
    return APP_VERSION


def start_background_checker():
    """后台定时检查，不阻塞界面"""
    def _loop():
        # 启动 20 秒后再查，避免拖慢启动
        time.sleep(20)
        while True:
            try:
                check_and_update(silent=True)
            except Exception:
                pass
            # 间隔可配置（内网环境可调短）
            try:
                interval = int(load_cfg().get("check_interval") or CHECK_INTERVAL)
            except Exception:
                interval = CHECK_INTERVAL
            time.sleep(max(600, interval))     # 最少 10 分钟

    threading.Thread(target=_loop, daemon=True).start()


def relaunch():
    """重启自己（用于应用更新后）"""
    cur = app_bundle_path()
    if not cur:
        return
    try:
        subprocess.Popen(["open", "-n", str(cur)])
    except Exception:
        pass
    os._exit(0)
