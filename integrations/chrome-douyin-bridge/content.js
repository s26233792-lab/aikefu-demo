(() => {
  "use strict";

  const CHANNEL = "douyin_feige";
  const POLL_MS = 1600;
  const OUTBOX_MS = 9000;
  const MAX_SEEN = 800;
  const SYSTEM_TOKENS = [
    "系统消息",
    "客服接入",
    "接入会话",
    "会话已结束",
    "抖音电商智能客服发送",
    "商家配置发送",
  ];

  let enabled = true;
  let processing = false;
  let currentCustomer = "";
  let nextOutboxAt = 0;
  const baselineCustomers = new Set();
  const seen = new Set();
  const sentTexts = new Set();

  function visible(element) {
    if (!element) return false;
    const style = getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
  }

  function trimSeen() {
    while (seen.size > MAX_SEEN) {
      seen.delete(seen.values().next().value);
    }
  }

  function hash(value) {
    let h = 2166136261;
    for (const char of value) {
      h ^= char.charCodeAt(0);
      h = Math.imul(h, 16777619);
    }
    return `dyx${(h >>> 0).toString(16)}`;
  }

  function normalizeButtonText(value) {
    return String(value || "").replace(/\s+/g, "").trim();
  }

  function getCustomerId() {
    const conversationItems = Array.from(
      document.querySelectorAll('[data-qa-id="qa-conversation-chat-item"]')
    ).filter(visible);
    const selected = conversationItems.find((item) =>
      item.getAttribute("aria-selected") === "true"
      || /active|selected|current/i.test(String(item.className || ""))
    ) || (conversationItems.length === 1 ? conversationItems[0] : null);

    const scopes = selected ? [selected, document] : [document];
    for (const scope of scopes) {
      const titled = Array.from(scope.querySelectorAll("[title]"))
        .filter(visible)
        .map((element) => String(element.getAttribute("title") || "").trim())
        .find((title) => title && title.length <= 160 && !/飞鸽|客服系统|商家后台/.test(title));
      if (titled) return titled;
    }
    return "";
  }

  function getMessageRows() {
    const rows = [];
    const unique = new Set();
    for (const selector of ['[data-qa-id="qa-message-warpper"]', ".msgItemWrap"]) {
      for (const row of document.querySelectorAll(selector)) {
        if (!unique.has(row) && visible(row)) {
          unique.add(row);
          rows.push(row);
        }
      }
    }
    return rows;
  }

  function getLastCustomerMessage(customerId) {
    const rows = getMessageRows();
    for (let index = rows.length - 1; index >= 0; index -= 1) {
      const row = rows[index];
      const inbound = row.querySelector(".messageNotMe");
      if (!inbound || row.querySelector(".messageIsMe")) continue;
      const text = String(inbound.querySelector("pre")?.innerText || "").trim();
      if (!text || SYSTEM_TOKENS.some((token) => text.includes(token))) continue;
      const raw = String(row.innerText || "").replace(/\s+/g, " ").trim();
      const domId = row.getAttribute("data-qa-message-id") || "";
      const fingerprint = hash(`${customerId}|${domId}|${raw}`);
      return { text, fingerprint, messageId: domId || fingerprint };
    }
    return null;
  }

  function getComposer() {
    return Array.from(
      document.querySelectorAll('textarea[data-qa-id="qa-send-message-textarea"], textarea')
    ).find(visible) || null;
  }

  function findSendControl(input) {
    let scope = input;
    for (let depth = 0; scope && depth < 7; depth += 1, scope = scope.parentElement) {
      const candidate = Array.from(scope.querySelectorAll('button,[role="button"]')).find((element) =>
        visible(element)
        && normalizeButtonText(element.innerText || element.textContent) === "发送"
        && !element.disabled
        && element.getAttribute("aria-disabled") !== "true"
      );
      if (candidate) return candidate;
    }
    return null;
  }

  async function bridgeRequest(message) {
    const response = await chrome.runtime.sendMessage(message);
    if (!response?.ok) throw new Error(response?.error || "Agent 暂不可用");
    return response.data;
  }

  function showStatus(message, kind = "ok", timeout = 7000) {
    let element = document.getElementById("__zhixia_douyin_bridge_status__");
    if (!element) {
      element = document.createElement("div");
      element.id = "__zhixia_douyin_bridge_status__";
      document.body.appendChild(element);
    }
    const colors = {
      ok: ["#166534", "#dcfce7"],
      warning: ["#92400e", "#fef3c7"],
      error: ["#991b1b", "#fee2e2"],
      paused: ["#334155", "#e2e8f0"],
    };
    const [color, background] = colors[kind] || colors.ok;
    element.style.cssText = [
      "position:fixed",
      "right:18px",
      "top:14px",
      "z-index:2147483647",
      `color:${color}`,
      `background:${background}`,
      "padding:9px 13px",
      "border-radius:9px",
      "font:600 13px/1.4 -apple-system,BlinkMacSystemFont,'PingFang SC',sans-serif",
      "box-shadow:0 6px 22px rgba(15,23,42,.18)",
      "max-width:360px",
    ].join(";");
    element.textContent = message;
    clearTimeout(element.__hideTimer);
    if (timeout > 0) {
      element.__hideTimer = setTimeout(() => element.remove(), timeout);
    }
  }

  async function sendReply(reply, expectedCustomer, expectedFingerprint) {
    const cleanReply = String(reply || "").trim();
    if (!cleanReply) return false;
    if (getCustomerId() !== expectedCustomer) {
      showStatus("会话已切换，已停止发送", "warning");
      return false;
    }
    const latest = getLastCustomerMessage(expectedCustomer);
    if (!latest || latest.fingerprint !== expectedFingerprint) {
      showStatus("顾客又发了新消息，已重新生成回复", "warning");
      return false;
    }
    const input = getComposer();
    const sendControl = input && findSendControl(input);
    if (!input || !sendControl) {
      showStatus("未识别到飞鸽发送框，已停止发送", "error", 0);
      return false;
    }

    input.focus();
    const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set;
    if (!setter) return false;
    setter.call(input, cleanReply);
    input.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: cleanReply }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
    await new Promise((resolve) => setTimeout(resolve, 120));

    if (getCustomerId() !== expectedCustomer) return false;
    sendControl.click();
    sentTexts.add(cleanReply);
    showStatus("栀夏 Agent 已回复", "ok");
    return true;
  }

  async function handleDecision(customerId, message) {
    const decision = await bridgeRequest({
      type: "decide",
      payload: {
        text: message.text,
        customer_id: customerId,
        message_id: message.messageId,
      },
    });
    const reply = String(decision?.reply || "").trim();
    if (decision?.status === "taken_over") {
      if (decision?.send_before_handoff && reply) {
        await sendReply(reply, customerId, message.fingerprint);
      }
      showStatus("已安抚顾客并转人工，自动回复已停止", "warning", 0);
      return;
    }
    if (decision?.needs_approval || decision?.status === "pending_approval") {
      if (decision?.send_before_handoff && reply) {
        await sendReply(reply, customerId, message.fingerprint);
      }
      showStatus("已进入人工审批，请在栀夏工作台处理", "warning", 0);
      return;
    }
    if (reply) await sendReply(reply, customerId, message.fingerprint);
  }

  async function pollOutbox(customerId) {
    const result = await bridgeRequest({ type: "pullOutbox", customer_id: customerId });
    for (const item of result?.outbox || []) {
      if (item.customer_id !== customerId || !item.content) continue;
      const latest = getLastCustomerMessage(customerId);
      if (!latest) return;
      if (await sendReply(item.content, customerId, latest.fingerprint)) {
        await bridgeRequest({ type: "ackOutbox", id: item.id });
      }
    }
  }

  async function tick() {
    if (!enabled || processing) return;
    const customerId = getCustomerId();
    if (!customerId) return;
    const message = getLastCustomerMessage(customerId);
    if (!message) return;

    if (!baselineCustomers.has(customerId)) {
      baselineCustomers.add(customerId);
      seen.add(message.fingerprint);
      trimSeen();
      currentCustomer = customerId;
      showStatus("栀夏 Agent 已连接，仅响应后续新消息", "ok");
      return;
    }
    if (customerId !== currentCustomer) currentCustomer = customerId;

    processing = true;
    try {
      if (Date.now() >= nextOutboxAt) {
        nextOutboxAt = Date.now() + OUTBOX_MS;
        await pollOutbox(customerId);
      }
      if (seen.has(message.fingerprint) || sentTexts.has(message.text)) return;
      seen.add(message.fingerprint);
      trimSeen();
      await handleDecision(customerId, message);
    } catch (error) {
      console.warn("[zhixia-douyin]", error);
      showStatus("本地 Agent 暂不可用，未发送回复", "error", 0);
      await chrome.runtime.sendMessage({ type: "setBadge", status: "error" }).catch(() => {});
    } finally {
      processing = false;
    }
  }

  function applyEnabled(nextEnabled) {
    enabled = Boolean(nextEnabled);
    chrome.runtime.sendMessage({ type: "setBadge", status: enabled ? "connected" : "paused" }).catch(() => {});
    showStatus(enabled ? "栀夏 Agent 已启用" : "栀夏 Agent 已暂停", enabled ? "ok" : "paused");
  }

  chrome.storage.local.get({ enabled: true }, (settings) => applyEnabled(settings.enabled));
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area === "local" && changes.enabled) applyEnabled(changes.enabled.newValue);
  });
  setInterval(tick, POLL_MS);
  tick();
})();
