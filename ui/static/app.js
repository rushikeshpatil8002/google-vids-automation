"use strict";
// Vanilla JS UI. Polls the job every 2 s (DEC-10). Button rules mirror the API's "allowed when" (03 §8, U3).

const $ = (sel) => document.querySelector(sel);
const state = { jobId: null, view: null, health: null, timer: null };

const EDITABLE = ["DRAFT", "VALIDATED"];
const RESUMABLE = ["PAUSED", "WAITING_FOR_USER", "BLOCKED_QUOTA", "FAILED"];
const RETRYABLE = ["COMPLETED_WITH_ERRORS", "PAUSED", "FAILED"];
const CHIP = {
  DOWNLOADED: "done", COMPLETED: "done", READY: "done", VALIDATED: "done",
  SUBMITTED: "running", GENERATING: "running", RUNNING: "running",
  PENDING: "pending", DRAFT: "pending",
  FAILED: "failed", COMPLETED_WITH_ERRORS: "failed",
  BLOCKED_QUOTA: "blocked", WAITING_FOR_USER: "blocked", PAUSED: "blocked",
};
const LABEL = { BLOCKED_QUOTA: "Blocked by quota", WAITING_FOR_USER: "Needs you", COMPLETED_WITH_ERRORS: "Completed with errors" };

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined && v !== false) node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) node.append(c instanceof Node ? c : document.createTextNode(c ?? ""));
  return node;
}
const chip = (status) => el("span", { class: `chip ${CHIP[status] || "pending"}` }, LABEL[status] || status.replaceAll("_", " ").toLowerCase());

async function api(method, url, body, isForm = false) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    if (isForm) opts.body = body;
    else { opts.body = JSON.stringify(body); opts.headers["Content-Type"] = "application/json"; }
  }
  const res = await fetch(url, opts);
  const data = res.headers.get("content-type")?.includes("json") ? await res.json() : null;
  if (!res.ok) throw new Error(data?.error?.message || `${res.status} ${res.statusText}`);
  return data;
}
function toast(message, kind = "error") {
  const b = el("div", { class: `banner ${kind}` }, message);
  $("#banners").prepend(b);
  setTimeout(() => b.remove(), 8000);
}
async function act(fn) {
  try { await fn(); } catch (e) { toast(e.message); }
  await refresh();
}

// ----- header -----------------------------------------------------------------------------------

async function loadHeader() {
  state.health = await api("GET", "/api/health");
  $("#badge-fake").hidden = !state.health.fake_mode;
  $("#badge-lan").hidden = !state.health.lan_mode;
  $("#app-version").textContent = `v${state.health.version}`;
  const b = await api("GET", "/api/browser/status");
  $("#browser-status").textContent = b.fake ? "Browser: simulated" : `Browser: ${b.browser_running ? "running" : "not running"}`;
}
$("#btn-signin").onclick = () => act(async () => {
  const r = await api("POST", "/api/browser/login");
  toast(r.message, "info");
});

// ----- jobs list ---------------------------------------------------------------------------------

async function loadJobs() {
  const jobs = await api("GET", "/api/jobs");
  const list = $("#job-list");
  list.replaceChildren(...jobs.map((j) => el("li", {
    class: j.job_id === state.jobId ? "active" : "",
    onclick: () => { state.jobId = j.job_id; $("#new-job-form").hidden = true; refresh(); },
  }, el("span", {}, j.project_name, el("br"), el("small", { class: "muted" }, `${j.mode} · ${j.counts.done}/${j.counts.total}`)), chip(j.overall_status))));
  $("#empty").hidden = jobs.length > 0 || !$("#new-job-form").hidden || !!state.jobId;
}
$("#btn-new-job").onclick = () => { $("#new-job-form").hidden = false; $("#job-view").hidden = true; $("#empty").hidden = true; };
$("#btn-cancel-new").onclick = () => { $("#new-job-form").hidden = true; refresh(); };
$("#new-job-form").onsubmit = (e) => {
  e.preventDefault();
  const body = Object.fromEntries(new FormData(e.target));
  act(async () => {
    const r = await api("POST", "/api/jobs", body);
    state.jobId = r.job_id; e.target.reset(); e.target.hidden = true;
  });
};
$("#import-file").onchange = (e) => {
  const file = e.target.files[0];
  if (!file) return;
  const form = new FormData(); form.append("manifest", file);
  act(async () => { const r = await api("POST", "/api/jobs/import", form, true); state.jobId = r.job_id; });
  e.target.value = "";
};

