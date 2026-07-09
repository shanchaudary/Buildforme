const BLACK_PATTERNS = [
  "print secret",
  "print secrets",
  "show api key",
  "commit .env",
  "commit env",
  "bypass auth",
  "disable auth",
  "fake success",
  "pretend it works",
  "skip tests and merge",
  "merge without review",
  "production write without approval",
  "delete audit log",
  "hide failing tests",
];

const RED_PATTERNS = [
  "production",
  "deploy",
  "deployment",
  "stripe",
  "payment",
  "charge",
  "capture",
  "refund",
  "database migration",
  "migration",
  "rls",
  "row level security",
  "tenant isolation",
  "auth",
  "session",
  "secret",
  "credential",
  "write-mode ingestion",
  "write mode ingestion",
  "erp credential",
  "s3",
  "email customers",
  "send customer email",
  "legal conclusion",
  "regulatory conclusion",
  "merge to main",
  "auto-merge",
];

const YELLOW_PATTERNS = [
  "fix",
  "implement",
  "route",
  "api",
  "frontend",
  "backend",
  "parser",
  "dashboard",
  "playwright",
  "test coverage",
  "component",
  "workflow",
];

const GREEN_PATTERNS = [
  "read-only",
  "audit",
  "documentation",
  "docs",
  "test-only",
  "tests only",
  "lint",
  "type-only",
  "review",
  "plan",
];

const SENSITIVE_FILE_PATTERNS = [
  ".env",
  "secrets",
  "credentials",
  "private-key",
  "id_rsa",
  "deploy",
  "migration",
  "prisma/migrations",
  "auth",
  "tenant",
  "stripe",
  "payment",
];

const EXAMPLE_TASK = {
  task_id: "BF-0002",
  operating_mode: "IMPLEMENTATION",
  objective: "Fix dashboard response parser and add tests.",
  allowed_files: "src/dashboard/**\ntests/**",
  forbidden_files: ".env\nsecrets/**",
  acceptance_criteria: "Tests pass\nNo secrets exposed\nFinal git status reported",
  data_mutation_allowed: false,
};

const VERDICTS = {
  GREEN: {
    title: "Low risk — agent may run unattended",
    blurb:
      "This looks like read-only, docs, tests, or review work. Let the agent run, then check the final report.",
    next: "Allow the agent to run this task and report status. Still do not auto-merge.",
  },
  YELLOW: {
    title: "Medium risk — branch and PR only",
    blurb:
      "Scoped implementation is fine, but a human should review before merge. Never auto-merge.",
    next: "Let the agent open a branch/PR and run tests. You (or a reviewer) approve before merge.",
  },
  RED: {
    title: "High risk — wait for founder approval",
    blurb:
      "This touches sensitive areas (auth, payments, production, migrations, deploy, or uncertain risk). Do not let the agent proceed alone.",
    next: "Pause automation. Review the plan yourself, then approve only if scope and tests are clear.",
  },
  BLACK: {
    title: "Blocked — rewrite the task",
    blurb:
      "The request matches a hard safety blacklist (secrets, auth bypass, fake success, unsafe merge).",
    next: "Reject this task. Rewrite the objective so it is safe, then classify again.",
  },
};

const PAGE_META = {
  classify: {
    kicker: "Workflow",
    title: "Classify task",
    desc: "Scope the work an AI agent may do. Get a risk decision before it runs.",
  },
  tasks: {
    kicker: "Library",
    title: "Saved tasks",
    desc: "Tasks stored on this machine. Open one to re-classify or record a decision.",
  },
  github: {
    kicker: "Integrations",
    title: "GitHub inspect",
    desc: "Read-only check of pull requests and issues before granting an agent more authority.",
  },
  guide: {
    kicker: "Policy",
    title: "Risk policy",
    desc: "How Buildforme classifies work. Prefer blocking uncertain work over silent approval.",
  },
};

let lastPacket = null;
let lastClassification = null;
let serverOnline = false;

function lines(value) {
  return value
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
}

