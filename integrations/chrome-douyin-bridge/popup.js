const enabled = document.getElementById("enabled");
const health = document.getElementById("health");

chrome.storage.local.get({ enabled: true }, (settings) => {
  enabled.checked = settings.enabled;
});

enabled.addEventListener("change", () => {
  chrome.storage.local.set({ enabled: enabled.checked });
});

chrome.runtime.sendMessage({ type: "health" }, (response) => {
  if (chrome.runtime.lastError || !response?.ok || response.data?.status !== "ok") {
    health.textContent = "本地 Agent 未连接，请先启动栀夏工作台";
    health.className = "error";
    return;
  }
  health.textContent = `已连接 · ${response.data.llm_model || "规则模式"}`;
  health.className = "ok";
});
