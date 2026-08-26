"""跨平台启动与诊断千帆客服工作台。

macOS 优先从 /Applications 或 ~/Applications 查找官方客户端，Windows 则从
LOCALAPPDATA/Program Files 查找。用户也可通过 XHS_QIANFAN_APP_PATH 显式指定。
"""
from __future__ import annotations

import importlib.util
import json
import os
import platform
import subprocess
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from pathlib import Path
from typing import Any, Mapping


OFFICIAL_DOWNLOAD_URL = "https://walle.xiaohongshu.com/client-update/"
DEFAULT_CDP_PORT = 9222


def system_name(value: str | None = None) -> str:
    return value or platform.system()


def cdp_port(env: Mapping[str, str] | None = None) -> int:
    source = os.environ if env is None else env
    raw = source.get("XHS_QIANFAN_CDP_PORT")
    if not raw and source.get("XHS_CDP_BASE"):
        parsed = urlsplit(source["XHS_CDP_BASE"])
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
            raise ValueError("XHS_CDP_BASE 只允许本机 http://127.0.0.1 或 localhost 地址")
        raw = str(parsed.port or DEFAULT_CDP_PORT)
    raw = raw or str(DEFAULT_CDP_PORT)
    try:
        port = int(raw)
    except ValueError as exc:
        raise ValueError("XHS_QIANFAN_CDP_PORT 必须是有效端口号") from exc
    if not 1 <= port <= 65535:
        raise ValueError("XHS_QIANFAN_CDP_PORT 必须在 1～65535 之间")
    return port


def cdp_base_url(env: Mapping[str, str] | None = None) -> str:
    return f"http://127.0.0.1:{cdp_port(env)}"


def default_profile_dir(
    *, system: str | None = None, home: Path | None = None, env: Mapping[str, str] | None = None
) -> Path:
    source = os.environ if env is None else env
    configured = source.get("XHS_QIANFAN_PROFILE")
    if configured:
        return Path(configured).expanduser()
    home_dir = home or Path.home()
    current = system_name(system)
    if current == "Darwin":
        return home_dir / "Library" / "Application Support" / "xhs-kefu-demo" / "qianfan-profile"
    if current == "Windows":
        base = Path(source.get("LOCALAPPDATA", str(home_dir / "AppData" / "Local")))
        return base / "xhs-kefu-demo" / "qianfan-profile"
    return home_dir / ".local" / "share" / "xhs-kefu-demo" / "qianfan-profile"


def chromium_args(*, system: str | None = None) -> list[str]:
    args = ["--disable-blink-features=AutomationControlled"]
    if system_name(system) == "Linux":
        args.append("--no-sandbox")
    return args


def app_candidates(
    *, system: str | None = None, home: Path | None = None, env: Mapping[str, str] | None = None
) -> list[Path]:
    source = os.environ if env is None else env
    configured = source.get("XHS_QIANFAN_APP_PATH")
    if configured:
        return [Path(configured).expanduser()]
    home_dir = home or Path.home()
    current = system_name(system)
    if current == "Darwin":
        return [
            Path("/Applications/千帆客服工作台.app"),
            home_dir / "Applications" / "千帆客服工作台.app",
            Path("/Applications/eva.app"),
            home_dir / "Applications" / "eva.app",
        ]
    if current == "Windows":
        local = Path(source.get("LOCALAPPDATA", str(home_dir / "AppData" / "Local")))
        program_files = Path(source.get("ProgramFiles", "C:/Program Files"))
        program_files_x86 = Path(source.get("ProgramFiles(x86)", "C:/Program Files (x86)"))
        return [
            local / "Programs" / "eva" / "千帆客服工作台.exe",
            program_files / "eva" / "千帆客服工作台.exe",
            program_files_x86 / "eva" / "千帆客服工作台.exe",
        ]
    return []


def find_app_path(
    *, system: str | None = None, home: Path | None = None, env: Mapping[str, str] | None = None
) -> Path | None:
    return next(
        (candidate for candidate in app_candidates(system=system, home=home, env=env) if candidate.exists()),
        None,
    )


def build_launch_command(app_path: Path, *, system: str | None = None, port: int | None = None) -> list[str]:
    current = system_name(system)
    debug_port = port or cdp_port()
    debug_args = [
        f"--remote-debugging-port={debug_port}",
        f"--remote-allow-origins=http://127.0.0.1:{debug_port}",
    ]
    if current == "Darwin" and app_path.suffix.lower() == ".app":
        return ["open", "-na", str(app_path), "--args", *debug_args]
    return [str(app_path), *debug_args]