function packetFromForm() {
  return {
    task_id: document.querySelector("#task_id").value.trim(),
    operating_mode: document.querySelector("#operating_mode").value,
    objective: document.querySelector("#objective").value.trim(),
    allowed_files: lines(document.querySelector("#allowed_files").value),
    forbidden_files: lines(document.querySelector("#forbidden_files").value),
    acceptance_criteria: lines(document.querySelector("#acceptance_criteria").value),
    data_mutation_allowed: document.querySelector("#data_mutation_allowed").checked,
  };
}

function fillForm(task) {
  document.querySelector("#task_id").value = task.task_id || "";
  document.querySelector("#operating_mode").value = task.operating_mode || "IMPLEMENTATION";
  document.querySelector("#objective").value = task.objective || "";
  document.querySelector("#allowed_files").value = Array.isArray(task.allowed_files)
    ? task.allowed_files.join("\n")
    : task.allowed_files || "";
  document.querySelector("#forbidden_files").value = Array.isArray(task.forbidden_files)
    ? task.forbidden_files.join("\n")
    : task.forbidden_files || "";
  document.querySelector("#acceptance_criteria").value = Array.isArray(task.acceptance_criteria)
    ? task.acceptance_criteria.join("\n")
    : task.acceptance_criteria || "";
  document.querySelector("#data_mutation_allowed").checked = Boolean(task.data_mutation_allowed);
}

function textForTask(task) {
  return JSON.stringify(task).toLowerCase();
}

function hits(text, patterns) {
  return patterns.filter((pattern) => text.includes(pattern)).sort();
}

function classifyLocally(task) {
  const text = textForTask(task);
  const blackHits = hits(text, BLACK_PATTERNS);
  if (blackHits.length > 0) {
    return {
      risk: "BLACK",
      auto_run_allowed: false,
      auto_merge_allowed: false,
      required_human_approval: true,
      reasons: blackHits.map((hit) => `Blacklisted unsafe request: ${hit}`),
      required_actions: ["Reject task", "Ask user to rewrite safely"],
    };
  }

  const reasons = [];
  const redHits = hits(text, RED_PATTERNS);
  reasons.push(...redHits.map((hit) => `High-risk term detected: ${hit}`));

  const allowedFiles = task.allowed_files.join("\n").toLowerCase();
  const sensitiveAllowedHits = hits(allowedFiles, SENSITIVE_FILE_PATTERNS);
  reasons.push(...sensitiveAllowedHits.map((hit) => `Sensitive allowed file or area detected: ${hit}`));

  if (task.data_mutation_allowed) {
    reasons.push("Task allows data mutation");
  }

  if (reasons.length > 0) {
    return {
      risk: "RED",
      auto_run_allowed: false,
      auto_merge_allowed: false,
      required_human_approval: true,
      reasons,
      required_actions: [
        "Require Shan approval before execution or merge",
        "Prepare plan and review packet before code changes",
        "Ensure tests cover failure paths and authorization boundaries",
      ],
    };
  }

  const yellowHits = hits(text, YELLOW_PATTERNS);
  if (yellowHits.length > 0) {
    return {
      risk: "YELLOW",
      auto_run_allowed: true,
      auto_merge_allowed: false,
      required_human_approval: true,
      reasons: yellowHits.map((hit) => `Implementation work detected: ${hit}`),
      required_actions: [
        "Create branch or PR only",
        "Run required tests",
        "Send to second-pass review before merge",
      ],
    };
  }

  const greenHits = hits(text, GREEN_PATTERNS);
  if (greenHits.length > 0) {
    return {
      risk: "GREEN",
      auto_run_allowed: true,
      auto_merge_allowed: false,
      required_human_approval: false,
      reasons: greenHits.map((hit) => `Low-risk work detected: ${hit}`),
      required_actions: ["Run scoped checks", "Report final status"],
    };
  }

  return {
    risk: "RED",
    auto_run_allowed: false,
    auto_merge_allowed: false,
    required_human_approval: true,
    reasons: ["Risk uncertain; defaulting to RED"],
    required_actions: ["Ask Shan or reviewer for explicit approval"],
  };
}

