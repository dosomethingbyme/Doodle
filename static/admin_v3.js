/* V3 trusted-delivery operations layered on the shared admin application. */

const v3BaseShowLogin = showLogin;
showLogin = function (message = "") {
  root.innerHTML = "";
  if (state.user && message.includes("没有执行此操作的权限")) {
    showApp();
    root.innerHTML = `<section class="panel empty"><h2>没有访问权限</h2><p>${esc(message)}</p><button class="btn" data-route="/admin">返回任务列表</button></section>`;
    return;
  }
  state.csrfToken = "";
  state.user = null;
  v3BaseShowLogin(message);
};

const v3BaseRenderRoute = renderRoute;
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  ["cancelModal", "statusModal", "blockModal"].forEach((id) => { const dialog = document.getElementById(id); if (dialog && !dialog.hidden) dialog.hidden = true; });
});
renderRoute = async function () {
  const path = location.pathname;
  try {
    if (path === "/admin/agenda") return renderAgendaV3();
    if (path === "/admin/notifications") return renderNotificationsV3();
    if (path === "/admin/settings") return renderSettingsV3();
    if (path === "/admin/users") return renderUsersV3();
    if (path === "/admin/audit") return renderAuditV3();
    return v3BaseRenderRoute();
  } catch (error) {
    root.innerHTML = `<section class="panel empty"><h2>页面加载失败</h2><p>${esc(error.message)}</p><button class="btn" data-route="/admin">返回任务列表</button></section>`;
  }
};

async function renderAgendaV3() {
  const today = new Date().toLocaleDateString("sv-SE");
  const date = new URLSearchParams(location.search).get("date") || today;
  const rows = await api(`/api/admin/agenda?date=${encodeURIComponent(date)}`);
  root.innerHTML = `<div class="page-head"><div><h1>日程议程</h1><div class="subtle">跨任务查看当天仍有效的预约。</div></div><input id="agendaDate" type="date" value="${date}"></div><section class="panel content"><div class="table-wrap"><table><thead><tr><th>时间</th><th>任务</th><th>预约编号</th><th>预约人</th><th>联系邮箱</th></tr></thead><tbody>${rows.map((row) => `<tr><td><strong>${row.time}–${row.endTime}</strong></td><td>${esc(row.taskName)}</td><td>${esc(row.bookingRef)}</td><td>${esc(row.name)}</td><td>${esc(row.email)}</td></tr>`).join("") || '<tr><td colspan="5">当天没有预约。</td></tr>'}</tbody></table></div></section>`;
  $("#agendaDate").onchange = () => route(`/admin/agenda?date=${$("#agendaDate").value}`, true);
}

async function renderNotificationsV3() {
  const rows = await api("/api/admin/notifications");
  root.innerHTML = `<div class="page-head"><div><h1>通知中心</h1><div class="subtle">邮件先进入可靠队列，失败记录可在这里重试。</div></div><button class="btn primary" id="retryAll">重试未发送</button></div><section class="panel content"><div class="table-wrap"><table><thead><tr><th>状态</th><th>类型</th><th>收件人</th><th>尝试</th><th>最后错误</th><th></th></tr></thead><tbody>${rows.map((row) => `<tr><td><span class="badge ${row.status === "sent" ? "published" : "paused"}">${row.status === "sent" ? "已发送" : "待重试"}</span></td><td>${esc(row.event_type)}</td><td>${esc(row.recipient)}</td><td>${row.attempts}</td><td>${esc(row.last_error || "—")}</td><td>${row.status !== "sent" ? `<button class="btn small" data-retry-notification="${row.id}">重试</button>` : ""}</td></tr>`).join("") || '<tr><td colspan="6">还没有通知记录。</td></tr>'}</tbody></table></div></section>`;
  $("#retryAll").onclick = async () => { const result = await api("/api/admin/notifications/retry", { method: "POST", body: "{}" }); toast(`处理 ${result.processed} 条，发送成功 ${result.sent} 条`); renderNotificationsV3(); };
  document.querySelectorAll("[data-retry-notification]").forEach((button) => button.onclick = async () => { await api(`/api/admin/notifications/${button.dataset.retryNotification}`, { method: "POST", body: "{}" }); renderNotificationsV3(); });
}

