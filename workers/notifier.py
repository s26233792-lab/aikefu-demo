"""人工介入提醒模块（桌面端，零额外依赖）。

当需要人工介入时，用 Win32 API 把真人从别处"拉回来关注千帆窗口"：
1. 窗口置顶：把千帆客服工作台窗口弹到最前（SetForegroundWindow / ShowWindow）；
2. 任务栏闪烁：FlashWindowEx 让任务栏图标不断闪烁，直到真人点击；
3. 声音提示：winsound 播放系统提示音。

这是"电脑前 + 频繁 + 及时"场景下最可靠的提醒方式——不依赖注入千帆 DOM
（刷新就丢），也不依赖额外的 Web 审批台页面。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import subprocess
import sys
import time
from threading import Thread

_IS_WINDOWS = sys.platform == "win32"
_IS_MACOS = sys.platform == "darwin"

# 千帆客户端进程名（MainWindowTitle 是 'eva'）
_QIANFAN_TITLE_PREFIX = "eva"

# Win32 常量
FLASHW_ALL = 0x00000003       # 闪烁任务栏 + 窗口标题
FLASHW_TIMERNOFG = 0x0000000C  # 持续闪烁直到获得焦点
SW_RESTORE = 9
SW_SHOW = 5


class FLASHWINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.UINT),
        ("hwnd", wt.HWND),
        ("dwFlags", wt.DWORD),
        ("uCount", wt.UINT),
        ("dwTimeout", wt.DWORD),
    ]


def _find_qianfan_hwnd() -> int | None:
    """找到千帆主窗口句柄（按标题 'eva' 匹配）。"""
    if not _IS_WINDOWS:
        return None
    user32 = ctypes.windll.user32

    def enum_callback(hwnd, lParam):
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value
        if title and title.startswith(_QIANFAN_TITLE_PREFIX):
            # 用可变容器保存句柄
            lParam.append(hwnd)
            return False  # 停止枚举
        return True

    found: list[int] = []
    EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, ctypes.POINTER(wt.INT))
    # 简化：用 list 作为 lParam 传递
    # ctypes 回调不能直接修改 python list，这里用一个简单遍历方式
    # 直接枚举所有顶级窗口
    result: list[int] = []

    @EnumWindowsProc
    def cb(hwnd, lparam):
        length = user32.GetWindowTextLengthW(hwnd)
        if length > 0:
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            if buf.value.startswith(_QIANFAN_TITLE_PREFIX):
                result.append(hwnd)
                return False
        return True

    user32.EnumWindows(cb, 0)
    return result[0] if result else None


def bring_to_front() -> bool:
    """把千帆窗口置顶并还原（若最小化则还原）。"""
    if _IS_MACOS:
        result = subprocess.run(
            ["open", "-a", "千帆客服工作台"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return result.returncode == 0
    if not _IS_WINDOWS:
        return False
    hwnd = _find_qianfan_hwnd()
    if not hwnd:
        return False
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    # 若窗口最小化，先还原
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    # 置顶
    user32.ShowWindow(hwnd, SW_SHOW)
    user32.SetForegroundWindow(hwnd)
    return True


def flash_taskbar(continuous: bool = True) -> None:
    """让千帆任务栏图标闪烁（continuous=True 时持续闪烁直到获得焦点）。"""
    if not _IS_WINDOWS:
        return
    hwnd = _find_qianfan_hwnd()
    if not hwnd:
        return

    info = FLASHWINFO()
    info.cbSize = ctypes.sizeof(FLASHWINFO)
    info.hwnd = hwnd
    info.dwFlags = FLASHW_TIMERNOFG if continuous else FLASHW_ALL
    info.uCount = 0 if continuous else 5
    info.dwTimeout = 0
    ctypes.windll.user32.FlashWindowEx(ctypes.byref(info))


def play_sound() -> None:
    """播放系统提示音（后台线程，不阻塞）。"""
    def _play():
        try:
            if _IS_MACOS:
                subprocess.run(
                    ["osascript", "-e", "beep 1"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            elif _IS_WINDOWS:
                import winsound
                winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        except Exception:  # noqa: BLE001
            pass
    Thread(target=_play, daemon=True).start()


def notify(level: str = "warning") -> dict:
    """综合提醒：置顶 + 闪烁 + 声音。

    level: "warning"（待审，温和）| "critical"（转人工，强烈）。

    返回执行结果字典。
    """
    brought = bring_to_front()
    if _IS_MACOS:
        message = "有顾客会话需要人工接管" if level == "critical" else "有客服回复需要人工审批"
        subprocess.run(
            [
                "osascript", "-e",
                f'display notification "{message}" with title "栀夏客服 Agent" sound name "Glass"',
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return {"brought_to_front": brought, "level": level, "platform": "macos"}
    if level == "critical":
        flash_taskbar(continuous=True)
        play_sound()
    else:
        flash_taskbar(continuous=False)
        play_sound()
    return {"brought_to_front": brought, "level": level, "platform": sys.platform}


if __name__ == "__main__":
    # 测试提醒
    print("测试提醒（3 秒后触发）...")
    time.sleep(3)
    result = notify("critical")
    print("提醒结果:", result)