async function classifyWithServer(task) {
  const response = await fetch("/api/classify", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(task),
  });
  if (!response.ok) {
    throw new Error(`Server classify failed: HTTP ${response.status}`);
  }
  return response.json();
}

function renderList(selector, values) {
  const node = document.querySelector(selector);
  node.innerHTML = "";
  if (!values || values.length === 0) {
    const item = document.createElement("li");
    item.textContent = "None listed.";
    node.appendChild(item);
    return;
  }
  for (const value of values) {
    const item = document.createElement("li");
    item.textContent = value;
    node.appendChild(item);
  }
}

function renderDecision(task, result, source) {
  lastPacket = task;
  lastClassification = result;

  document.querySelector("#decision-empty").hidden = true;
  document.querySelector("#decision-body").hidden = false;

  const risk = result.risk || "RED";
  const verdict = VERDICTS[risk] || VERDICTS.RED;
  const riskKey = String(risk).toLowerCase();

  const badge = document.querySelector("#risk-badge");
  badge.textContent = risk;
  badge.className = `risk-badge risk-${riskKey}`;

  const banner = document.querySelector("#verdict-banner");
  banner.className = `verdict-banner is-${riskKey}`;

  document.querySelector("#verdict-title").textContent = verdict.title;
  document.querySelector("#verdict-blurb").textContent = verdict.blurb;
  document.querySelector("#next-step").textContent = verdict.next;

  const sourceLabel =
    source === "server"
      ? "Classified by local policy server"
      : source === "server-saved"
        ? "Classified and saved on this machine"
        : "Classified in browser (server offline — fallback)";
  document.querySelector("#decision-source").textContent = sourceLabel;

  document.querySelector("#auto_run").textContent = result.auto_run_allowed ? "Allowed" : "Not allowed";
  document.querySelector("#auto_merge").textContent = result.auto_merge_allowed ? "Allowed" : "Never";
  document.querySelector("#human_approval").textContent = result.required_human_approval
    ? "Required"
    : "Not required";

  const autorunGate = document.querySelector("#gate-autorun");
  const automergeGate = document.querySelector("#gate-automerge");
  const humanGate = document.querySelector("#gate-human");
  for (const el of [autorunGate, automergeGate, humanGate]) {
    el.classList.remove("is-yes", "is-no", "is-warn");
  }
  autorunGate.classList.add(result.auto_run_allowed ? "is-yes" : "is-no");
  automergeGate.classList.add(result.auto_merge_allowed ? "is-yes" : "is-no");
  humanGate.classList.add(result.required_human_approval ? "is-warn" : "is-yes");

  renderList("#reasons", result.reasons);
  renderList("#actions", result.required_actions);

  document.querySelector("#packet").textContent = JSON.stringify(
    { ...task, classification: result, source },
    null,
    2,
  );

  const canRecord = serverOnline && Boolean(task.task_id);
  document.querySelector("#approve-task").hidden = !canRecord;
  document.querySelector("#block-task").hidden = !canRecord;

  if (window.matchMedia("(max-width: 960px)").matches) {
    document.querySelector(".decision-card")?.scrollIntoView({ behavior: "smooth", block: "start" });
  }
}

function showFeedback(selector, message, kind) {
  const node = document.querySelector(selector);
  if (!node) return;
  if (!message) {
    node.hidden = true;
    node.textContent = "";
    return;
  }
  node.hidden = false;
  node.textContent = message;
  node.classList.remove("is-ok", "is-error");
  if (kind) node.classList.add(kind);
}

