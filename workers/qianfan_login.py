"""千帆登录 + 页面结构抓取辅助脚本。

用法：
    python workers/qianfan_login.py

流程：
1. 打开一个全新的、非 headless 的 Chromium（独立 profile，持久化到 data/qianfan-profile/）。
2. 打开千帆工作台 ark.xiaohongshu.com。
3. 你在浏览器窗口里扫码登录（小红书 App 扫码 / 手机号验证码）。
4. 登录成功后脚本自动检测，dump 出客服工作台的 DOM 结构（会话列表/消息区/输入框选择器），
   供 qianfan_browser.py 校准选择器使用。

登录态会保存到 data/qianfan-profile/，之后 qianfan_browser.py 直接复用该 profile，
无需再次登录。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
PROFILE_DIR = BASE_DIR / "data" / "qianfan-profile"
DUMP_DIR = BASE_DIR / "data" / "dump"
QIANFAN_URL = "https://ark.xiaohongshu.com/"


def main() -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("未安装 playwright，请先: pip install playwright && playwright install chromium")
        sys.exit(1)

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    DUMP_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            viewport={"width": 1280, "height": 900},
        )
        page = browser.new_page()
        print(f"[login] 打开千帆 {QIANFAN_URL} ...")
        page.goto(QIANFAN_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(5000)

        print("[login] 请在打开的浏览器窗口中扫码登录小红书千帆。")
        print("[login] 登录成功后，本脚本每 5 秒自动检测一次，检测到客服工作台即开始抓取。")

        # 轮询等待登录完成（URL 或页面元素变化）
        detected = False
        for _ in range(120):  # 最长 10 分钟
            page.wait_for_timeout(5000)
            url = page.url
            title = page.title()
            print(f"[login] 当前 URL: {url} | 标题: {title}")
            # 登录后通常进入客服工作台；检测若干候选信号
            signals = [
                "ark.xiaohongshu.com" in url,
                "客服" in title or "千帆" in title or "商家" in title,
            ]
            if any(signals) and "login" not in url.lower() and "passport" not in url.lower():
                detected = True
                break
        if not detected:
            print("[login] 未在限定时间内检测到登录成功，请确认是否已登录。")
            browser.close()
            sys.exit(1)

        print("[login] 检测到登录成功，开始抓取页面 DOM 结构...")
        page.wait_for_timeout(5000)
        dump = _dump_structure(page)
        out = DUMP_DIR / "qianfan_structure.txt"
        out.write_text(dump, encoding="utf-8")
        print(f"[login] 结构已保存到 {out}")
        print("=" * 60)
        print(dump)
        print("=" * 60)
        print("[login] 已完成。保持浏览器开启 60 秒供观察，随后关闭（登录态已持久化）。")
        page.wait_for_timeout(60000)
        browser.close()


def _dump_structure(page) -> str:
    """抓取关键区域的可选元素与 class，供选择器校准。"""
    js = """
    () => {
      const out = {url: location.href, title: document.title, root: null, iframes: [], candidates: []};
      const root = document.getElementById('ark-app-root') || document.body;
      out.root = root ? (root.tagName + '#' + (root.id||'')) : null;
      // 收集所有 iframe
      document.querySelectorAll('iframe').forEach(f => {
        out.iframes.push({src: f.src, id: f.id, cls: f.className});
      });
      // 候选元素：可能的会话列表 / 消息 / 输入框
      const sel = [
        "[class*='session']","[class*='conversation']","[class*='chat']","[class*='message']",
        "[class*='msg']","[class*='contact']","[class*='user-list']","[class*='im-']",
        "textarea","[contenteditable='true']","[role='textbox']","button[class*='send']",
        "[class*='list']","[class*='panel']","[class*='main']"
      ];
      sel.forEach(s => {
        document.querySelectorAll(s).forEach(el => {
          const t = (el.innerText||'').slice(0,30).replace(/\\n/g,' ');
          if (out.candidates.length < 80) {
            out.candidates.push({sel:s, tag:el.tagName, cls:(el.className||'').toString().slice(0,80), id:el.id||'', text:t});
          }
        });
      });
      return JSON.stringify(out, null, 2);
    }
    """
    try:
        return page.evaluate(js)
    except Exception as e:  # noqa: BLE001
        return f"抓取失败: {e}"


if __name__ == "__main__":
    main()
