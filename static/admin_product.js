/* Product capabilities layered on the shared admin shell. */

const baseTaskShell = taskShell;
const baseRenderTask = renderTask;
const baseRenderWizardStep = renderWizardStep;
const baseRenderPreview = renderPreview;

blankEditor = function () {
  const date = new Date(Date.now() + 86400000);
  const key = `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
  return {
    id: null,
    name: "",
    title: "",
    description: "",
    location: "",
    contact: "",
    timezone: "Asia/Shanghai",
    slotMinutes: 10,
    openingStrategy: "any",
    minAdvanceMinutes: 0,
    maxAdvanceDays: 365,
    dates: [key],
    periods: [{ label: "上午", start: "09:00", end: "12:00" }],
    dateOverrides: [],
    fields: [],
  };
};

openEditor = function (task = null) {
  state.editor = task ? JSON.parse(JSON.stringify(task)) : blankEditor();
  state.editor.minAdvanceMinutes ??= 0;
  state.editor.maxAdvanceDays ??= 365;
  state.editor.dateOverrides ??= [];
  const params = new URLSearchParams(location.search);
  state.step = Number(params.get("step")) || 1;
  state.forceRemoveBlocks = false;
  state.dirty = false;
  state.saving = false;
  renderWizard();
};

statusButtons = function (task) {
  const publishDraft = task.hasUnpublishedChanges && ["published", "paused"].includes(task.status)
    ? '<button class="btn primary" data-publish-version>发布修改</button>'
    : "";
  if (task.status === "draft") return '<button class="btn primary" data-status="published">发布</button>';
  if (task.status === "published") return `${publishDraft}<button class="btn warning" data-status="paused">暂停</button><button class="btn" data-status="ended">结束</button>`;
  if (task.status === "paused") return `${publishDraft}<button class="btn primary" data-status="published">恢复</button><button class="btn" data-status="ended">结束</button>`;
  if (task.status === "ended") return '<button class="btn" data-status="archived">归档</button>';
  return '<button class="btn" data-status="ended">恢复到已结束</button>';
};

taskShell = function (section, content) {
  baseTaskShell(section, content);
  const task = state.task;
  const titleRow = root.querySelector(".task-title-row");
  if (titleRow && task.publishedVersion) {
    titleRow.insertAdjacentHTML(
      "beforeend",
      `<span class="badge published">线上 v${task.publishedVersion}</span>${task.hasUnpublishedChanges ? '<span class="badge paused">有未发布修改</span>' : ""}`,
    );
  }
  const publishButton = root.querySelector("[data-publish-version]");
  if (publishButton) publishButton.onclick = publishVersion;
};

renderTask = function (section) {
  baseRenderTask(section);
  const task = state.task;
  const content = root.querySelector(".content");
  if (!content) return;
  if (section === "overview" && task.hasUnpublishedChanges) {
    content.insertAdjacentHTML(
      "afterbegin",
      `<div class="conflict" style="margin-bottom:14px"><strong>线上仍是 v${task.publishedVersion}</strong><br>当前编辑内容尚未发布，公开预约页不受影响。完成检查后点击“发布修改”。</div>`,
    );
  }
  if (section === "settings") {
    const hours = Number(task.minAdvanceMinutes || 0) / 60;
    content.insertAdjacentHTML(
      "beforeend",
      `<div class="info-grid" style="margin-top:12px"><div class="info"><strong>最少提前</strong>${hours ? `${hours} 小时` : "不限制"}</div><div class="info"><strong>最远可约</strong>未来 ${task.maxAdvanceDays || 365} 天</div><div class="info"><strong>日期例外</strong>${(task.dateOverrides || []).length} 条</div><div class="info"><strong>发布版本</strong>v${task.publishedVersion || 0}${task.hasUnpublishedChanges ? " · 待发布" : ""}</div></div>`,
    );
  }
};

async function publishVersion() {
  try {
    const result = await api(`/api/admin/tasks/${state.task.id}/publish`, { method: "POST", body: "{}" });
    toast(`已发布线上版本 v${result.version}`);
    await loadTask(state.task.id);
    renderTask(location.pathname.split("/").filter(Boolean)[3] || "overview");
  } catch (error) {
    toast(error.message);
  }
}

renderWizard = function () {
  const editor = state.editor;
  const labels = ["基本信息", "开放时间", "收集表单", "预览发布"];
  const isLive = ["published", "paused"].includes(editor.status);
  const intro = editor.id
    ? (isLive ? `保存后成为未发布修改，线上 v${editor.publishedVersion || 1} 保持不变。` : "修改后需保存才能生效。")
    : "按步骤完成设置，最后预览并发布。";
  const draftLabel = isLive ? "保存为未发布修改" : `保存${editor.id ? "修改" : "草稿"}`;
  const canPublish = !["ended", "archived"].includes(editor.status);
  root.innerHTML = `<div class="page-head"><div><h1>${editor.id ? "编辑预约任务" : "新建预约任务"}</h1><div class="subtle">${intro}</div></div><button class="btn" data-route="${editor.id ? `/admin/tasks/${editor.id}/settings` : "/admin"}">退出编辑</button></div><section class="panel wizard"><div class="steps">${labels.map((label, index) => `<button class="step ${state.step === index + 1 ? "active" : state.step > index + 1 ? "done" : ""}" data-step="${index + 1}">${index + 1}. ${label}</button>`).join("")}</div><div class="wizard-grid"><div id="wizardMain"></div><aside class="preview" id="livePreview"></aside></div><div class="error" id="wizardError"></div><div id="conflictBox"></div><div class="wizard-footer"><button class="btn" id="prevStep" ${state.step === 1 ? "disabled" : ""}>上一步</button><div class="actions">${state.step < 4 ? '<button class="btn primary" id="nextStep">下一步</button>' : `<button class="btn" id="saveDraft">${draftLabel}</button>${canPublish ? '<button class="btn primary" id="savePublish">保存并发布</button>' : ""}`}</div></div></section>`;
  renderWizardStep();
  root.querySelector(".wizard").addEventListener("input", () => state.dirty = true);
  root.querySelectorAll("[data-step]").forEach((button) => button.onclick = () => {
    syncWizard();
    state.step = Number(button.dataset.step);
    renderWizard();
  });
  $("#prevStep").onclick = () => { syncWizard(); state.step -= 1; renderWizard(); };
  if ($("#nextStep")) $("#nextStep").onclick = () => {
    syncWizard();
    const error = validateStep(state.step);
    if (error) { $("#wizardError").textContent = error; return; }
    state.step += 1;
    renderWizard();
  };
  if ($("#saveDraft")) $("#saveDraft").onclick = () => saveTask(false);
  if ($("#savePublish")) $("#savePublish").onclick = () => saveTask(true);
};

function overridePeriodRow(period, ruleIndex, periodIndex) {
  return `<div class="row-card"><div class="field"><label>名称</label><input aria-label="例外时段名称" data-override="${ruleIndex}" data-override-period="${periodIndex}" data-key="label" value="${esc(period.label)}"></div><div class="field"><label>开始</label><input aria-label="例外开始时间" type="time" data-override="${ruleIndex}" data-override-period="${periodIndex}" data-key="start" value="${period.start}"></div><div class="field"><label>结束</label><input aria-label="例外结束时间" type="time" data-override="${ruleIndex}" data-override-period="${periodIndex}" data-key="end" value="${period.end}"></div><button class="btn danger" data-remove-override-period="${ruleIndex}:${periodIndex}">删除</button></div>`;
}

function overrideRuleRow(rule, index) {
  const body = rule.closed
    ? '<div class="subtle">这一天完全关闭，不生成可预约时段。</div>'
    : `<div>${rule.periods.map((period, periodIndex) => overridePeriodRow(period, index, periodIndex)).join("")}</div><button class="btn small" data-add-override-period="${index}">添加例外时段</button>`;
  return `<section class="info" style="margin-top:8px"><div class="page-head" style="margin-bottom:8px"><div><strong>${rule.date}</strong><div class="subtle">${rule.closed ? "关闭该日" : "使用独立时段"}</div></div><button class="btn small danger" data-remove-override="${index}">移除例外</button></div>${body}</section>`;
}

renderWizardStep = function () {
  if (state.step !== 2) {
    baseRenderWizardStep();
    if (state.step === 4) {
      const editor = state.editor;
      const review = $("#wizardMain .review");
      review.insertAdjacentHTML("beforeend", `<div class="info"><strong>预约窗口</strong>至少提前 ${Number(editor.minAdvanceMinutes || 0) / 60} 小时<br>未来 ${editor.maxAdvanceDays || 365} 天</div><div class="info"><strong>日期例外</strong>${(editor.dateOverrides || []).length} 条</div>`);
      $("#wizardMain .conflict").innerHTML = `<strong>版本发布</strong><br>${["published", "paused"].includes(editor.status) ? `保存草稿不会影响线上 v${editor.publishedVersion || 1}；只有“保存并发布”才会切换公开页面。` : "保存草稿不会开放链接；保存并发布会创建第一个线上版本。"}`;
    }
    return;
  }
  const editor = state.editor;
  const availableDates = editor.dates.map((date) => `<option value="${date}">${date}</option>`).join("");
  $("#wizardMain").innerHTML = `<section class="editor-card"><h3>可预约日期</h3><div class="date-tools"><input id="singleDate" type="date"><button class="btn" id="addDate">添加日期</button></div><div class="range-row"><input id="rangeStart" type="date" aria-label="批量开始日期"><input id="rangeEnd" type="date" aria-label="批量结束日期"><button class="btn" id="addRange">批量添加</button></div><div class="weekday">${["日", "一", "二", "三", "四", "五", "六"].map((value, index) => `<label><input type="checkbox" value="${index}" checked>周${value}</label>`).join("")}</div><div class="chips">${editor.dates.map((date) => `<button class="chip" data-remove-date="${date}">${date} ×</button>`).join("")}</div></section><section class="editor-card" style="margin-top:12px"><div class="page-head"><h3>默认开放时段</h3><button class="btn" id="addPeriod">添加时段</button></div><div id="periodRows">${editor.periods.map((period, index) => periodRow(period, index)).join("")}</div><div class="info-grid"><div class="field"><label for="editDuration">单次预约时长</label><select id="editDuration">${[5, 10, 15, 20, 30, 60].map((value) => `<option value="${value}" ${Number(editor.slotMinutes) === value ? "selected" : ""}>${value} 分钟</option>`).join("")}</select></div><div class="field"><label for="editStrategy">开放方式</label><select id="editStrategy"><option value="any" ${editor.openingStrategy === "any" ? "selected" : ""}>全部时间立即开放</option><option value="sequential" ${editor.openingStrategy === "sequential" ? "selected" : ""}>按顺序逐个开放</option></select></div><div class="field"><label for="editMinAdvanceHours">最少提前（小时）</label><input id="editMinAdvanceHours" type="number" min="0" max="168" step="0.5" value="${Number(editor.minAdvanceMinutes || 0) / 60}"></div><div class="field"><label for="editMaxAdvanceDays">最远可预约（天）</label><input id="editMaxAdvanceDays" type="number" min="1" max="730" value="${editor.maxAdvanceDays || 365}"></div></div></section><section class="editor-card" style="margin-top:12px"><div><h3>日期例外</h3><div class="subtle">可关闭某一天，或为某一天使用不同于默认值的开放时段。</div></div><div class="range-row"><select id="overrideDate" aria-label="例外日期">${availableDates}</select><select id="overrideMode" aria-label="例外类型"><option value="closed">关闭该日</option><option value="custom">自定义时段</option></select><button class="btn" id="applyOverride">添加/替换例外</button></div><div id="overrideRows">${editor.dateOverrides.map(overrideRuleRow).join("") || '<div class="empty">还没有日期例外。</div>'}</div></section>`;
  bindScheduleEditor();
  renderPreview();
};

bindScheduleEditor = function () {
  $("#addDate").onclick = () => {
    const date = $("#singleDate").value;
    if (date && !state.editor.dates.includes(date)) state.editor.dates.push(date);
    state.editor.dates.sort();
    renderWizard();
  };
  $("#addRange").onclick = () => {
    const start = $("#rangeStart").value;
    const endValue = $("#rangeEnd").value;
    if (!start || !endValue) return;
    const days = new Set([...document.querySelectorAll(".weekday input:checked")].map((item) => Number(item.value)));
    for (let date = new Date(`${start}T12:00:00`), end = new Date(`${endValue}T12:00:00`); date <= end; date.setDate(date.getDate() + 1)) {
      if (!days.has(date.getDay())) continue;
      const key = `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
      if (!state.editor.dates.includes(key)) state.editor.dates.push(key);
    }
    state.editor.dates.sort();
    renderWizard();
  };
  document.querySelectorAll("[data-remove-date]").forEach((button) => button.onclick = () => {
    state.editor.dates = state.editor.dates.filter((date) => date !== button.dataset.removeDate);
    state.editor.dateOverrides = state.editor.dateOverrides.filter((rule) => rule.date !== button.dataset.removeDate);
    renderWizard();
  });
  $("#addPeriod").onclick = () => {
    state.editor.periods.push({ label: `时段 ${state.editor.periods.length + 1}`, start: "13:00", end: "17:00" });
    renderWizard();
  };
  $("#periodRows").addEventListener("input", (event) => {
    const index = Number(event.target.dataset.period);
    if (Number.isInteger(index)) state.editor.periods[index][event.target.dataset.key] = event.target.value;
    renderPreview();
  });
  document.querySelectorAll("[data-remove-period]").forEach((button) => button.onclick = () => {
    state.editor.periods.splice(Number(button.dataset.removePeriod), 1);
    renderWizard();
  });
  $("#editDuration").onchange = () => { state.editor.slotMinutes = Number($("#editDuration").value); renderPreview(); };
  $("#editStrategy").onchange = () => state.editor.openingStrategy = $("#editStrategy").value;
  $("#editMinAdvanceHours").oninput = () => {
    state.editor.minAdvanceMinutes = Math.round(Number($("#editMinAdvanceHours").value || 0) * 60);
    renderPreview();
  };
  $("#editMaxAdvanceDays").oninput = () => {
    state.editor.maxAdvanceDays = Number($("#editMaxAdvanceDays").value || 365);
    renderPreview();
  };
  $("#applyOverride").onclick = () => {
    const date = $("#overrideDate").value;
    if (!date) return;
    const closed = $("#overrideMode").value === "closed";
    const periods = closed ? [] : JSON.parse(JSON.stringify(state.editor.periods));
    state.editor.dateOverrides = state.editor.dateOverrides.filter((rule) => rule.date !== date);
    state.editor.dateOverrides.push({ date, closed, periods });
    state.editor.dateOverrides.sort((a, b) => a.date.localeCompare(b.date));
    renderWizard();
  };
  document.querySelectorAll("[data-remove-override]").forEach((button) => button.onclick = () => {
    state.editor.dateOverrides.splice(Number(button.dataset.removeOverride), 1);
    renderWizard();
  });
  document.querySelectorAll("[data-add-override-period]").forEach((button) => button.onclick = () => {
    const rule = state.editor.dateOverrides[Number(button.dataset.addOverridePeriod)];
    rule.periods.push({ label: `时段 ${rule.periods.length + 1}`, start: "13:00", end: "17:00" });
    renderWizard();
  });
  document.querySelectorAll("[data-remove-override-period]").forEach((button) => button.onclick = () => {
    const [ruleIndex, periodIndex] = button.dataset.removeOverridePeriod.split(":").map(Number);
    state.editor.dateOverrides[ruleIndex].periods.splice(periodIndex, 1);
    renderWizard();
  });
  $("#overrideRows").addEventListener("input", (event) => {
    const ruleIndex = Number(event.target.dataset.override);
    const periodIndex = Number(event.target.dataset.overridePeriod);
    if (!Number.isInteger(ruleIndex) || !Number.isInteger(periodIndex)) return;
    state.editor.dateOverrides[ruleIndex].periods[periodIndex][event.target.dataset.key] = event.target.value;
    renderPreview();
  });
};

