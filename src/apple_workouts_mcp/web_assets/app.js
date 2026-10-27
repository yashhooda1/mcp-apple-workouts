/* HoodaRun browser client: no frameworks, no third-party scripts, no token storage. */
"use strict";

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const GOALS = { mile: "4:59", "5k": "16:59", half: "1:19:59", marathon: "2:59:59" };
let bearer = "";
let connected = false;
let latestOverview = null;
let toastTimeout = null;

function node(tag, className, text) {
  const out = document.createElement(tag);
  if (className) out.className = className;
  if (text !== undefined && text !== null) out.textContent = String(text);
  return out;
}
function append(parent, ...children) {
  children.forEach((child) => parent.append(child));
  return parent;
}
function replace(container, ...children) {
  container.replaceChildren(...children);
}
function isoLocal(date) {
  return [date.getFullYear(), String(date.getMonth() + 1).padStart(2, "0"),
    String(date.getDate()).padStart(2, "0")].join("-");
}
function addDays(date, n) {
  const copy = new Date(date.getFullYear(), date.getMonth(), date.getDate());
  copy.setDate(copy.getDate() + n);
  return copy;
}
function nextMonday() {
  const now = new Date();
  return addDays(now, (8 - now.getDay()) % 7 || 7);
}
function dateFromISO(value) {
  return new Date(value + "T12:00:00");
}
function dateLabel(value) {
  if (!value) return "TBA";
  return dateFromISO(value).toLocaleDateString(undefined, {
    month: "short", day: "numeric", year: "numeric"
  });
}
function weekdayLabel(value) {
  if (!value) return "TBA";
  return dateFromISO(value).toLocaleDateString(undefined, { weekday: "short" });
}
function value(form, key) {
  return form.elements.namedItem(key)?.value?.trim() ?? "";
}
function toast(message, error = false) {
  const out = $("#toast");
  out.textContent = message;
  out.classList.toggle("is-error", error);
  out.classList.add("is-visible");
  window.clearTimeout(toastTimeout);
  toastTimeout = window.setTimeout(() => out.classList.remove("is-visible"), 5200);
}
function show(view) {
  $$(".view").forEach((section) =>
    section.classList.toggle("is-visible", section.id === "view-" + view));
  $$(".nav-link").forEach((link) => {
    const active = link.dataset.view === view;
    link.classList.toggle("is-current", active);
    if (active) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
  $("#page-title").textContent = {
    dashboard: "Overview", plans: "Training plans", workouts: "Workout builder",
    strength: "Strength studio", nutrition: "Fuel & groceries",
    races: "Texas races", schedule: "Watch schedule"
  }[view];
  window.scrollTo({ top: 0, behavior: "smooth" });
}
function markConnection(ok) {
  connected = ok;
  const status = $("#connection-status");
  status.classList.toggle("connected", ok);
  status.lastChild.textContent = ok ? " Connected" : " Not connected";
  $("#open-connection").textContent = ok ? "Change connection" : "Connect server";
}
function connectionDialog(open) {
  $("#connection-dialog").hidden = !open;
  $("#connect-error").textContent = "";
  if (open) $("#server-token").focus();
}
async function api(payload, tokenOverride = bearer) {
  const headers = { "Content-Type": "application/json" };
  if (tokenOverride) headers.Authorization = "Bearer " + tokenOverride;
  const response = await fetch("/api/web", {
    method: "POST", headers, body: JSON.stringify(payload),
    credentials: "omit", cache: "no-store"
  });
  let body = {};
  try { body = await response.json(); } catch { /* Server might be unavailable. */ }
  if (!response.ok) {
    if (response.status === 401) markConnection(false);
    throw new Error(body.error || ("Request failed (HTTP " + response.status + ")"));
  }
  return body;
}
async function refreshOverview() {
  const overview = await api({ action: "overview" });
  latestOverview = overview;
  markConnection(true);
  $("#metric-total").textContent = overview.count;
  $("#metric-upcoming").textContent = overview.upcoming_count;
  $("#metric-onwatch").textContent = overview.on_watch_count;
  $("#metric-sync").textContent = overview.last_sync
    ? dateLabel(overview.last_sync.slice(0, 10)) : "No sync yet";
  renderSessions($("#dashboard-upcoming"), overview.upcoming.slice(0, 5));
  renderSessions($("#schedule-output"), overview.upcoming);
}
function renderSessions(target, sessions) {
  target.replaceChildren();
  if (!sessions.length) {
    target.append(node("div", "empty-state", "No future sessions in your workout feed yet."));
    return;
  }
  sessions.forEach((s) => {
    const row = node("div", "session-row");
    const date = node("div", "session-date");
    const dateObj = dateFromISO(s.date);
    append(date, node("strong", "", dateObj.getDate()),
      node("small", "", dateObj.toLocaleDateString(undefined, { month: "short" }).toUpperCase()));
    const meta = node("div", "session-meta");
    append(meta, node("strong", "", s.name),
      node("small", "", weekdayLabel(s.date) + " · " + s.sport +
        (s.plan ? " · " + s.plan : "")));
    const state = node("span", "sync-label" + (s.on_watch ? " confirmed" : ""),
      s.on_watch ? "On Watch" : "Pending sync");
    append(row, date, meta, state);
    target.append(row);
  });
}
async function submitPreview(target, request, button) {
  button.disabled = true;
  const oldText = button.textContent;
  button.textContent = "Building preview…";
  try {
    const data = await api({ action: "preview", ...request });
    renderPreview(target, data, request);
    toast("Preview ready. Review the details before publishing.");
  } catch (error) {
    replace(target, node("div", "info-box warning", error.message));
    toast(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = oldText;
  }
}
function infoPair(key, val) {
  const card = node("div");
  append(card, node("small", "", key), node("strong", "", val || "—"));
  return card;
}
function renderPreview(target, data, request) {
  target.replaceChildren();
  const header = node("div", "result-header");
  const copy = node("div");
  append(copy, node("div", "eyebrow", "PREVIEW ONLY"),
    node("h3", "", data.name || data.sessions?.[0]?.name || "Workout preview"),
    node("p", "", data.count + " scheduled session" + (data.count === 1 ? "" : "s") +
      " · Nothing has been published yet."));
  append(header, copy, node("span", "chip", "REVIEW FIRST"));
  target.append(header);

  const report = data.report || {};
  const details = node("div", "detail-grid");
  if (request.kind === "road") {
    append(details, infoPair("GOAL", report.goal_time),
      infoPair("TRAINING PACE / MI", report.training_pace_per_mile),
      infoPair("WEEKS", report.weeks),
      infoPair("RUNNING DAYS", report.days_per_week));
  } else if (request.kind === "marathon") {
    append(details, infoPair("GOAL REQUESTED", report.goal_requested),
      infoPair("GOAL BUILT TOWARD", report.goal_built_toward),
      infoPair("WEEKS", report.weeks),
      infoPair("SESSIONS", data.count));
  } else {
    append(details, infoPair("DATE", dateLabel(data.sessions[0].date)),
      infoPair("WORKOUT STEPS", data.sessions[0].steps));
  }
  target.append(details);
  const note = report.warning || report.safety || (request.kind === "marathon"
    ? "Training load is scaled to the available mileage; a stretch goal may be revised."
    : "Published workouts remain pending until HoodaRun on your iPhone syncs them.");
  target.append(node("div", "info-box" +
    (/warning|benchmark|slower|provisional/i.test(note) ? " warning" : ""), note));

  target.append(node("div", "section-label", "WORKOUT SCHEDULE"));
  const list = node("div", "preview-list");
  for (const item of data.sessions.slice(0, 80)) {
    const row = node("div", "preview-item");
    append(row, node("span", "", dateLabel(item.date)),
      node("span", "", item.name),
      node("span", "", item.sport === "strength" ? "LIFT" : "RUN"));
    list.append(row);
    if (item.exercises) for (const exercise of item.exercises) {
      list.append(node("div", "preview-item", ""));
      list.lastChild.append(node("span", "", ""), node("span", "",
        exercise.sets + " sets · " + (exercise.exercise || exercise.category) +
        " · " + (exercise.reps || exercise.hold)), node("span", "", "SET"));
    }
  }
  target.append(list);
  if (data.sessions.length > 80)
    target.append(node("p", "muted text-small", "Showing first 80 sessions in this preview."));

  const publish = node("div", "publish-controls");
  const button = node("button", "btn btn-primary", "Publish to HoodaRun feed ↗");
  button.type = "button";
  button.addEventListener("click", async () => {
    const description = "Publish " + data.count + " session(s) to your workout feed? " +
      "Existing sessions of the same plan name may be replaced. " +
      "Your iPhone must sync before they reach Apple Watch.";
    if (!window.confirm(description)) return;
    button.disabled = true;
    button.textContent = "Publishing…";
    try {
      const result = await api({
        action: "publish", ...request, preview_token: data.preview_token
      });
      replace(publish, node("div", "info-box",
        result.published + " session(s) written to the workout feed. " +
        "Open HoodaRun on iPhone and sync to schedule on the Watch."));
      toast("Published. iPhone Watch sync still required.");
      await refreshOverview();
    } catch (error) {
      button.disabled = false;
      button.textContent = "Publish to HoodaRun feed ↗";
      toast(error.message, true);
    }
  });
  append(publish, button, node("small", "",
    "Approval required · Preview valid for 15 minutes · iPhone sync required"));
  target.append(publish);
}
function invalidatePreview(output) {
  const button = output.querySelector(".publish-controls button");
  if (!button || button.disabled) return;
  button.disabled = true;
  button.textContent = "Inputs changed — preview again";
}
function changePlanDistance() {
  const dist = $("#plan-distance").value;
  $("#plan-goal").value = GOALS[dist];
  $$(".road-only").forEach((x) => { x.hidden = dist === "marathon"; });
  $("#plan-race").required = dist === "marathon";
  const next = nextMonday();
  if (dist === "marathon" && !$("#plan-race").value) {
    $("#plan-race").value = isoLocal(addDays(next, 16 * 7 - 1));
  }
}
function syncPlanRaceDate() {
  const start = $("#plan-start").value;
  if (start && !$("#plan-race").required && !$("#plan-race").value) {
    // Optional for road events; leave it empty so the Python engine picks the end date.
    return;
  }
}
function planRequest(form) {
  const distance = value(form, "distance");
  const marathon = distance === "marathon";
  const params = {
    goal_time: value(form, "goal_time"),
    start_date: value(form, "start_date") || undefined,
    current_weekly_miles: Number(value(form, "mileage")),
    days_per_week: Number(value(form, "days"))
  };
  if (marathon) {
    params.race_name = "HoodaRun Marathon";
    params.race_date = value(form, "race_date");
    params.heat_penalty = Number(value(form, "heat"));
    if (!params.race_date) throw Error("Marathon race date is required.");
  } else {
    params.distance = distance;
    params.weeks = Number(value(form, "weeks"));
    params.race_date = value(form, "race_date") || undefined;
    params.heat_penalty_s_per_mile = Number(value(form, "heat"));
    if (value(form, "benchmark_time")) {
      params.benchmark_time = value(form, "benchmark_time");
      params.benchmark_distance = value(form, "benchmark_distance");
    }
  }
  Object.keys(params).forEach((key) => {
    if (params[key] === undefined) delete params[key];
  });
  return { kind: marathon ? "marathon" : "road", params };
}
function workoutRequest(form) {
  const kind = value(form, "type");
  const name = value(form, "name");
  let steps;
  if (kind === "easy") {
    const miles = Number(value(form, "easy_miles"));
    if (!(miles > 0)) throw Error("Easy-run distance must be positive.");
    steps = [{ kind: "run", duration: miles + "mi", target: "hr:2" }];
  } else {
    const fast = value(form, "pace_fast");
    const slow = value(form, "pace_slow");
    const target = fast && slow ? "pace:" + fast + "-" + slow : null;
    steps = [
      { kind: "warmup", duration: value(form, "warmup"), target: "hr:2" },
      { repeat: Number(value(form, "reps")), steps: [
        { kind: "run", duration: value(form, "work"), target },
        { kind: value(form, "recovery_kind"), duration: value(form, "recovery"),
          target: value(form, "recovery_kind") === "rest" ? null : "hr:2",
          label: value(form, "recovery_kind") === "rest" ? "Standing recovery" : "Jog easy" }
      ] },
      { kind: "cooldown", duration: value(form, "cooldown"), target: "hr:2" }
    ];
  }
  return {
    kind: "workout",
    params: { schedule_date: value(form, "date"), spec: {
      name, location: value(form, "location"), steps
    } }
  };
}
function strengthRequest(form) {
  return { kind: "strength", params: {
    schedule_date: value(form, "date"), preset: value(form, "preset"),
    phase: value(form, "phase")
  } };
}
function showWorkoutType() {
  const isEasy = $("#workout-type").value === "easy";
  $$(".interval-only").forEach((item) => { item.hidden = isEasy; });
  $$(".easy-only").forEach((item) => { item.hidden = !isEasy; });
  $$(".interval-only input").forEach((item) => { item.required = !isEasy; });
}
function addMeal(parent, title, content) {
  const p = node("p");
  append(p, node("b", "", title), node("span", "", content));
  parent.append(p);
}
function renderNutrition(data) {
  const target = $("#nutrition-output");
  target.replaceChildren();
  const heading = node("div", "panel-head");
  append(heading, node("h3", "", "Your seven-day menu"),
    node("span", "chip", data.diet.replaceAll("_", " ").toUpperCase()));
  target.append(heading);
  const days = node("div", "meal-grid");
  data.days.forEach((day) => {
    const card = node("article", "meal-day");
    card.append(node("div", "eyebrow", day.day.toUpperCase()));
    addMeal(card, "Breakfast", day.breakfast);
    addMeal(card, "Lunch", day.lunch);
    addMeal(card, "Dinner", day.dinner);
    addMeal(card, "Snack", day.snack);
    if (day.run_fueling) addMeal(card, "Long-run fuel", day.run_fueling);
    days.append(card);
  });
  target.append(days);
  target.append(node("div", "section-header",
    "")); // heading inserted safely with DOM nodes
  const header = target.lastChild;
  append(header, append(node("div"), node("div", "eyebrow", "SHOPPING LIST"),
    node("h2", "", "One trip. All the essentials.")));
  const copy = node("button", "btn btn-secondary btn-sm", "Copy list");
  copy.type = "button";
  copy.addEventListener("click", async () => {
    const text = Object.entries(data.shopping_list).map(([group, items]) =>
      group.toUpperCase() + "\n" + items.map((item) => "• " + item.item).join("\n"))
      .join("\n\n");
    try {
      await navigator.clipboard.writeText(text);
      toast("Grocery list copied.");
    } catch { toast("Clipboard not available in this browser.", true); }
  });
  header.append(copy);
  const grouped = node("div", "grocery-grid");
  for (const [group, items] of Object.entries(data.shopping_list)) {
    const block = node("div", "grocery-group");
    block.append(node("h3", "", group));
    for (const item of items) {
      const label = node("label", "grocery-item");
      const input = document.createElement("input");
      input.type = "checkbox";
      append(label, input, node("span", "", item.item));
      block.append(label);
    }
    grouped.append(block);
  }
  target.append(grouped);
  target.append(node("div", "info-box", data.caution + " " +
    data.nutrition.micronutrients));
}
function renderRaces(data) {
  const target = $("#race-output");
  target.replaceChildren();
  if (!data.races.length) {
    target.append(node("div", "empty-state",
      "No matching future dates in the curated list. Try another distance or metro."));
  }
  for (const race of data.races) {
    const card = node("article", "race-card");
    const top = node("div", "race-card-top");
    append(top, node("span", "chip", race.area.toUpperCase()),
      node("span", "race-date", race.date ? dateLabel(race.date) : "DATES TBD"));
    append(card, top, node("h3", "", race.name),
      node("div", "race-meta", race.city + " · " + race.distances.join(" / ")),
      node("p", "", race.notes));
    const link = node("a", "", "Official race website ↗");
    link.href = race.url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    card.append(link);
    target.append(card);
  }
  const foot = node("div", "info-box");
  foot.textContent = data.caveat + " Calendar last reviewed: " + data.checked_on;
  target.after(foot);
  const olderFoot = target.parentNode.querySelectorAll(".info-box");
  olderFoot.forEach((item) => { if (item !== foot) item.remove(); });
}
async function doForm(form, build, target, event) {
  event.preventDefault();
  const button = form.querySelector('button[type="submit"]');
  try { await submitPreview(target, build(form), button); }
  catch (error) { toast(error.message, true); }
}
function install() {
  $$(".nav-link").forEach((link) =>
    link.addEventListener("click", () => show(link.dataset.view)));
  $$("[data-go]").forEach((button) =>
    button.addEventListener("click", () => show(button.dataset.go)));
  $$("[data-goal]").forEach((button) => button.addEventListener("click", () => {
    show("plans");
    $("#plan-distance").value = button.dataset.goal;
    changePlanDistance();
    invalidatePreview($("#plan-output"));
    $("#plan-goal").focus();
  }));
  $("#open-connection").addEventListener("click", () => connectionDialog(true));
  $("#close-connection").addEventListener("click", () => connectionDialog(false));
  $("#connection-dialog").addEventListener("click", (e) => {
    if (e.target.id === "connection-dialog") connectionDialog(false);
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") connectionDialog(false);
  });
  $("#connect-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const candidate = $("#server-token").value.trim();
    try {
      await api({ action: "overview" }, candidate);
      bearer = candidate;
      await refreshOverview();
      connectionDialog(false);
      $("#server-token").value = "";
      toast("HoodaRun server connected.");
    } catch (error) { $("#connect-error").textContent = error.message; }
  });
  $("#plan-start").value = isoLocal(nextMonday());
  $("#workout-date").value = isoLocal(addDays(new Date(), 1));
  $("#strength-date").value = isoLocal(addDays(new Date(), 1));
  $("#plan-distance").addEventListener("change", changePlanDistance);
  $("#workout-type").addEventListener("change", showWorkoutType);
  $("#plan-form").addEventListener("submit", (e) =>
    doForm(e.target, planRequest, $("#plan-output"), e));
  $("#workout-form").addEventListener("submit", (e) =>
    doForm(e.target, workoutRequest, $("#workout-output"), e));
  $("#strength-form").addEventListener("submit", (e) =>
    doForm(e.target, strengthRequest, $("#strength-output"), e));
  $("#nutrition-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    const button = f.querySelector('button[type="submit"]');
    button.disabled = true;
    try {
      const data = await api({
        action: "nutrition", diet: value(f, "diet"),
        training_load: value(f, "training_load"),
        servings: Number(value(f, "servings")),
        long_run_day: value(f, "long_run_day")
      });
      renderNutrition(data);
      toast("Meal plan and grocery list ready.");
    } catch (error) { toast(error.message, true); }
    finally { button.disabled = false; }
  });
  $("#race-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    try {
      renderRaces(await api({
        action: "races", area: value(f, "area"), distance: value(f, "distance")
      }));
    } catch (error) { toast(error.message, true); }
  });
  $("#refresh-schedule").addEventListener("click", async () => {
    try { await refreshOverview(); toast("Schedule refreshed."); }
    catch (error) { toast(error.message, true); }
  });
  for (const [formId, outputId] of [
    ["plan-form", "plan-output"], ["workout-form", "workout-output"],
    ["strength-form", "strength-output"]
  ]) {
    for (const type of ["input", "change"]) {
      $("#" + formId).addEventListener(type, () => invalidatePreview($("#" + outputId)));
    }
  }
  changePlanDistance();
  showWorkoutType();
  // Local loopback can work anonymously; an authenticated deployment asks
  // for the token only after the first protected API call receives 401.
  refreshOverview().catch(() => {
    if (!connected) connectionDialog(true);
  });
}
document.addEventListener("DOMContentLoaded", install);
