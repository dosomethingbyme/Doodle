const routeParts = location.pathname.split("/").filter(Boolean);
let publicId = routeParts[0] === "b" ? routeParts[1] : "";
const manageRef = routeParts[0] === "manage" ? routeParts[1] : "";
const state = {
  task: null,
  selectedDate: "",
  selectedTime: "",
  verificationToken: "",
  verifiedEmail: "",
  currentBooking: null,
  reschedules: [],
  rescheduling: false,
  idempotencyKey: "",
  manageToken: "",
};
const $ = (selector) => document.querySelector(selector);
const app = $("#app");
const stateCard = $("#stateCard");
const dateTabs = $("#dateTabs");
const slotGroups = $("#slotGroups");

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const data = response.headers.get("content-type")?.includes("json")
    ? await response.json()
    : await response.text();
  if (!response.ok) throw new Error(data.error || "请求失败");
  return data;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;",
  })[character]);
}

function toDate(key) {
  const [year, month, day] = key.split("-").map(Number);
  return new Date(year, month - 1, day);
}

const dateFormat = new Intl.DateTimeFormat("zh-CN", { month: "numeric", day: "numeric", weekday: "short" });

function toast(message) {
  $("#toast").textContent = message;
  $("#toast").classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => $("#toast").classList.remove("show"), 2600);
}

function showState(title, message) {
  app.hidden = true;
  stateCard.hidden = false;
  stateCard.innerHTML = `<h1>${escapeHtml(title)}</h1><p>${escapeHtml(message)}</p>`;
}

function renderTask() {
  const task = state.task;
  document.title = task.title;
  $("#taskTitle").textContent = task.title;
  $("#taskDescription").textContent = task.description || "";
  const meta = [];
  if (task.location) meta.push(`地点：${escapeHtml(task.location)}`);
  if (task.contact) meta.push(`联系：${escapeHtml(task.contact)}`);
  meta.push(`每次 ${task.slotMinutes} 分钟`);
  if (task.minAdvanceMinutes) meta.push(`至少提前 ${task.minAdvanceMinutes / 60} 小时`);
  meta.push(`可预约未来 ${task.maxAdvanceDays} 天`);
  meta.push(task.timezone === "Asia/Shanghai" ? "中国标准时间（UTC+8）" : `时区：${escapeHtml(task.timezone)}`);
  $("#taskMeta").innerHTML = meta.map((value) => `<span class="pill">${value}</span>`).join("");
  $("#policyCard").hidden = !(task.policyText || task.privacyText);
  $("#policyCard").innerHTML = `${task.policyText ? `<h3>预约政策</h3><p>${escapeHtml(task.policyText)}</p>` : ""}${task.privacyText ? `<h3>隐私说明</h3><p>${escapeHtml(task.privacyText)}</p>` : ""}`;
  $("#statusBanner").hidden = task.status === "published";
  $("#statusBanner").textContent = task.status === "paused"
    ? "新预约和改期已暂停。已有预约仍然有效，你可以在下方验证邮箱并管理预约。"
    : task.status === "ended"
      ? "此任务已结束，不再接受新预约或改期。已有预约仍可查询或取消。"
      : "";
  $("#customFields").innerHTML = task.fields.map(renderCustomField).join("");
  if (!task.dates.includes(state.selectedDate)) {
    state.selectedDate = state.currentBooking?.date && task.dates.includes(state.currentBooking.date)
      ? state.currentBooking.date
      : task.dates[0] || "";
  }
  renderDates();
  renderSlots();
  updateForm();
  app.hidden = false;
  stateCard.hidden = true;
}