syncWizard = function () {
  const editor = state.editor;
  if ($("#editName")) {
    editor.name = $("#editName").value.trim();
    editor.title = $("#editTitle").value.trim();
    editor.description = $("#editDescription").value.trim();
    editor.location = $("#editLocation").value.trim();
    editor.contact = $("#editContact").value.trim();
    editor.timezone = $("#editTimezone").value;
  }
  if ($("#editDuration")) {
    editor.slotMinutes = Number($("#editDuration").value);
    editor.openingStrategy = $("#editStrategy").value;
    editor.minAdvanceMinutes = Math.round(Number($("#editMinAdvanceHours").value || 0) * 60);
    editor.maxAdvanceDays = Number($("#editMaxAdvanceDays").value || 365);
  }
};

validateStep = function (step) {
  const editor = state.editor;
  if (step === 1 && (!editor.name || !editor.title)) return "请填写后台任务名称和公开页面标题。";
  if (step === 2) {
    if (!editor.dates.length || !editor.periods.length) return "请至少设置一个日期和一个默认开放时段。";
    if (editor.minAdvanceMinutes < 0 || editor.minAdvanceMinutes > 10080) return "最少提前时间必须在 0–168 小时之间。";
    if (editor.maxAdvanceDays < 1 || editor.maxAdvanceDays > 730) return "最远可预约天数必须在 1–730 天之间。";
    for (const rule of editor.dateOverrides) if (!rule.closed && !rule.periods.length) return `${rule.date} 的例外时段不能为空。`;
  }
  if (step === 3) {
    for (const field of editor.fields) {
      if (!field.label) return "请填写所有附加字段名称。";
      if (field.type === "select" && (field.options || []).length < 2) return `“${field.label}”至少需要两个选项。`;
    }
  }
  return "";
};

