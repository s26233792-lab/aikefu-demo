"""千帆人工介入的跨平台桌面提醒。

Windows 使用 Win32 置顶、任务栏闪烁和系统声音；macOS 使用 AppleScript 激活
千帆、发送系统通知并播放系统声音。失败时只降级提醒，不影响 Worker 主循环。
"""
from __future__ import annotations

import os
import subprocess
import sys
from threading import Thread


def _app_names() -> list[str]:
    configured = os.environ.get("XHS_QIANFAN_APP_NAME", "").strip()
    return [name for name in (configured, "千帆客服工作台", "eva") if name]


def _run_osascript(script: str) -> bool:
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _windows_find_hwnd() -> int | None:
    if sys.platform != "win32":
        return None
    import ctypes
    import ctypes.wintypes as wt

    user32 = ctypes.windll.user32
    result: list[int] = []
    title_prefixes = tuple(_app_names())
    enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)

    @enum_proc
    def callback(hwnd, _lparam):
        length = user32.GetWindowTextLengthW(hwnd)
        if length > 0:
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            if buffer.value.startswith(title_prefixes):
                result.append(hwnd)
                return False
        return True

    user32.EnumWindows(callback, 0)
    return result[0] if result else None


def bring_to_front() -> bool:
    """激活千帆窗口；未找到或缺少系统权限时返回 False。"""
    if sys.platform == "darwin":
        for name in _app_names():
            safe_name = name.replace("\\", "\\\\").replace('"', '\\"')
            if _run_osascript(f'tell application "{safe_name}" to activate'):
                return True
        return False
    if sys.platform != "win32":
        return False

    import ctypes

    hwnd = _windows_find_hwnd()
    if not hwnd:
        return False
    user32 = ctypes.windll.user32
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    user32.ShowWindow(hwnd, 5)  # SW_SHOW
    user32.SetForegroundWindow(hwnd)
    return True


def flash_taskbar(continuous: bool = True) -> None:
    """Windows 闪烁任务栏；macOS 发送通知作为等价提醒。"""
    if sys.platform == "darwin":
        message = "有会话需要人工立即处理" if continuous else "有一条回复等待人工审核"
        _run_osascript(
            f'display notification "{message}" with title "小红书千帆客服" sound name "Glass"'
        )
        return
    if sys.platform != "win32":
        return

    import ctypes
    import ctypes.wintypes as wt

    hwnd = _windows_find_hwnd()
    if not hwnd:
        return

    class FlashWindowInfo(ctypes.Structure):
        _fields_ = [
            ("cbSize", wt.UINT),
            ("hwnd", wt.HWND),
            ("dwFlags", wt.DWORD),
            ("uCount", wt.UINT),
            ("dwTimeout", wt.DWORD),
        ]

    info = FlashWindowInfo()
    info.cbSize = ctypes.sizeof(FlashWindowInfo)
    info.hwnd = hwnd
    info.dwFlags = 0x0000000C if continuous else 0x00000003
    info.uCount = 0 if continuous else 5
    info.dwTimeout = 0
    ctypes.windll.user32.FlashWindowEx(ctypes.byref(info))


def play_sound() -> None:
    """非阻塞播放平台系统提示音。"""
    def _play() -> None:
        try:
            if sys.platform == "darwin":
                subprocess.run(
                    ["afplay", "/System/Library/Sounds/Glass.aiff"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                )
            elif sys.platform == "win32":
                import winsound

                winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
            else:
                print("\a", end="", flush=True)
        except (OSError, subprocess.SubprocessError):
            pass

    Thread(target=_play, daemon=True).start()


def notify(level: str = "warning") -> dict:
    """综合提醒：激活窗口、系统通知/任务栏提醒、声音。"""
    brought = bring_to_front()
    flash_taskbar(continuous=level == "critical")
    # macOS 的 display notification 已带声音，避免重复播放。
    if sys.platform != "darwin":
        play_sound()
    return {"brought_to_front": brought, "level": level, "platform": sys.platform}


if __name__ == "__main__":
    print("提醒测试结果：", notify("critical"))