async function classify() {
  showFeedback("#form-feedback", "");
  const task = packetFromForm();
  if (!task.task_id || !task.objective) {
    showFeedback("#form-feedback", "Add a Task ID and Objective before classifying.", "is-error");
    return;
  }
  if (!task.allowed_files.length || !task.forbidden_files.length || !task.acceptance_criteria.length) {
    showFeedback(
      "#form-feedback",
      "Fill allowed files, forbidden files, and at least one acceptance criterion.",
      "is-error",
    );
    return;
  }

  const classifyBtn = document.querySelector("#classify-btn");
  if (classifyBtn) {
    classifyBtn.disabled = true;
    classifyBtn.setAttribute("aria-busy", "true");
  }

  try {
    const serverResult = await classifyWithServer(task);
    renderDecision(task, serverResult.classification, "server");
    showFeedback("#form-feedback", `Classified as ${serverResult.classification.risk}`, "is-ok");
  } catch (error) {
    renderDecision(task, classifyLocally(task), "browser-fallback");
    showFeedback("#form-feedback", "Server offline — used browser fallback classifier.", "is-error");
  } finally {
    if (classifyBtn) {
      classifyBtn.disabled = false;
      classifyBtn.removeAttribute("aria-busy");
    }
  }
}

async function checkServer() {
  const chip = document.querySelector("#server-chip");
  const node = document.querySelector("#server-status");
  chip.classList.remove("ok", "warn");
  chip.classList.add("checking");
  try {
    const response = await fetch("/api/health");
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    const payload = await response.json();
    serverOnline = true;
    chip.classList.remove("checking");
    chip.classList.add("ok");
    node.textContent = `Online · ${payload.service}`;
    document.querySelector("#save-task").disabled = false;
    await loadTasks();
  } catch (error) {
    serverOnline = false;
    chip.classList.remove("checking");
    chip.classList.add("warn");
    node.textContent = "Offline · run serve";
    document.querySelector("#save-task").disabled = true;
    document.querySelector("#task-count").textContent = "0";
    document.querySelector("#tasks-list").innerHTML =
      `<div class="empty-inline warning">Start the local server to save tasks and inspect GitHub.<br><code>python -m buildforme.cli serve</code></div>`;
  }
}

async function saveTask() {
  showFeedback("#form-feedback", "");
  const task = packetFromForm();
  try {
    const response = await fetch("/api/tasks", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(task),
    });
    if (!response.ok) {
      throw new Error(`Save failed: HTTP ${response.status}`);
    }
    const payload = await response.json();
    renderDecision(task, payload.classification, "server-saved");
    showFeedback("#form-feedback", "Task saved on this machine.", "is-ok");
    await loadTasks();
  } catch (error) {
    showFeedback("#form-feedback", error.message, "is-error");
  }
}

async function recordDecision(status) {
  if (!lastPacket?.task_id) {
    showFeedback("#decision-feedback", "Classify and save a task first.", "is-error");
    return;
  }
  try {
    const saveResponse = await fetch("/api/tasks", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(lastPacket),
    });
    if (!saveResponse.ok) {
      throw new Error(`Could not save task: HTTP ${saveResponse.status}`);
    }

    const response = await fetch("/api/decisions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        task_id: lastPacket.task_id,
        decision: {
          status,
          risk: lastClassification?.risk,
          note: status === "approved" ? "Founder recorded approval" : "Founder recorded block",
          recorded_via: "dashboard",
        },
      }),
    });
    if (!response.ok) {
      throw new Error(`Decision failed: HTTP ${response.status}`);
    }
    showFeedback(
      "#decision-feedback",
      status === "approved" ? "Recorded: approved for this task id." : "Recorded: blocked for this task id.",
      "is-ok",
    );
    await loadTasks();
  } catch (error) {
    showFeedback("#decision-feedback", error.message, "is-error");
  }
}

function taskCard(record) {
  const risk = record.classification?.risk || "UNKNOWN";
  const task = record.task || {};
  const node = document.createElement("button");
  node.type = "button";
  node.className = "task-row";
  node.innerHTML = `
    <div class="task-id">${escapeHtml(task.task_id || "untitled")}</div>
    <div>
      <p class="task-obj">${escapeHtml(task.objective || "No objective")}</p>
      <div class="task-meta">${escapeHtml(record.status || "draft")} · ${escapeHtml(record.updated_at || "unknown")}</div>
    </div>
    <div class="task-risk"><span class="risk-badge risk-${String(risk).toLowerCase()}">${escapeHtml(risk)}</span></div>
  `;
  node.addEventListener("click", () => {
    fillForm(task);
    showView("classify");
    if (record.classification) {
      renderDecision(task, record.classification, "server");
    }
    showFeedback("#form-feedback", `Loaded ${task.task_id || "task"} into the form.`, "is-ok");
  });
  return node;
}