async function renderSettingsV3() {
  const settings = await api("/api/admin/settings/email");
  const source = (key) => settings.sources?.[key] === "env" ? "（来自 .env 默认值）" : "（后台覆盖）";
  root.innerHTML = `<div class="page-head"><div><h1>系统设置</h1><div class="subtle">后台值优先；选择恢复默认后继续使用 .env 配置。</div></div><div class="actions"><button class="btn" data-route="/admin/users">管理员</button><button class="btn" data-route="/admin/audit">审计日志</button></div></div><section class="panel content"><h2>邮件服务器</h2><form id="emailSettings"><div class="info-grid"><div class="field"><label for="smtpHost">SMTP 主机 ${source("host")}</label><input id="smtpHost" value="${esc(settings.host)}"></div><div class="field"><label for="smtpPort">端口 ${source("port")}</label><input id="smtpPort" type="number" value="${settings.port || ""}"></div><div class="field"><label for="smtpUser">账号 ${source("user")}</label><input id="smtpUser" autocomplete="username" value="${esc(settings.user)}"></div><div class="field"><label for="smtpFrom">发件人 ${source("from")}</label><input id="smtpFrom" type="email" value="${esc(settings.from)}"></div><div class="field"><label for="smtpPassword">密码（${settings.passwordConfigured ? "已配置" : "未配置"}，留空保持不变）</label><input id="smtpPassword" type="password" autocomplete="new-password"></div><div class="field"><label>传输安全</label><label><input id="smtpSsl" type="checkbox" ${settings.useSsl ? "checked" : ""}> SSL</label><label><input id="smtpStarttls" type="checkbox" ${settings.starttls ? "checked" : ""}> STARTTLS</label></div></div><div class="actions"><button class="btn primary" type="submit">保存后台覆盖</button><button class="btn" id="restoreEnv" type="button">全部恢复为 .env 默认</button></div></form><hr><h3>发送测试</h3><div class="range-row"><input id="testRecipient" type="email" placeholder="测试收件邮箱"><button class="btn" id="testEmail">发送测试邮件</button></div></section>`;
  $("#emailSettings").onsubmit = async (event) => { event.preventDefault(); await api("/api/admin/settings/email", { method: "PUT", body: JSON.stringify({ host: $("#smtpHost").value, port: Number($("#smtpPort").value), user: $("#smtpUser").value, from: $("#smtpFrom").value, password: $("#smtpPassword").value, useSsl: $("#smtpSsl").checked, starttls: $("#smtpStarttls").checked }) }); toast("邮件设置已保存"); renderSettingsV3(); };
  $("#restoreEnv").onclick = async () => { await api("/api/admin/settings/email", { method: "PUT", body: JSON.stringify({ inherit: ["host", "port", "user", "password", "from", "useSsl", "starttls"] }) }); toast("已恢复 .env 默认值"); renderSettingsV3(); };
  $("#testEmail").onclick = async () => { await api("/api/admin/settings/email/test", { method: "POST", body: JSON.stringify({ recipient: $("#testRecipient").value }) }); toast("测试邮件已发送"); };
}

async function renderUsersV3() {
  const users = await api("/api/admin/users");
  root.innerHTML = `<div class="page-head"><div><h1>管理员</h1><div class="subtle">Owner 可管理系统设置；Operator 可管理任务和预约。</div></div><button class="btn" data-route="/admin/settings">返回设置</button></div><section class="panel content"><div class="table-wrap"><table><thead><tr><th>账号</th><th>角色</th><th>状态</th><th>最近登录</th></tr></thead><tbody>${users.map((user) => `<tr><td>${esc(user.username)}</td><td>${user.role}</td><td>${user.is_active ? "启用" : "停用"}</td><td>${esc(user.last_login_at || "从未")}</td></tr>`).join("")}</tbody></table></div><h3>添加管理员</h3><form id="newUser" class="range-row"><input id="newUsername" placeholder="账号" required><input id="newPassword" type="password" placeholder="至少 10 位密码" required><select id="newRole"><option value="operator">Operator</option><option value="owner">Owner</option></select><button class="btn primary">添加</button></form></section>`;
  $("#newUser").onsubmit = async (event) => { event.preventDefault(); await api("/api/admin/users", { method: "POST", body: JSON.stringify({ username: $("#newUsername").value, password: $("#newPassword").value, role: $("#newRole").value }) }); toast("管理员已添加"); renderUsersV3(); };
}