renderPreview = function () {
  baseRenderPreview();
  $("#livePreview").insertAdjacentHTML(
    "beforeend",
    `<div class="subtle" style="margin-top:10px">至少提前 ${Number(state.editor.minAdvanceMinutes || 0) / 60} 小时 · 最远 ${state.editor.maxAdvanceDays || 365} 天 · ${(state.editor.dateOverrides || []).length} 条日期例外</div>`,
  );
};

saveTask = async function (publish, force = false) {
  if (state.saving) return;
  syncWizard();
  for (let step = 1; step <= 3; step += 1) {
    const error = validateStep(step);
    if (error) {
      state.step = step;
      renderWizard();
      $("#wizardError").textContent = error;
      return;
    }
  }
  state.saving = true;
  root.querySelectorAll(".wizard-footer button").forEach((button) => button.disabled = true);
  const payload = { ...state.editor, forceRemoveBlocks: force };
  ["slots", "createdAt", "updatedAt", "isDefault", "publishedVersion", "hasUnpublishedChanges"].forEach((key) => delete payload[key]);
  try {
    let task;
    if (state.editor.id) task = await api(`/api/admin/tasks/${state.editor.id}`, { method: "PUT", body: JSON.stringify(payload) });
    else task = await api("/api/admin/tasks", { method: "POST", body: JSON.stringify(payload) });
    if (publish) {
      if (task.status === "draft") {
        await api(`/api/admin/tasks/${task.id}/status`, { method: "POST", body: JSON.stringify({ status: "published" }) });
      } else {
        await api(`/api/admin/tasks/${task.id}/publish`, { method: "POST", body: "{}" });
      }
    }
    state.dirty = false;
    toast(publish ? "任务已保存并发布新版本" : (task.publishedVersion ? "修改已保存，线上版本未变化" : "任务已保存"));
    route(`/admin/tasks/${task.id}/overview`, true);
  } catch (error) {
    state.saving = false;
    root.querySelectorAll(".wizard-footer button").forEach((button) => button.disabled = false);
    if (error.data?.requiresBlockCleanup) {
      $("#conflictBox").innerHTML = `<div class="conflict"><strong>${esc(error.message)}</strong><br>这些占用不再属于新开放时间。<div class="actions" style="margin-top:8px"><button class="btn warning" id="forceSave">确认清理并保存</button></div></div>`;
      $("#forceSave").onclick = () => saveTask(publish, true);
    } else $("#wizardError").textContent = error.message;
  }
};

if (!$("#adminApp").hidden) renderRoute();
