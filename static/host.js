"use strict";

const token = location.pathname.split("/").filter(Boolean)[1];
const errorEl = document.getElementById("error");
const publishBtn = document.getElementById("publish");

async function refresh() {
  let s;
  try {
    const r = await fetch("/api/h/" + token);
    const data = await r.json();
    if (!r.ok) {
      document.getElementById("content").hidden = true;
      const fatal = document.getElementById("fatal");
      fatal.hidden = false;
      fatal.textContent = data.error || "主持链接无效";
      return;
    }
    s = data;
  } catch {
    return; // 网络抖动，下次轮询再试
  }
  render(s);
}

function render(s) {
  document.getElementById("published-banner").hidden = !s.published;

  const pending = s.reviewers.filter((r) => !r.submitted).map((r) => r.name);
  document.getElementById("pending-summary").textContent = s.published
    ? "互评已结束。"
    : pending.length
      ? "待交人员：" + pending.join("、")
      : "全部已交，可以公布。";

  const tbody = document.getElementById("reviewers");
  tbody.innerHTML = "";
  for (const r of s.reviewers) {
    const tr = document.createElement("tr");
    const tdName = document.createElement("td");
    tdName.textContent = r.name;
    const tdStatus = document.createElement("td");
    tdStatus.textContent = r.submitted ? "已交" : "待交";
    tdStatus.className = r.submitted ? "ok" : "pending";
    tr.append(tdName, tdStatus);
    tbody.appendChild(tr);
  }

  publishBtn.disabled = !s.can_publish;

  const linksBody = document.getElementById("links");
  linksBody.innerHTML = "";
  for (const l of s.links) {
    const tr = document.createElement("tr");
    const tdName = document.createElement("td");
    tdName.textContent = l.name;
    const tdLink = document.createElement("td");
    const a = document.createElement("a");
    a.href = location.origin + l.url;
    a.textContent = location.origin + l.url;
    tdLink.appendChild(a);
    tr.append(tdName, tdLink);
    linksBody.appendChild(tr);
  }
}

publishBtn.addEventListener("click", async () => {
  if (!confirm("确定公布？公布后稿件与意见均不可再修改。")) return;
  errorEl.textContent = "";
  try {
    const r = await fetch("/api/h/" + token + "/publish", { method: "POST" });
    const data = await r.json();
    if (!r.ok) {
      errorEl.textContent =
        (data.error || "公布失败") +
        (data.missing ? "（缺：" + data.missing.join("、") + "）" : "");
    }
  } catch {
    errorEl.textContent = "网络错误，请重试";
  }
  refresh();
});

refresh();
setInterval(refresh, 3000);