async function renderAuditV3() {
  const rows = await api("/api/admin/audit");
  root.innerHTML = `<div class="page-head"><div><h1>审计日志</h1><div class="subtle">记录关键后台安全和配置操作。</div></div><button class="btn" data-route="/admin/settings">返回设置</button></div><section class="panel content"><div class="table-wrap"><table><thead><tr><th>时间</th><th>操作者</th><th>动作</th><th>对象</th></tr></thead><tbody>${rows.map((row) => `<tr><td>${esc(row.created_at)}</td><td>${esc(row.actor_label)}</td><td>${esc(row.action)}</td><td>${esc(row.entity_type)} #${esc(row.entity_id)}</td></tr>`).join("") || '<tr><td colspan="4">还没有审计事件。</td></tr>'}</tbody></table></div></section>`;
}

const v3BaseOpenEditor = openEditor;
openEditor = function (task = null) {
  v3BaseOpenEditor(task);
  Object.assign(state.editor, {
    cancelCutoffMinutes: state.editor.cancelCutoffMinutes ?? 0,
    rescheduleCutoffMinutes: state.editor.rescheduleCutoffMinutes ?? 0,
    maxReschedules: state.editor.maxReschedules ?? 10,
    policyText: state.editor.policyText ?? "",
    privacyText: state.editor.privacyText ?? "",
    retentionDays: state.editor.retentionDays ?? 365,
  });
  renderWizard();
};

const v3BaseRenderWizardStep = renderWizardStep;
renderWizardStep = function () {
  v3BaseRenderWizardStep();
  if (state.step !== 4) return;
  $("#wizardMain").insertAdjacentHTML("beforeend", `<section class="editor-card" style="margin-top:12px"><h3>取消、改期与隐私政策</h3><div class="info-grid"><div class="field"><label for="cancelCutoff">取消截止（分钟前）</label><input id="cancelCutoff" type="number" min="0" max="10080" value="${state.editor.cancelCutoffMinutes}"></div><div class="field"><label for="rescheduleCutoff">改期截止（分钟前）</label><input id="rescheduleCutoff" type="number" min="0" max="10080" value="${state.editor.rescheduleCutoffMinutes}"></div><div class="field"><label for="maxReschedules">最多改期次数</label><input id="maxReschedules" type="number" min="0" max="100" value="${state.editor.maxReschedules}"></div><div class="field"><label for="retentionDays">数据保留天数</label><input id="retentionDays" type="number" min="30" max="3650" value="${state.editor.retentionDays}"></div></div><div class="field"><label for="policyText">预约政策</label><textarea id="policyText">${esc(state.editor.policyText)}</textarea></div><div class="field"><label for="privacyText">隐私说明</label><textarea id="privacyText">${esc(state.editor.privacyText)}</textarea></div></section>`);
};

const v3BaseSyncWizard = syncWizard;
syncWizard = function () {
  v3BaseSyncWizard();
  if ($("#cancelCutoff")) {
    state.editor.cancelCutoffMinutes = Number($("#cancelCutoff").value || 0);
    state.editor.rescheduleCutoffMinutes = Number($("#rescheduleCutoff").value || 0);
    state.editor.maxReschedules = Number($("#maxReschedules").value || 0);
    state.editor.retentionDays = Number($("#retentionDays").value || 365);
    state.editor.policyText = $("#policyText").value.trim();
    state.editor.privacyText = $("#privacyText").value.trim();
  }
};

