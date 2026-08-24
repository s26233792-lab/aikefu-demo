"""Windows/macOS 千帆接入的跨平台回归测试。"""
from __future__ import annotations

import os
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath
from unittest.mock import patch


ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(ROOT_DIR / "src"))

from workers import notifier, qianfan_launcher  # noqa: E402
from workers.cdp_client import cdp_http  # noqa: E402
from workers.qianfan_launcher import (  # noqa: E402
    app_candidates,
    build_launch_command,
    chromium_args,
    default_profile_dir,
)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    fake_home = Path("/Users/demo")
    mac_candidates = app_candidates(system="Darwin", home=fake_home, env={})
    assert mac_candidates[0] == Path("/Applications/千帆客服工作台.app")
    assert mac_candidates[1] == fake_home / "Applications" / "千帆客服工作台.app"

    configured = app_candidates(
        system="Darwin",
        home=fake_home,
        env={"XHS_QIANFAN_APP_PATH": "~/Apps/Qianfan.app"},
    )
    assert configured == [Path("~/Apps/Qianfan.app").expanduser()]

    command = build_launch_command(
        PurePosixPath("/Applications/千帆客服工作台.app"), system="Darwin", port=9333
    )
    assert command == [
        "open", "-na", "/Applications/千帆客服工作台.app", "--args",
        "--remote-debugging-port=9333", "--remote-allow-origins=http://127.0.0.1:9333",
    ]

    windows_command = build_launch_command(
        PureWindowsPath("C:/Apps/千帆客服工作台.exe"), system="Windows", port=9222
    )
    assert windows_command[0] == "C:\\Apps\\千帆客服工作台.exe"
    assert "--remote-debugging-port=9222" in windows_command

    mac_profile = default_profile_dir(system="Darwin", home=fake_home, env={})
    assert mac_profile == fake_home / "Library" / "Application Support" / "xhs-kefu-demo" / "qianfan-profile"
    assert "--no-sandbox" not in chromium_args(system="Darwin")
    assert "--no-sandbox" in chromium_args(system="Linux")

    with patch.dict(os.environ, {"XHS_QIANFAN_CDP_PORT": "9333"}, clear=False):
        assert cdp_http() == "http://127.0.0.1:9333"
    with patch.dict(
        os.environ,
        {"XHS_CDP_BASE": "http://localhost:9444"},
        clear=True,
    ):
        assert cdp_http() == "http://127.0.0.1:9444"

    with patch.object(sys, "platform", "darwin"), patch.object(
        notifier, "_run_osascript", return_value=True
    ) as run_script:
        result = notifier.notify("critical")
        assert result["platform"] == "darwin"
        assert result["brought_to_front"] is True
        assert run_script.call_count >= 2

    with patch.object(qianfan_launcher, "cdp_ready", side_effect=[False, True]), patch.object(
        qianfan_launcher,
        "find_app_path",
        return_value=PurePosixPath("/Applications/千帆客服工作台.app"),
    ), patch.object(qianfan_launcher, "build_launch_command", return_value=["open", "-na", "qianfan"]), patch.object(
        qianfan_launcher.subprocess, "Popen"
    ) as popen:
        launched = qianfan_launcher.launch_qianfan(wait_seconds=0.2)
        assert launched["ok"] is True and launched["already_running"] is False
        popen.assert_called_once()

    for script_name in ("install-macos.command", "start-macos.command"):
        script_bytes = (ROOT_DIR / script_name).read_bytes()
        assert script_bytes.startswith(b"#!/bin/bash\n")
        assert b"\r\n" not in script_bytes

    print("✅ Windows/macOS 千帆跨平台测试通过")


if __name__ == "__main__":
    main()
