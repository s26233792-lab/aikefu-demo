"""千帆桌面端（Electron）—— CDP 连接 + 页面结构抓取（轻量版）。

通过 websocket 直连 CDP 的客服工作台 page target，抓取真实 DOM 结构。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

try:
    from .cdp_client import CdpSession, find_cstools_page
except ImportError:  # 兼容直接执行脚本
    from cdp_client import CdpSession, find_cstools_page

BASE_DIR = Path(__file__).resolve().parent.parent
DUMP_DIR = BASE_DIR / "data" / "dump"

DUMP_JS = r"""
() => {
  const out = {url: location.href, title: document.title, nav: [], candidates: []};
  document.querySelectorAll("a, [role='menuitem'], [class*='menu'], [class*='nav'], [class*='tab'], [class*='item']").forEach(el => {
    const t = (el.innerText || '').trim().slice(0, 20);
    const href = el.getAttribute('href') || '';
    if (out.nav.length < 60 && ((el.tagName === 'A' && t) || t.length <= 12)) {
      out.nav.push({tag: el.tagName, cls: (el.className||'').toString().slice(0,60), text: t, href});
    }
  });
  const sel = ["[class*='session']","[class*='conversation']","[class*='chat']","[class*='message']",
    "[class*='msg']","[class*='contact']","[class*='im-']","[class*='customer']","[class*='buyer']",
    "[class*='list']","[class*='panel']","textarea","[contenteditable]","[role='textbox']",
    "button[class*='send']","[class*='input']","[class*='editor']"];
  const seen = new Set();
  sel.forEach(s => {
    document.querySelectorAll(s).forEach(el => {
      const t = (el.innerText || '').slice(0, 40).replace(/\\n/g, ' ');
      const key = s + '|' + t;
      if (seen.has(key)) return;
      seen.add(key);
      if (out.candidates.length < 120) {
        out.candidates.push({sel: s, tag: el.tagName, cls: (el.className||'').toString().slice(0,90), id: el.id, text: t});
      }
    });
  });
  return JSON.stringify(out, null, 2);
}
"""


def main() -> None:
    target = find_cstools_page()
    if target is None:
        print("[cdp] 未找到已登录的客服工作台 page target。请确认千帆客户端已登录并打开客服工作台。")
        sys.exit(1)

    ws_url = target.get("webSocketDebuggerUrl")
    print(f"[cdp] 目标页: {target.get('title')} | {target.get('url')}")
    session = CdpSession(ws_url)
    try:
        result = session.evaluate(DUMP_JS)
        dump = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, indent=2)
        out = DUMP_DIR / "qianfan_desktop_structure.txt"
        out.write_text(dump, encoding="utf-8")
        print(f"[cdp] 结构已保存到 {out}")
        print("=" * 70)
        print(dump)
        print("=" * 70)
    finally:
        session.close()


if __name__ == "__main__":
    main()