function renderDates() {
  dateTabs.innerHTML = state.task.dates.map((key) => {
    const slots = state.task.slots.filter((slot) => slot.date === key);
    const available = slots.filter((slot) => slot.status === "available").length;
    const override = state.task.dateOverrides?.find((item) => item.date === key);
    const label = override?.closed ? "该日关闭" : available ? `${available} 个可预约` : "暂无可约";
    return `<button class="date-tab ${key === state.selectedDate ? "active" : ""}" type="button" data-date="${key}" aria-pressed="${key === state.selectedDate}"><strong>${dateFormat.format(toDate(key))}</strong><span>${label}</span></button>`;
  }).join("");
}

function statusLabel(slot) {
  if (slot.status === "closed" && slot.availabilityReason === "too_soon") return "尚未开放";
  if (slot.status === "closed" && slot.availabilityReason === "too_far") return "超出范围";
  return { available: "可预约", occupied: "已占用", locked: "待开放", closed: "不可预约" }[slot.status] || slot.status;
}

function renderSlots() {
  const slots = state.task.slots.filter((slot) => slot.date === state.selectedDate);
  const groups = new Map();
  slots.forEach((slot) => {
    if (!groups.has(slot.periodId)) groups.set(slot.periodId, { label: slot.periodLabel, slots: [] });
    groups.get(slot.periodId).slots.push(slot);
  });
  slotGroups.innerHTML = groups.size
    ? [...groups.values()].map((group) => `<section class="period"><div class="period-head"><strong>${escapeHtml(group.label)}</strong><span>${group.slots[0].time}–${group.slots.at(-1).endTime}</span></div><div class="slots">${group.slots.map((slot) => `<button type="button" class="slot ${slot.status} ${slot.time === state.selectedTime ? "selected" : ""}" data-time="${slot.time}" aria-pressed="${slot.time === state.selectedTime}" ${slot.status !== "available" ? "disabled" : ""}>${slot.time}${slot.time === state.selectedTime ? " ✓" : ""}<small>${statusLabel(slot)}</small></button>`).join("")}</div></section>`).join("")
    : '<div class="empty">该日期暂无开放时间。</div>';
}

function renderCustomField(field, index) {
  const label = `<label for="custom_${index}">${escapeHtml(field.label)}${field.required ? "" : '<span class="optional">选填</span>'}</label>`;
  if (field.type === "textarea") return `<div class="field">${label}<textarea id="custom_${index}" data-key="${field.key}" ${field.required ? "required" : ""}></textarea></div>`;
  if (field.type === "select") return `<div class="field">${label}<select id="custom_${index}" data-key="${field.key}" ${field.required ? "required" : ""}><option value="">请选择</option>${field.options.map((value) => `<option value="${escapeHtml(value)}">${escapeHtml(value)}</option>`).join("")}</select></div>`;
  return `<div class="field">${label}<input id="custom_${index}" data-key="${field.key}" type="${field.type === "phone" ? "tel" : "text"}" ${field.required ? "required" : ""}></div>`;
}