const v3BaseRenderBookings = renderBookings;
renderBookings = async function () {
  await v3BaseRenderBookings();
  const exportButton = $("#bookingExport");
  exportButton?.insertAdjacentHTML("beforebegin", '<button class="btn primary" id="adminCreateBooking">代客预约</button>');
  root.querySelector(".content")?.insertAdjacentHTML("beforeend", `<div class="modal-backdrop" id="bookingEditorModal" hidden><section class="modal" role="dialog" aria-modal="true" aria-labelledby="bookingEditorTitle"><h2 id="bookingEditorTitle">代客预约</h2><form id="bookingEditorForm"><input id="bookingEditorId" type="hidden"><div class="field"><label for="bookingName">姓名</label><input id="bookingName" required></div><div class="field"><label for="bookingEmail">邮箱</label><input id="bookingEmail" type="email" required></div><div class="field"><label for="bookingSlot">预约时间</label><select id="bookingSlot" required></select></div><div class="field"><label for="bookingNotes">内部备注</label><textarea id="bookingNotes"></textarea></div><div class="error" id="bookingEditorError" role="alert"></div><div class="actions"><button class="btn" id="closeBookingEditor" type="button">取消</button><button class="btn primary" type="submit">保存并发送通知</button></div></form></section></div>`);
  $("#adminCreateBooking").onclick = () => openBookingEditorV3();
  $("#closeBookingEditor").onclick = () => $("#bookingEditorModal").hidden = true;
  $("#bookingEditorForm").onsubmit = saveBookingEditorV3;
};

const v3BaseRenderBookingTable = renderBookingTable;
renderBookingTable = function () {
  v3BaseRenderBookingTable();
  document.querySelectorAll("#bookingTable tbody tr").forEach((row, index) => {
    const booking = state.bookings[index];
    const cell = row.lastElementChild;
    if (booking?.status === "confirmed" && cell) {
      cell.insertAdjacentHTML("afterbegin", `<button class="btn small" data-edit-booking="${booking.id}">编辑/改期</button> `);
    }
  });
  document.querySelectorAll("[data-edit-booking]").forEach((button) => button.onclick = () => openBookingEditorV3(state.bookings.find((item) => item.id === Number(button.dataset.editBooking))));
};

function openBookingEditorV3(booking = null) {
  const modal = $("#bookingEditorModal");
  if (!modal) return;
  $("#bookingEditorTitle").textContent = booking ? `编辑预约 ${booking.bookingRef}` : "代客预约";
  $("#bookingEditorId").value = booking?.id || "";
  $("#bookingName").value = booking?.name || "";
  $("#bookingEmail").value = booking?.email || "";
  $("#bookingNotes").value = booking?.internalNotes || "";
  const options = state.task.slots.filter((slot) => slot.status === "available" || (booking && slot.date === booking.date && slot.time === booking.time));
  $("#bookingSlot").innerHTML = options.map((slot) => `<option value="${slot.date}|${slot.time}" ${booking && slot.date === booking.date && slot.time === booking.time ? "selected" : ""}>${slot.date} ${slot.time}–${slot.endTime}</option>`).join("");
  $("#bookingEditorError").textContent = "";
  modal.hidden = false;
  $("#bookingName").focus();
}

async function saveBookingEditorV3(event) {
  event.preventDefault();
  const id = $("#bookingEditorId").value;
  const [date, time] = $("#bookingSlot").value.split("|");
  const payload = { name: $("#bookingName").value.trim(), email: $("#bookingEmail").value.trim(), date, time, internalNotes: $("#bookingNotes").value.trim() };
  try {
    await api(id ? `/api/admin/tasks/${state.task.id}/bookings/${id}` : `/api/admin/tasks/${state.task.id}/bookings`, { method: id ? "PUT" : "POST", body: JSON.stringify(payload) });
    $("#bookingEditorModal").hidden = true;
    toast(id ? "预约已更新并记录时间线" : "代客预约已创建");
    state.bookings = await api(`/api/admin/tasks/${state.task.id}/bookings`);
    await renderBookings();
  } catch (error) {
    $("#bookingEditorError").textContent = error.message;
  }
}
