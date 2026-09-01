const API_BASE = "http://127.0.0.1:18081";
const FEIGE_ORIGIN = "https://im.jinritemai.com/";

chrome.runtime.onInstalled.addListener(() => {
  chrome.storage.local.get(["enabled"], (current) => {
    if (typeof current.enabled !== "boolean") {
      chrome.storage.local.set({ enabled: true });
    }
  });
});

function isFeigeSender(sender) {
  return Boolean(sender.tab?.url?.startsWith(FEIGE_ORIGIN));
}

async function fetchJson(path, options = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 70000);
  try {
    const response = await fetch(`${API_BASE}${path}`, {
      ...options,
      signal: controller.signal,
      headers: {
        "Content-Type": "application/json",
        ...(options.headers || {}),
      },
    });
    if (!response.ok) {
      throw new Error(`Agent HTTP ${response.status}`);
    }
    return await response.json();
  } finally {
    clearTimeout(timer);
  }
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  const allowedWithoutPage = message?.type === "health";
  if (!allowedWithoutPage && !isFeigeSender(sender)) {
    sendResponse({ ok: false, error: "invalid_sender" });
    return false;
  }

  (async () => {
    if (message?.type === "health") {
      return { ok: true, data: await fetchJson("/health") };
    }
    if (message?.type === "setBadge") {
      const text = message.status === "connected" ? "ON" : message.status === "paused" ? "Ⅱ" : "!";
      const color = message.status === "connected" ? "#16a34a" : message.status === "paused" ? "#64748b" : "#dc2626";
      await chrome.action.setBadgeBackgroundColor({ tabId: sender.tab.id, color });
      await chrome.action.setBadgeText({ tabId: sender.tab.id, text });
      return { ok: true };
    }
    if (message?.type === "decide") {
      const payload = message.payload || {};
      const text = String(payload.text || "").trim().slice(0, 2000);
      const customerId = String(payload.customer_id || "").trim().slice(0, 160);
      const messageId = String(payload.message_id || "").trim().slice(0, 200);
      if (!text || !customerId || !messageId) {
        throw new Error("invalid_message");
      }
      return {
        ok: true,
        data: await fetchJson("/platforms/douyin/decide", {
          method: "POST",
          body: JSON.stringify({
            text,
            customer_id: customerId,
            message_id: messageId,
            tenant_id: "demo",
            store_id: "STORE-001",
            suppress_intro: true,
          }),
        }),
      };
    }
    if (message?.type === "pullOutbox") {
      const customerId = String(message.customer_id || "").trim().slice(0, 160);
      if (!customerId) throw new Error("invalid_customer");
      const query = new URLSearchParams({ channel: "douyin_feige", customer_id: customerId });
      return { ok: true, data: await fetchJson(`/v1/outbox/pull?${query}`) };
    }
    if (message?.type === "ackOutbox") {
      const id = String(message.id || "").trim();
      if (!/^[A-Za-z0-9_-]{1,160}$/.test(id)) throw new Error("invalid_outbox_id");
      return {
        ok: true,
        data: await fetchJson(`/v1/outbox/${encodeURIComponent(id)}/ack`, {
          method: "POST",
          body: JSON.stringify({ status: "sent" }),
        }),
      };
    }
    throw new Error("unsupported_message");
  })()
    .then(sendResponse)
    .catch((error) => sendResponse({ ok: false, error: error?.message || "request_failed" }));

  return true;
});
