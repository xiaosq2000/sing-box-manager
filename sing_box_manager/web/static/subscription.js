"use strict";

for (const button of document.querySelectorAll("[data-copy-target]")) {
  button.hidden = false;
  button.addEventListener("click", async () => {
    const input = document.getElementById(button.dataset.copyTarget);
    const status = document.getElementById(button.dataset.copyStatus);
    if (!input || !status) return;
    try {
      await navigator.clipboard.writeText(input.value);
      status.textContent = "已复制订阅链接。链接是凭据，请勿分享。";
    } catch {
      input.focus();
      input.select();
      status.textContent = "无法自动复制。链接已选中，请手动复制。";
    }
  });
}