async function loadTasks() {
  const list = document.querySelector("#tasks-list");
  try {
    const response = await fetch("/api/tasks");
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    const payload = await response.json();
    list.innerHTML = "";
    const tasks = payload.tasks || [];
    document.querySelector("#task-count").textContent = String(tasks.length);
    if (tasks.length === 0) {
      list.innerHTML = `<div class="empty-inline">No saved tasks yet. Classify a task, then click <strong>Save task</strong>.</div>`;
      return;
    }
    for (const record of tasks.slice().reverse()) {
      list.appendChild(taskCard(record));
    }
  } catch (error) {
    document.querySelector("#task-count").textContent = "0";
    list.innerHTML = `<div class="empty-inline warning">Task storage unavailable in static mode.</div>`;
  }
}

function renderGithubPr(payload) {
  const summary = document.querySelector("#github-summary");
  const pr = payload.pull_request || payload;
  const files = payload.files || [];
  if (payload.error) {
    summary.hidden = false;
    summary.innerHTML = `<h3>Could not load PR</h3><p class="warning">${escapeHtml(payload.error)}</p>`;
    return;
  }
  const title = pr.title || "Pull request";
  const number = pr.number != null ? `#${pr.number}` : "";
  const state = pr.state || "unknown";
  const url = pr.html_url || pr.url || "";
  const fileItems = files
    .slice(0, 12)
    .map((f) => {
      const name = f.filename || f.path || JSON.stringify(f);
      const status = f.status ? ` (${f.status})` : "";
      return `<li><code>${escapeHtml(name)}</code>${escapeHtml(status)}</li>`;
    })
    .join("");
  summary.hidden = false;
  summary.innerHTML = `
    <h3>${escapeHtml(title)} ${escapeHtml(number)}</h3>
    <p>State: <strong>${escapeHtml(state)}</strong>${
      url ? ` · <a href="${escapeHtml(url)}" target="_blank" rel="noopener">Open on GitHub</a>` : ""
    }</p>
    <p><strong>Changed files</strong> (${files.length})</p>
    <ul>${fileItems || "<li>No file list returned.</li>"}</ul>
  `;
}

function renderGithubIssues(payload) {
  const summary = document.querySelector("#github-summary");
  if (payload.error) {
    summary.hidden = false;
    summary.innerHTML = `<h3>Could not load issues</h3><p class="warning">${escapeHtml(payload.error)}</p>`;
    return;
  }
  const issues = payload.issues || [];
  const items = issues
    .slice(0, 15)
    .map((issue) => {
      const num = issue.number != null ? `#${issue.number}` : "";
      const title = issue.title || "untitled";
      const url = issue.html_url || issue.url || "";
      const link = url
        ? `<a href="${escapeHtml(url)}" target="_blank" rel="noopener">${escapeHtml(num)} ${escapeHtml(title)}</a>`
        : `${escapeHtml(num)} ${escapeHtml(title)}`;
      return `<li>${link}</li>`;
    })
    .join("");
  summary.hidden = false;
  summary.innerHTML = `
    <h3>Open issues (${issues.length})</h3>
    <ul>${items || "<li>No open issues found.</li>"}</ul>
  `;
}

async function inspectPullRequest(event) {
  event.preventDefault();
  const repository = document.querySelector("#github_repository").value.trim();
  const number = document.querySelector("#github_pr_number").value.trim();
  const output = document.querySelector("#github-output");
  const summary = document.querySelector("#github-summary");
  output.textContent = "Loading pull request…";
  summary.hidden = true;
  try {
    const response = await fetch(
      `/api/github/pr?repository=${encodeURIComponent(repository)}&number=${encodeURIComponent(number)}`,
    );
    const payload = await response.json();
    output.textContent = JSON.stringify(payload, null, 2);
    renderGithubPr(payload);
  } catch (error) {
    output.textContent = `GitHub check failed: ${error.message}`;
    summary.hidden = false;
    summary.innerHTML = `<h3>GitHub check failed</h3><p class="warning">${escapeHtml(error.message)}</p>`;
  }
}