// ----- job view -----------------------------------------------------------------------------------

function banners(job) {
  const out = [];
  if (state.health?.lan_mode) out.push(["info", "LAN mode: Chrome, the automation and all files are on the host computer."]);
  const s = job.overall_status;
  if (s === "WAITING_FOR_USER") out.push(["warn", `Action needed: ${job.last_error || "see events"} Then click Resume.`]);
  if (s === "BLOCKED_QUOTA") out.push(["warn", "Google Vids usage limit reached. Completed files are saved; blocked parts stay resumable. The limit can't be bypassed: click Resume after it resets."]);
  if (s === "PAUSED" && job.last_error) out.push(["warn", `Paused: ${job.last_error}`]);
  if (s === "FAILED" && job.last_error) out.push(["error", `Couldn't run: ${job.last_error}`]);
  for (const code of job.limitations || []) out.push(["info", `Limitation (${code.replaceAll("_", " ").toLowerCase()}): see the review plan.`]);
  return out;
}

function renderFields(job) {
  const editable = EDITABLE.includes(job.overall_status);
  const field = (label, name, value, opts = {}) => el("label", {}, label,
    opts.select ? el("select", { name, disabled: !editable, onchange: (e) => patchJob({ [name]: e.target.value }) },
      ...opts.select.map(([v, t]) => el("option", { value: v, selected: v === value }, t)))
      : el("input", { name, value: value ?? "", disabled: !editable, onchange: (e) => patchJob({ [name]: e.target.value }) }));
  $("#job-fields").replaceChildren(
    field("Mode", "mode", job.mode, { select: [["single", "Single video"], ["multi_part", "Multi-part project"], ["separate_clips", "Separate clips"]] }),
    field("Output folder", "output_dir", job.output_dir),
    field("Clip name template", "clip_template", job.clip_template),
    field("Final name template", "final_template", job.final_template),
  );
}
const patchJob = (body) => act(() => api("PATCH", `/api/jobs/${state.jobId}`, body));