def cdp_ready(*, env: Mapping[str, str] | None = None, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(f"{cdp_base_url(env)}/json/version", timeout=timeout) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError, ValueError):
        return False


def api_ready(url: str | None = None, *, timeout: float = 2.0) -> bool:
    base = (url or os.environ.get("XHS_DECISION_URL") or "http://127.0.0.1:18081").rstrip("/")
    try:
        with urllib.request.urlopen(f"{base}/health", timeout=timeout) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def launch_qianfan(*, wait_seconds: float = 20.0) -> dict[str, Any]:
    if cdp_ready():
        return {"ok": True, "already_running": True, "cdp_url": cdp_base_url()}
    app_path = find_app_path()
    if app_path is None:
        searched = [str(item) for item in app_candidates()]
        return {
            "ok": False,
            "error": "未找到千帆客服工作台客户端",
            "searched": searched,
            "download_url": OFFICIAL_DOWNLOAD_URL,
            "hint": "安装官方客户端，或设置 XHS_QIANFAN_APP_PATH 指向 .app/.exe。",
        }
    command = build_launch_command(app_path)
    try:
        subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        return {"ok": False, "error": f"启动千帆失败：{exc}", "command": command}
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if cdp_ready():
            return {
                "ok": True,
                "already_running": False,
                "app_path": str(app_path),
                "cdp_url": cdp_base_url(),
                "command": command,
            }
        time.sleep(0.5)
    return {
        "ok": False,
        "error": "千帆客户端已启动，但 CDP 调试端口尚未就绪",
        "app_path": str(app_path),
        "cdp_url": cdp_base_url(),
        "hint": "请完全退出已打开的千帆客户端后重试；macOS 若仍失败，可改用 python run.py login / worker 网页模式。",
    }


def doctor() -> dict[str, Any]:
    current = system_name()
    app_path = find_app_path()
    websocket_installed = importlib.util.find_spec("websocket") is not None
    python_ok = sys.version_info >= (3, 11)
    report = {
        "platform": current,
        "python": platform.python_version(),
        "python_ok": python_ok,
        "websocket_client": websocket_installed,
        "qianfan_app": str(app_path) if app_path else None,
        "qianfan_app_found": app_path is not None,
        "cdp_url": cdp_base_url(),
        "cdp_ready": cdp_ready(),
        "decision_api_ready": api_ready(),
        "api_auth_configured": bool(os.environ.get("XHS_API_KEY", "").strip()),
        "official_download": OFFICIAL_DOWNLOAD_URL,
    }
    report["ready"] = bool(
        python_ok and websocket_installed and (report["cdp_ready"] or report["qianfan_app_found"])
    )
    return report


def print_doctor(report: dict[str, Any]) -> None:
    marks = {True: "✅", False: "❌"}
    print(f"平台：{report['platform']} · Python {report['python']}")
    print(f"{marks[report['python_ok']]} Python 3.11+")
    print(f"{marks[report['websocket_client']]} websocket-client")
    print(f"{marks[report['qianfan_app_found']]} 千帆客户端：{report['qianfan_app'] or '未找到'}")
    print(f"{marks[report['cdp_ready']]} CDP：{report['cdp_url']}")
    print(f"{'✅' if report['decision_api_ready'] else 'ℹ️'} 决策 API：{'已启动' if report['decision_api_ready'] else '尚未启动'}")
    print(f"{'✅' if report['api_auth_configured'] else '⚠️'} API 鉴权：{'已配置' if report['api_auth_configured'] else '未配置（接入真实顾客前请设置 XHS_API_KEY）'}")
    if not report["qianfan_app_found"]:
        print(f"官方下载：{report['official_download']}")
    if system_name() == "Darwin":
        print("macOS 提醒需要在“系统设置 → 隐私与安全性 → 自动化/通知”中允许终端。")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="千帆客服工作台跨平台启动与诊断")
    parser.add_argument("action", choices=("doctor", "launch"), nargs="?", default="doctor")
    args = parser.parse_args()
    if args.action == "doctor":
        report = doctor()
        print_doctor(report)
        raise SystemExit(0 if report["ready"] else 1)
    result = launch_qianfan()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result.get("ok") else 1)


if __name__ == "__main__":
    main()