function updateForm() {
  const verified = state.verificationToken && state.verifiedEmail === $("#email").value.trim().toLowerCase();
  $("#verifyNotice").classList.toggle("ok", Boolean(verified));
  $("#verifyNotice").textContent = verified
    ? state.currentBooking
      ? (state.rescheduling ? "请选择新的可预约时间，然后确认改期。" : "邮箱已验证，并找到您在此任务中的预约。")
      : "邮箱已验证，当前没有此任务的有效预约。完成人员信息后即可提交。"
    : "完成邮箱验证后即可提交预约。";
  $("#currentBookingCard").hidden = !state.currentBooking;
  if (state.currentBooking) {
    $("#currentBookingTime").textContent = `${state.currentBooking.date} ${state.currentBooking.time}–${state.currentBooking.endTime}`;
    $("#currentBookingRef").textContent = state.currentBooking.bookingRef ? `预约编号：${state.currentBooking.bookingRef}` : "";
    $("#calendarLink").hidden = !(state.currentBooking.bookingRef && state.manageToken);
    if (!$("#calendarLink").hidden) $("#calendarLink").href = `/api/public/bookings/${encodeURIComponent(state.currentBooking.bookingRef)}.ics?token=${encodeURIComponent(state.manageToken)}`;
    $("#currentBookingLocation").textContent = state.task.location ? `地点：${state.task.location}` : "";
    const rescheduleCount = state.currentBooking.rescheduleCount || state.reschedules.length;
    $("#rescheduleHistory").textContent = rescheduleCount
      ? `已改期 ${rescheduleCount} 次，历史记录已保留。`
      : "尚未改期。";
  }
  $("#startReschedule").hidden = !state.currentBooking || state.rescheduling || state.task.status !== "published";
  $("#cancelReschedule").hidden = !state.rescheduling;
  const selected = state.task?.slots.find((slot) => slot.date === state.selectedDate && slot.time === state.selectedTime);
  $("#selectionText").textContent = selected ? `${state.selectedDate} ${selected.time}–${selected.endTime}` : "尚未选择时间";
  $("#selectionSummary").hidden = state.task.status !== "published" || Boolean(state.currentBooking && !state.rescheduling);
  $("#submitBooking").textContent = state.rescheduling ? "确认改期" : "确认预约";
  $("#submitBooking").disabled = !(state.task.status === "published" && verified && selected && (state.rescheduling || (!state.currentBooking && $("#name").value.trim())));
}

async function lookupCurrentBooking() {
  const result = await api(`/api/public/tasks/${publicId}/bookings/lookup`, {
    method: "POST",
    body: JSON.stringify({ email: state.verifiedEmail, verificationToken: state.verificationToken }),
  });
  state.currentBooking = result.booking;
  state.reschedules = result.reschedules || [];
  state.rescheduling = false;
  updateForm();
}

async function refresh() {
  state.task = await api(`/api/public/tasks/${publicId}`);
  if (["draft", "archived"].includes(state.task.status)) {
    const copy = state.task.status === "draft"
      ? ["尚未开放", "此预约任务仍在准备中。"]
      : ["预约已归档", "此预约任务已经结束。"];
    showState(copy[0], copy[1]);
    return;
  }
  renderTask();
}

dateTabs.addEventListener("click", (event) => {
  const button = event.target.closest("[data-date]");
  if (!button) return;
  state.selectedDate = button.dataset.date;
  state.selectedTime = "";
  renderDates();
  renderSlots();
  updateForm();
});

slotGroups.addEventListener("click", (event) => {
  const button = event.target.closest("[data-time]");
  if (!button || button.disabled) return;
  state.selectedTime = button.dataset.time;
  renderSlots();
  updateForm();
});

$("#email").addEventListener("input", () => {
  if ($("#email").value.trim().toLowerCase() !== state.verifiedEmail) {
    state.verificationToken = "";
    state.currentBooking = null;
    state.reschedules = [];
    state.rescheduling = false;
  }
  updateForm();
});
$("#name").addEventListener("input", updateForm);

$("#sendCode").addEventListener("click", async () => {
  try {
    const email = $("#email").value.trim();
    const result = await api("/api/verification/send", { method: "POST", body: JSON.stringify({ email }) });
    if (result.devCode) $("#emailCode").value = result.devCode;
    toast(result.message);
  } catch (error) {
    toast(error.message);
  }
});

$("#verifyCode").addEventListener("click", async () => {
  try {
    const email = $("#email").value.trim().toLowerCase();
    const result = await api("/api/verification/verify", {
      method: "POST",
      body: JSON.stringify({ email, code: $("#emailCode").value.trim() }),
    });
    state.verifiedEmail = email;
    state.verificationToken = result.verificationToken;
    await lookupCurrentBooking();
    toast("邮箱验证成功");
  } catch (error) {
    toast(error.message);
  }
});

$("#startReschedule").addEventListener("click", () => {
  state.rescheduling = true;
  state.selectedDate = state.currentBooking.date;
  state.selectedTime = "";
  renderDates();
  renderSlots();
  updateForm();
  $(".schedule-panel").scrollIntoView({ behavior: "smooth", block: "start" });
});