function renderParts(view) {
  const editable = EDITABLE.includes(view.job.overall_status);
  const parts = view.parts;
  const move = (i, d) => act(() => {
    const order = parts.map((p) => p.part_id);
    [order[i], order[i + d]] = [order[i + d], order[i]];
    return api("POST", `/api/jobs/${state.jobId}/parts/reorder`, { order });
  });
  $("#parts").replaceChildren(...parts.map((p, i) => el("div", { class: "part-card" },
    el("div", { class: "part-head" },
      el("strong", {}, `Part ${p.sequence}`), p.title ? el("span", {}, `· ${p.title}`) : "",
      el("span", { class: "muted" }, `· script ${p.script_chars} chars · ${p.image_count} image(s)`),
      el("span", { class: "spacer" }),
      el("button", { class: "secondary small", disabled: !editable || i === 0, onclick: () => move(i, -1), title: "Move up" }, "↑"),
      el("button", { class: "secondary small", disabled: !editable || i === parts.length - 1, onclick: () => move(i, 1), title: "Move down" }, "↓"),
      el("button", { class: "danger secondary small", disabled: !editable, onclick: () => act(() => api("DELETE", `/api/jobs/${state.jobId}/parts/${p.part_id}`)) }, "Remove")),
    el("div", { class: "thumbs" }, ...p.images.map((name, n) => el("figure", {},
      el("img", { src: `/api/parts/${p.part_id}/thumb/${n}`, alt: `Part ${p.sequence} image ${n + 1}` }), name))),
    editable ? el("label", {}, "Replace script (.txt)", el("input", { type: "file", accept: ".txt,text/plain",
      onchange: (e) => replace(p, "script_file", e.target.files) })) : "",
    editable ? el("label", {}, "Replace image(s)", el("input", { type: "file", multiple: true, accept: "image/png,image/jpeg,image/webp",
      onchange: (e) => replace(p, "images", e.target.files) })) : "",
    p.validation_errors.length ? el("ul", { class: "errors" }, ...p.validation_errors.map((m) => el("li", {}, m)))
      : (view.job.overall_status === "VALIDATED" ? el("span", { class: "ok" }, "✓ valid") : ""),
  )));
  $("#btn-add-part").disabled = !editable;
  $("#btn-validate").disabled = !editable;
  $("#editor").hidden = !editable && view.job.overall_status !== "DRAFT";
}
function replace(part, field, files) {
  const form = new FormData();
  for (const f of files) form.append(field, f);
  act(() => api("PATCH", `/api/jobs/${state.jobId}/parts/${part.part_id}`, form, true));
}
$("#btn-add-part").onclick = () => { $("#add-part-form").hidden = false; };
$("#btn-cancel-part").onclick = () => { $("#add-part-form").hidden = true; };
$("#add-part-form").onsubmit = (e) => {
  e.preventDefault();
  const src = new FormData(e.target);
  const form = new FormData();
  if (src.get("title")) form.append("title", src.get("title"));
  const file = src.get("script_file");
  if (file && file.size) form.append("script_file", file); else form.append("script_text", src.get("script_text") || "");
  for (const img of src.getAll("images")) if (img.size) form.append("images", img);
  act(async () => { await api("POST", `/api/jobs/${state.jobId}/parts`, form, true); e.target.reset(); e.target.hidden = true; });
};
$("#btn-validate").onclick = () => act(async () => {
  const r = await api("POST", `/api/jobs/${state.jobId}/validate`);
  $("#job-errors").replaceChildren(...r.job_errors.map((m) => el("li", {}, m)));
  if (r.ok) toast("Validation passed. Review the plan, then Start.", "info");
});

async function renderPlan(view) {
  const show = view.job.overall_status === "VALIDATED";
  $("#review").hidden = !show;
  if (!show) return;
  const plan = await api("GET", `/api/jobs/${state.jobId}/plan`);
  $("#plan").replaceChildren(
    el("p", {}, `Mode: ${plan.mode}`, plan.effective_mode !== plan.mode ? ` (runs as ${plan.effective_mode})` : "", ` · ${plan.part_count} part(s) · output: `, el("code", {}, plan.output_dir)),
    ...plan.limitations.map((l) => el("div", { class: "banner warn" }, l.message)),
    el("table", {}, el("thead", {}, el("tr", {}, ...["#", "Title", "Script", "Images", "Output file"].map((h) => el("th", {}, h)))),
      el("tbody", {}, ...plan.parts.map((p) => el("tr", {},
        el("td", {}, p.sequence), el("td", {}, p.title || ""), el("td", {}, `${p.script_file} (${p.script_chars} chars)`),
        el("td", {}, el("div", { class: "thumbs" }, ...p.images.map((n, i) => el("figure", {}, el("img", { src: `/api/parts/${p.part_id}/thumb/${i}`, alt: n }), n)))),
        el("td", {}, el("code", {}, p.output)))))),
    plan.final_output ? el("p", {}, "Combined video: ", el("code", {}, plan.final_output)) : "",
  );
  $("#btn-start").disabled = !!view.busy_job;
}
$("#btn-start").onclick = () => act(() => api("POST", `/api/jobs/${state.jobId}/start`));

