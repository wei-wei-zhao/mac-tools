#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WiFiDrop 发布脚本
把打包好的 .app 打成 zip，生成 version.json，供自动更新使用。

用法：
    python3 release.py 1.1.0                      # 版本号必填
    python3 release.py 1.1.0 --base-url https://your-server.com/wifidrop/
    python3 release.py 1.1.0 --notes "修复了 bug"

产出（在 dist/release/ 下）：
    WiFiDrop-1.1.0.zip
    version.json
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

BASE = Path(__file__).resolve().parent
DIST = BASE / "dist"
APP = DIST / "WiFiDrop.app"


def sha256_of(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def bump_version_in_source(ver: str):
    """把版本号写进 updater.py / server.py，保证两边一致"""
    for fname, pattern in [
        ("updater.py", 'APP_VERSION = "'),
        ("server.py", 'APP_VERSION = "'),
    ]:
        f = BASE / fname
        if not f.exists():
            continue
        lines = f.read_text(encoding="utf-8").splitlines(keepends=True)
        out = []
        for ln in lines:
            if ln.startswith(pattern):
                out.append(f'{pattern}{ver}"\n')
            else:
                out.append(ln)
        f.write_text("".join(out), encoding="utf-8")
    print(f"  版本号已写入源码: {ver}")


def set_builtin_update_url(url: str, fallback: str = ""):
    """
    把更新地址预置进 updater.py 的 BUILTIN_UPDATE_URL。
    这样同事装上就能自动更新，不用手动配置。
    fallback 是主机名解析失败时的备用地址（通常是 IP 版）。
    """
    f = BASE / "updater.py"
    if not f.exists():
        return
    lines = f.read_text(encoding="utf-8").splitlines(keepends=True)
    out = []
    seen_main = seen_fb = False
    for ln in lines:
        if ln.startswith("BUILTIN_UPDATE_URL = "):
            out.append(f'BUILTIN_UPDATE_URL = "{url}"\n')
            seen_main = True
        elif ln.startswith("BUILTIN_UPDATE_URL_FALLBACK = "):
            out.append(f'BUILTIN_UPDATE_URL_FALLBACK = "{fallback}"\n')
            seen_fb = True
        else:
            out.append(ln)

    if not seen_main or not seen_fb:
        res = []
        for ln in out:
            res.append(ln)
            if ln.startswith("APP_VERSION = "):
                if not seen_main:
                    res.append(f'BUILTIN_UPDATE_URL = "{url}"\n')
                if not seen_fb:
                    res.append(f'BUILTIN_UPDATE_URL_FALLBACK = "{fallback}"\n')
        out = res

    f.write_text("".join(out), encoding="utf-8")
    print(f"  主更新地址: {url or '(空)'}")
    if fallback:
        print(f"  回退地址:   {fallback}")


def detect_local_hosts() -> tuple:
    """
    探测本机可用于内网访问的地址：
    返回 (hostname_url_prefix, ip_url_prefix)
    """
    host = ""
    ip = ""
    try:
        r = subprocess.run(["scutil", "--get", "LocalHostName"],
                           capture_output=True, text=True, timeout=3)
        h = r.stdout.strip()
        if h:
            host = f"http://{h}.local"
    except Exception:
        pass
    try:
        r = subprocess.run(["ipconfig", "getifaddr", "en0"],
                           capture_output=True, text=True, timeout=3)
        v = r.stdout.strip()
        if v:
            ip = f"http://{v}"
    except Exception:
        pass
    return host, ip


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("version", help="版本号，如 1.1.0")
    ap.add_argument("--base-url", default="", help="zip 的对外下载地址前缀")
    ap.add_argument("--local", action="store_true",
                    help="用本机做更新源（自动取主机名 + IP，端口 8790）")
    ap.add_argument("--port", type=int, default=8790, help="配合 --local，更新服务器端口")
    ap.add_argument("--notes", default="", help="更新说明")
    ap.add_argument("--skip-build", action="store_true", help="跳过打包，直接用现有 dist")
    args = ap.parse_args()

    ver = args.version.strip()

    # 1. 版本号 + 预置更新地址写入源码
    print(f"\n[1/4] 写入版本号 {ver}")
    bump_version_in_source(ver)

    base_url = args.base_url.rstrip("/")
    fallback_url = ""

    if args.local:
        host_pfx, ip_pfx = detect_local_hosts()
        if not host_pfx and not ip_pfx:
            print("  ❌ 无法探测本机地址")
            sys.exit(1)
        # 主地址用主机名（IP 变了也能找到），回退用当前 IP
        base_url = f"{host_pfx}:{args.port}" if host_pfx else f"{ip_pfx}:{args.port}"
        fallback_url = f"{ip_pfx}:{args.port}" if ip_pfx else ""
        print(f"  本机更新源模式，端口 {args.port}")

    if base_url:
        manifest_url = f"{base_url}/version.json"
        fb_manifest = f"{fallback_url}/version.json" if fallback_url else ""
        set_builtin_update_url(manifest_url, fb_manifest)
        print(f"  同事端将自动从这个地址检查更新")
    else:
        set_builtin_update_url("", "")
        print("  ⚠️ 未指定 --base-url 或 --local，同事端不会自动更新")

    # 2. 打包
    if not args.skip_build:
        print("\n[2/4] 打包 .app（约需 1-3 分钟）")
        r = subprocess.run(
            [sys.executable, "-m", "PyInstaller", "wifidrop.spec",
             "--noconfirm", "--log-level", "WARN"],
            cwd=str(BASE),
        )
        if r.returncode != 0:
            print("❌ 打包失败")
            sys.exit(1)
    else:
        print("\n[2/4] 跳过打包")

    if not APP.exists():
        print(f"❌ 找不到 {APP}")
        sys.exit(1)

    # 3. 打 zip
    print("\n[3/4] 生成发布包")
    rel = DIST / "release"
    rel.mkdir(parents=True, exist_ok=True)

    zip_name = f"WiFiDrop-{ver}.zip"
    zip_path = rel / zip_name

    if zip_path.exists():
        zip_path.unlink()

    # 用 ditto 保留 .app 的符号链接和权限（zipfile 会破坏 .app 结构）
    r = subprocess.run(
        ["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent",
         str(APP), str(zip_path)],
        capture_output=True,
    )
    if r.returncode != 0 or not zip_path.exists():
        # 退回 zipfile（可能损坏 .app，但至少能用）
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            for f in APP.rglob("*"):
                z.write(f, f.relative_to(DIST))

    size_mb = zip_path.stat().st_size / 1024 / 1024
    digest = sha256_of(zip_path)
    print(f"  {zip_name}  ({size_mb:.1f} MB)")
    print(f"  sha256: {digest}")

    # 4. 生成 version.json
    print("\n[4/4] 生成 version.json")
    # 注意：这里用相对路径，update 那边 resolve_url 会自动拼成绝对地址，
    # 这样主机名和 IP 两种访问方式都能拿到同一个 zip
    manifest = {
        "version": ver,
        "url": zip_name,
        "sha256": digest,
        "size": zip_path.stat().st_size,
        "notes": args.notes or f"WiFiDrop {ver}",
        "released": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    mf = rel / "version.json"
    mf.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"  {mf}")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))

    print("\n" + "=" * 60)
    print("发布完成。把 dist/release/ 里这两个文件传到你的服务器：")
    print(f"  1. {zip_name}")
    print("  2. version.json")
    print("")
    print("然后把 version.json 的地址填进每个同事的 WiFiDrop：")
    print("  菜单栏 ⚡ → 更新设置… → 粘贴地址")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()