async function loadIssues() {
  const repository = document.querySelector("#github_repository").value.trim();
  const output = document.querySelector("#github-output");
  const summary = document.querySelector("#github-summary");
  output.textContent = "Loading issues…";
  summary.hidden = true;
  try {
    const response = await fetch(
      `/api/github/issues?repository=${encodeURIComponent(repository)}&state=open&limit=20`,
    );
    const payload = await response.json();
    output.textContent = JSON.stringify(payload, null, 2);
    renderGithubIssues(payload);
  } catch (error) {
    output.textContent = `GitHub issue check failed: ${error.message}`;
    summary.hidden = false;
    summary.innerHTML = `<h3>GitHub check failed</h3><p class="warning">${escapeHtml(error.message)}</p>`;
  }
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function showView(name) {
  const meta = PAGE_META[name] || PAGE_META.classify;
  document.querySelector("#page-kicker").textContent = meta.kicker;
  document.querySelector("#page-title").textContent = meta.title;
  document.querySelector("#page-desc").textContent = meta.desc;

  document.querySelectorAll(".nav-item").forEach((item) => {
    const active = item.dataset.view === name;
    item.classList.toggle("is-active", active);
    item.setAttribute("aria-selected", active ? "true" : "false");
  });

  document.querySelectorAll("[data-view-panel]").forEach((panel) => {
    const active = panel.dataset.viewPanel === name;
    panel.hidden = !active;
    panel.classList.toggle("is-active", active);
  });

  // Top actions relevant to classify view
  const onClassify = name === "classify";
  document.querySelector("#reset-example").style.display = onClassify ? "" : "none";
  document.querySelector("#save-task").style.display = onClassify ? "" : "none";
  document.querySelector("#classify-btn").style.display = onClassify ? "" : "none";
}

function setupNav() {
  document.querySelectorAll(".nav-item").forEach((item) => {
    item.addEventListener("click", () => showView(item.dataset.view));
  });
}

async function copyPacket() {
  if (!lastPacket || !lastClassification) {
    showFeedback("#decision-feedback", "Classify a task first.", "is-error");
    return;
  }
  const payload = JSON.stringify({ ...lastPacket, classification: lastClassification }, null, 2);
  try {
    await navigator.clipboard.writeText(payload);
    showFeedback("#decision-feedback", "Packet copied — paste it into your agent prompt.", "is-ok");
  } catch (error) {
    showFeedback(
      "#decision-feedback",
      "Could not copy automatically. Expand JSON below and copy manually.",
      "is-error",
    );
  }
}

// —— Events ——
document.querySelector("#task-form").addEventListener("submit", (event) => {
  event.preventDefault();
  classify();
});

document.querySelector("#save-task").addEventListener("click", () => {
  saveTask();
});

document.querySelector("#reset-example").addEventListener("click", () => {
  fillForm(EXAMPLE_TASK);
  document.querySelector("#decision-empty").hidden = false;
  document.querySelector("#decision-body").hidden = true;
  showFeedback("#form-feedback", "Example task restored. Click Classify risk.", "is-ok");
  showFeedback("#decision-feedback", "");
});

document.querySelector("#copy-packet").addEventListener("click", () => {
  copyPacket();
});

document.querySelector("#approve-task").addEventListener("click", () => {
  recordDecision("approved");
});

document.querySelector("#block-task").addEventListener("click", () => {
  recordDecision("blocked");
});

document.querySelector("#github-form").addEventListener("submit", inspectPullRequest);
document.querySelector("#load-issues").addEventListener("click", loadIssues);

setupNav();
showView("classify");
checkServer();