function renderProgress(view) {
  const job = view.job, s = job.overall_status;
  const show = !EDITABLE.includes(s);
  $("#progress").hidden = !show;
  if (!show) return;
  const c = view.counts;
  $("#progress-count").textContent = `${c.done} / ${c.total} done`;
  $("#bar-fill").style.width = c.total ? `${(100 * c.done) / c.total}%` : "0";
  const cur = view.current;
  if (cur && cur.step) {
    const part = view.parts.find((p) => p.part_id === cur.part_id);
    const since = cur.part_started_at || cur.started_at;
    const secs = since ? Math.round(Date.now() / 1000 - since) : 0;
    $("#current-step").textContent = `${part ? `Part ${part.sequence} · ` : ""}${cur.step.replaceAll("_", " ").toLowerCase()} · ${secs} s`;
  } else $("#current-step").textContent = "";
  $("#progress-rows").replaceChildren(...view.parts.map((p) => el("tr", {},
    el("td", {}, p.sequence), el("td", {}, p.title || ""), el("td", {}, chip(p.status)), el("td", {}, p.retry_count),
    el("td", {}, p.output_name ? el("code", {}, p.output_name) : (p.status === "READY" && job.effective_mode === "multi_part" ? "in final export" : "")),
    el("td", { class: p.error_message ? "errors" : "" }, p.error_message || ""))));
  const fe = job.final_export_status;
  $("#final-line").textContent = fe && fe !== "NOT_APPLICABLE"
    ? `Combined video: ${fe.replaceAll("_", " ").toLowerCase()}${job.final_output_path ? ` → ${job.final_output_path.split(/[\\/]/).pop()}` : ""}${job.partial_output_path ? ` · partial: ${job.partial_output_path.split(/[\\/]/).pop()}` : ""}` : "";
  const running = s === "RUNNING" || view.busy_job === state.jobId;
  const otherBusy = view.busy_job && view.busy_job !== state.jobId;
  $("#btn-stop").disabled = !running;
  $("#btn-resume").disabled = running || otherBusy || !RESUMABLE.includes(s);
  $("#btn-retry").disabled = running || otherBusy || !RETRYABLE.includes(s) || !view.parts.some((p) => p.status === "FAILED") && job.final_export_status !== "FAILED";
  $("#btn-open").disabled = !!state.health?.lan_mode;
  $("#events").replaceChildren(...view.events.slice(0, 10).map((e) => el("li", { class: e.severity },
    `${e.ts.replace("T", " ").slice(0, 19)} · ${e.step} · ${e.message}`)));
}
$("#btn-stop").onclick = () => act(() => api("POST", `/api/jobs/${state.jobId}/stop`));
$("#btn-resume").onclick = () => act(() => api("POST", `/api/jobs/${state.jobId}/resume`));
$("#btn-retry").onclick = () => act(() => api("POST", `/api/jobs/${state.jobId}/retry-failed`));
$("#btn-open").onclick = () => act(() => api("POST", `/api/jobs/${state.jobId}/open-output`));
$("#btn-report").onclick = () => { window.location = `/api/jobs/${state.jobId}/report?format=md`; };
$("#btn-delete").onclick = () => {
  const outputs = window.confirm("Delete this job's database records, input copies and thumbnails?\n\nOK = continue. You'll be asked separately about the output videos.");
  if (!outputs) return;
  const alsoVideos = window.confirm("Also delete the output videos and reports for this job?\n\nOK = delete videos too · Cancel = keep videos");
  act(async () => { await api("DELETE", `/api/jobs/${state.jobId}?delete_outputs=${alsoVideos}`); state.jobId = null; });
};

async function refresh() {
  try {
    await loadJobs();
    if (!state.jobId) { $("#job-view").hidden = true; return; }
    const view = await api("GET", `/api/jobs/${state.jobId}`);
    state.view = view;
    $("#job-view").hidden = false;
    $("#empty").hidden = true;
    $("#job-title").textContent = view.job.project_name;
    $("#job-status").replaceChildren(chip(view.job.overall_status));
    const bannerNodes = banners(view.job).map(([k, m]) => el("div", { class: `banner ${k}` }, m));
    for (const old of document.querySelectorAll("#banners .banner.sticky")) old.remove();
    for (const b of bannerNodes) { b.classList.add("sticky"); $("#banners").append(b); }
    renderFields(view.job);
    renderParts(view);
    await renderPlan(view);
    renderProgress(view);
  } catch (e) {
    if (e.message.startsWith("404")) state.jobId = null; else console.warn(e);
  }
}

loadHeader().catch(console.warn);
refresh();
state.timer = setInterval(() => {
  const s = state.view?.job?.overall_status;
  // While editing, poll only the list so typing isn't interrupted; poll everything otherwise.
  if (state.jobId && s && !EDITABLE.includes(s)) refresh(); else loadJobs().catch(() => {});
}, 2000);
