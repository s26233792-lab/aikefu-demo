"""一键启动小红书千帆客服 Agent。

用法：
    python run.py web       # 启动决策 API + Web 演示界面 (默认，端口 18081)
    python run.py qianfan   # 自动启动千帆桌面客户端并连接 Worker（Windows/macOS）
    python run.py doctor    # 检查 Python、依赖、千帆客户端、CDP 与 API
    python run.py desktop   # 千帆桌面端(Electron) CDP 真实 Worker（需客户端带调试端口运行）
    python run.py login     # 千帆网页版扫码登录（旧方案，网页版用）
    python run.py worker    # 千帆网页版浏览器 Worker（旧方案，网页版用）
    python run.py smoke     # 离线冒烟测试（rules 模式，无需 LLM Key）

千帆桌面端真实接入（推荐）：
1. 另开终端先启动决策 API：python run.py web
2. 自动启动并连接官方客户端：python run.py qianfan

macOS 可直接双击 install-macos.command 完成首次安装，再双击 start-macos.command。
"""
from __future__ import annotations

import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


def run_web() -> None:
    import uvicorn
    uvicorn.run("xhs_kefu.api:app", host="127.0.0.1", port=18081, reload=False)


def run_login() -> None:
    from xhs_kefu.config import load_dotenv
    load_dotenv()
    from workers.qianfan_login import main as login_main
    login_main()


def run_worker() -> None:
    import asyncio
    from xhs_kefu.config import load_dotenv
    load_dotenv()
    from workers.qianfan_browser import main as worker_main
    asyncio.run(worker_main())


def run_desktop() -> None:
    """启动千帆桌面端（Electron）CDP Worker。前提：千帆客户端已带调试端口运行。"""
    import asyncio
    from xhs_kefu.config import load_dotenv
    load_dotenv()
    from workers.qianfan_cdp_worker import main as desktop_main
    asyncio.run(desktop_main())


def run_qianfan() -> None:
    """跨平台启动千帆官方客户端，等待 CDP 就绪后运行 Worker。"""
    from xhs_kefu.config import load_dotenv
    load_dotenv()
    from workers.qianfan_launcher import launch_qianfan

    result = launch_qianfan()
    if not result.get("ok"):
        print(f"启动失败：{result.get('error', '未知错误')}")
        if result.get("hint"):
            print(f"处理建议：{result['hint']}")
        if result.get("download_url"):
            print(f"官方下载：{result['download_url']}")
        raise SystemExit(1)
    print(f"千帆 CDP 已就绪：{result.get('cdp_url')}")
    run_desktop()


def run_doctor() -> None:
    from xhs_kefu.config import load_dotenv
    load_dotenv()
    from workers.qianfan_launcher import doctor, print_doctor

    report = doctor()
    print_doctor(report)
    raise SystemExit(0 if report.get("ready") else 1)


def run_smoke() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    import asyncio
    from xhs_kefu.config import Settings
    from xhs_kefu.domain import IncomingMessage
    from xhs_kefu.fixtures import Fixtures
    from xhs_kefu.planner import build_planner
    from xhs_kefu.policy import CompensationRule, PolicyEngine
    from xhs_kefu.runtime import AgentRuntime
    from xhs_kefu.storage import SQLiteStore
    import tempfile
    import os
    import time

    settings = Settings.from_env()
    tmp = tempfile.mkdtemp(prefix="xhs-smoke-")
    db = os.path.join(tmp, "smoke.db")
    store = SQLiteStore(db)
    fixtures = Fixtures(settings.data_dir)
    rule = CompensationRule.from_file(settings.policy_path) if settings.policy_path.exists() else CompensationRule.defaults()
    policy = PolicyEngine(rule)
    planner = build_planner("rules", base_url="", model="", api_key=None)
    runtime = AgentRuntime(store=store, fixtures=fixtures, policy=policy, planner=planner)

    cases = [
        ("售前-推荐", "CUST-9001", "你们店有什么外套推荐"),
        ("售前-参数", "CUST-9001", "SKU-JACKET-RED-M 是什么材质能机洗吗"),
        ("售前-下单", "CUST-9001", "怎么下单"),
        ("售中-物流", "CUST-9002", "XHS-20260101-001 物流到哪了"),
        ("售中-改址", "CUST-9002", "XHS-20260101-001 改成上海市浦东新区张江镇祖冲之路100号"),
        ("售中-拦截", "CUST-9002", "XHS-20260101-001 帮我拦截退回"),
        ("售后-异常", "CUST-9003", "XHS-20260102-002 快递怎么一直不动"),
        ("售后-补偿", "CUST-9003", "XHS-20260102-002 赔3元"),
    ]

    async def run():
        for i, (label, cid, text) in enumerate(cases):
            msg = IncomingMessage(
                tenant_id="demo", customer_id=cid, message_id=f"smoke-{i}", text=text
            )
            result = await runtime.handle_message(msg)
            print(f"[{label}] intent={result['intent']} status={result['status']}")
            print(f"  回复: {result['reply'][:60]}")
            if result.get("policy"):
                print(f"  风控: {result['policy']['outcome']} ({result['policy']['reason_code']})")
    asyncio.run(run())
    print("\n冒烟测试完成（rules 模式，无 LLM）。")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    mode = sys.argv[1] if len(sys.argv) > 1 else "web"
    if mode == "web":
        run_web()
    elif mode == "login":
        run_login()
    elif mode == "worker":
        run_worker()
    elif mode == "desktop":
        run_desktop()
    elif mode in {"qianfan", "mac"}:
        run_qianfan()
    elif mode == "doctor":
        run_doctor()
    elif mode == "smoke":
        run_smoke()
    else:
        print("未知模式，可选: web / qianfan / doctor / login / worker / desktop / smoke")