$("#cancelReschedule").addEventListener("click", () => {
  state.rescheduling = false;
  state.selectedTime = "";
  renderSlots();
  updateForm();
});

$("#bookingForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("#formError").textContent = "";
  try {
    if (state.rescheduling) {
      const result = await api(`/api/public/tasks/${publicId}/bookings/by-email`, {
        method: "PUT",
        body: JSON.stringify({
          email: $("#email").value.trim(),
          verificationToken: state.verificationToken,
          date: state.selectedDate,
          time: state.selectedTime,
        }),
      });
      state.currentBooking = result;
      state.reschedules = result.reschedules || [];
      state.rescheduling = false;
      state.selectedTime = "";
      await refresh();
      toast("改期成功，原时间已释放");
      return;
    }
    const answers = {};
    document.querySelectorAll("[data-key]").forEach((input) => answers[input.dataset.key] = input.value.trim());
    for (const field of state.task.fields) {
      if (field.required && !answers[field.key]) {
        $("#formError").textContent = `请填写${field.label}`;
        return;
      }
    }
    state.idempotencyKey ||= crypto.randomUUID();
    const result = await api(`/api/public/tasks/${publicId}/bookings`, {
      method: "POST",
      headers: { "Idempotency-Key": state.idempotencyKey },
      body: JSON.stringify({
        name: $("#name").value.trim(),
        email: $("#email").value.trim(),
        verificationToken: state.verificationToken,
        date: state.selectedDate,
        time: state.selectedTime,
        answers,
      }),
    });
    state.currentBooking = result;
    state.manageToken = result.manageToken || state.manageToken;
    state.idempotencyKey = "";
    state.selectedTime = "";
    await refresh();
    updateForm();
    toast("预约成功，预约信息已显示在页面中");
    if (result.emailWarning) toast(result.emailWarning);
  } catch (error) {
    $("#formError").textContent = error.message;
  }
});

$("#cancelBooking").addEventListener("click", () => { $("#publicCancelModal").hidden = false; $("#keepBooking").focus(); });
$("#keepBooking").addEventListener("click", () => $("#publicCancelModal").hidden = true);
document.addEventListener("keydown", (event) => { if (event.key === "Escape" && !$("#publicCancelModal").hidden) { $("#publicCancelModal").hidden = true; $("#cancelBooking").focus(); } });
$("#confirmPublicCancel").addEventListener("click", async () => {
  try {
    await api(`/api/public/tasks/${publicId}/bookings/by-email`, {
      method: "DELETE",
      body: JSON.stringify({
        email: $("#email").value.trim(),
        verificationToken: state.verificationToken,
        reason: $("#publicCancelReason").value.trim(),
      }),
    });
    $("#publicCancelModal").hidden = true;
    state.currentBooking = null;
    state.reschedules = [];
    state.rescheduling = false;
    $("#publicCancelReason").value = "";
    toast("预约已取消，您可以重新选择时间");
    await refresh();
    updateForm();
  } catch (error) {
    toast(error.message);
  }
});

async function bootstrap() {
  if (manageRef) {
    const token = new URLSearchParams(location.search).get("token") || "";
    const result = await api(`/api/public/bookings/${encodeURIComponent(manageRef)}?token=${encodeURIComponent(token)}`);
    publicId = result.publicId;
    state.currentBooking = result.booking;
    state.reschedules = result.reschedules || [];
    state.verificationToken = result.verificationToken;
    state.manageToken = token;
    state.verifiedEmail = result.booking.email.toLowerCase();
    $("#email").value = result.booking.email;
    $("#email").readOnly = true;
    $("#name").value = result.booking.name;
    $("#name").readOnly = true;
    $("#emailCode").closest(".field").hidden = true;
    $("#sendCode").hidden = true;
  }
  await refresh();
  updateForm();
}

bootstrap().catch((error) => showState("无法打开预约任务", error.message));
