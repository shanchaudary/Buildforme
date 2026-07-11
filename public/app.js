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
  queue: {
    kicker: "Supervisor",
    title: "Work queue",
    desc: "Open PRs, issues, CI, risk, and what needs you next — GitHub read-only, local notes only.",
  },
  approvals: {
    kicker: "Supervisor",
    title: "Approvals",
    desc: "Local-only decisions. These are not GitHub reviews and do not authorize merges.",
  },
  packets: {
    kicker: "Handoff",
    title: "Agent packets",
    desc: "Generate tool-neutral instructions for Grok, Codex, Claude, or GLM. No live agent execution.",
  },
  constitution: {
    kicker: "Stage 5.6",
    title: "AI Constitution",
    desc: "Versioned, hashed, leased engineering law. Every provider, packet, and run inherits it. No bypass.",
  },
  execution: {
    kicker: "Stage 6",
    title: "Execution control",
    desc: "Kill switch, locks, multi-provider discovery, dry-run and live supervised worktrees. No merge/deploy.",
  },
  planner: {
    kicker: "Control plane",
    title: "Chief planner",
    desc: "Deterministic next-action ranking from roadmap, truth, CI, and risk. No live agents.",
  },
  projects: {
    kicker: "Control plane",
    title: "Projects",
    desc: "Local project registry, roadmap stages, planned tasks, and project truth.",
  },
  guide: {
    kicker: "Policy",
    title: "Risk policy",
    desc: "How Buildforme classifies work. Prefer blocking uncertain work over silent approval.",
  },
};

let lastGeneratedPacket = null;
let lastPlan = null;

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
    await loadApprovals();
    await loadSavedPackets();
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

  if (name === "queue" && serverOnline) {
    loadWatchedRepos();
    if (!document.querySelector("#queue-pr-list .queue-item")) {
      // auto-load once when entering empty queue
      refreshWorkQueue();
    }
  }
  if (name === "approvals" && serverOnline) {
    loadApprovals();
  }
  if (name === "packets" && serverOnline) {
    loadPacketTaskOptions();
    loadSavedPackets();
    updatePacketSourceUI();
  }
  if (name === "planner" && serverOnline) {
    loadPlannerProjects();
  }
  if (name === "projects" && serverOnline) {
    loadProjectsPage();
  }
  if (name === "execution" && serverOnline) {
    refreshExecutionPage();
  }
  if (name === "constitution" && serverOnline) {
    refreshConstitutionPage();
  }
}

async function refreshConstitutionPage() {
  const lawsEl = document.querySelector("#constitution-laws");
  const acksEl = document.querySelector("#constitution-acks");
  const leasesEl = document.querySelector("#constitution-leases");
  const violEl = document.querySelector("#constitution-violations");
  const runsEl = document.querySelector("#constitution-run-compliance");
  const reminderEl = document.querySelector("#constitution-reminder");
  if (!lawsEl) return;
  try {
    const response = await fetch("/api/constitution");
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    const status = data.status || {};
    document.querySelector("#const-version").textContent = status.version || "—";
    document.querySelector("#const-hash").textContent = status.hash_short || (status.hash || "").slice(0, 12) || "—";
    document.querySelector("#const-hash").title = status.hash || "";
    document.querySelector("#const-laws-count").textContent = String(status.law_count ?? "—");
    document.querySelector("#const-doc-valid").textContent = status.document_valid ? "valid" : "invalid";

    const laws = data.laws || [];
    lawsEl.innerHTML = `
      <table class="data-table">
        <thead><tr><th>ID</th><th>Name</th><th>Severity</th></tr></thead>
        <tbody>
          ${laws
            .map(
              (law) => `<tr>
                <td class="mono">${escapeHtml(law.id || "")}</td>
                <td>${escapeHtml(law.name || "")}</td>
                <td><span class="pill risk-${escapeHtml(String(law.severity || "").toLowerCase())}">${escapeHtml(law.severity || "")}</span></td>
              </tr>`
            )
            .join("")}
        </tbody>
      </table>`;

    const acks = data.provider_acknowledgements || [];
    acksEl.innerHTML = `
      <table class="data-table">
        <thead><tr><th>Provider</th><th>Ack</th><th>Version</th><th>Refresh</th></tr></thead>
        <tbody>
          ${acks
            .map(
              (a) => `<tr>
                <td>${escapeHtml(a.provider_id || "")}</td>
                <td>${a.constitution_acknowledged ? "yes" : "no"}</td>
                <td class="mono">${escapeHtml(a.constitution_version || "—")}</td>
                <td>${a.needs_refresh ? "needed" : "current"}</td>
              </tr>`
            )
            .join("") || `<tr><td colspan="4" class="muted">No providers</td></tr>`}
        </tbody>
      </table>`;

    const leases = data.leases || [];
    leasesEl.innerHTML = `
      <table class="data-table">
        <thead><tr><th>Lease</th><th>Run</th><th>Hash</th></tr></thead>
        <tbody>
          ${leases
            .slice(0, 20)
            .map(
              (l) => `<tr>
                <td class="mono">${escapeHtml(String(l.lease_id || "").slice(0, 14))}</td>
                <td class="mono">${escapeHtml(l.run_id || "—")}</td>
                <td class="mono" title="${escapeHtml(l.constitution_hash || "")}">${escapeHtml(String(l.hash_short || (l.constitution_hash || "").slice(0, 12)))}</td>
              </tr>`
            )
            .join("") || `<tr><td colspan="3" class="muted">No leases yet</td></tr>`}
        </tbody>
      </table>`;

    const violations = data.violations || [];
    violEl.innerHTML = `
      <table class="data-table">
        <thead><tr><th>Law</th><th>Severity</th><th>Evidence</th></tr></thead>
        <tbody>
          ${violations
            .slice(0, 30)
            .map(
              (v) => `<tr>
                <td class="mono">${escapeHtml(v.law_id || "")}</td>
                <td>${escapeHtml(v.severity || "")}</td>
                <td>${escapeHtml(String(v.evidence || "").slice(0, 120))}</td>
              </tr>`
            )
            .join("") || `<tr><td colspan="3" class="muted">No violations recorded</td></tr>`}
        </tbody>
      </table>`;

    const runs = data.run_compliance || [];
    runsEl.innerHTML = `
      <table class="data-table">
        <thead><tr><th>Run</th><th>Status</th><th>Bound</th><th>Compliance</th></tr></thead>
        <tbody>
          ${runs
            .slice(0, 20)
            .map(
              (r) => `<tr>
                <td class="mono">${escapeHtml(String(r.run_id || "").slice(0, 14))}</td>
                <td>${escapeHtml(r.status || "")}</td>
                <td>${r.binding_valid ? "yes" : "no"}</td>
                <td>${escapeHtml((r.compliance && r.compliance.status) || "—")}</td>
              </tr>`
            )
            .join("") || `<tr><td colspan="4" class="muted">No runs</td></tr>`}
        </tbody>
      </table>`;

    reminderEl.textContent = (data.reminder_sample && data.reminder_sample.text) || "";
  } catch (error) {
    lawsEl.innerHTML = `<p class="muted">Failed to load constitution: ${escapeHtml(error.message)}</p>`;
  }
}

function setupNav() {
  document.querySelectorAll(".nav-item").forEach((item) => {
    item.addEventListener("click", () => showView(item.dataset.view));
  });
}

// —— Work queue ——

async function loadWatchedRepos() {
  const row = document.querySelector("#watched-repos");
  if (!row) return;
  try {
    const response = await fetch("/api/repos");
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const payload = await response.json();
    const repos = payload.repositories || [];
    row.innerHTML = "";
    if (repos.length === 0) {
      row.innerHTML = `<span class="muted">No watched repos yet. Add one above.</span>`;
      return;
    }
    for (const repo of repos) {
      const chip = document.createElement("span");
      chip.className = "repo-chip";
      chip.innerHTML = `<span>${escapeHtml(repo)}</span>`;
      const remove = document.createElement("button");
      remove.type = "button";
      remove.setAttribute("aria-label", `Remove ${repo}`);
      remove.textContent = "×";
      remove.addEventListener("click", () => removeWatchedRepo(repo));
      chip.appendChild(remove);
      row.appendChild(chip);
    }
  } catch (error) {
    row.innerHTML = `<span class="warning">Could not load watched repos.</span>`;
  }
}

async function addWatchedRepo(event) {
  event.preventDefault();
  const repository = document.querySelector("#watch_repository").value.trim();
  if (!repository) {
    showFeedback("#queue-feedback", "Enter owner/repo first.", "is-error");
    return;
  }
  try {
    const response = await fetch("/api/repos", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ repository }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    showFeedback("#queue-feedback", `Watching ${repository}`, "is-ok");
    await loadWatchedRepos();
    await refreshWorkQueue();
  } catch (error) {
    showFeedback("#queue-feedback", error.message, "is-error");
  }
}

async function removeWatchedRepo(repository) {
  try {
    const response = await fetch(`/api/repos/${encodeURIComponent(repository)}`, {
      method: "DELETE",
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    showFeedback("#queue-feedback", `Removed ${repository}`, "is-ok");
    await loadWatchedRepos();
    await refreshWorkQueue();
  } catch (error) {
    showFeedback("#queue-feedback", error.message, "is-error");
  }
}

async function refreshWorkQueue() {
  const prList = document.querySelector("#queue-pr-list");
  const issueList = document.querySelector("#queue-issue-list");
  const errorBox = document.querySelector("#queue-errors");
  const errorList = document.querySelector("#queue-error-list");
  if (!prList || !issueList) return;

  prList.innerHTML = `<div class="loading-inline">Loading work queue from GitHub (read-only)…</div>`;
  issueList.innerHTML = `<div class="loading-inline">Loading issues…</div>`;
  showFeedback("#queue-feedback", "");

  try {
    const response = await fetch("/api/work-queue");
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);

    const summary = payload.summary || {};
    setText("#sum-prs", summary.open_prs ?? 0);
    setText("#sum-issues", summary.open_issues ?? 0);
    setText("#sum-ci", summary.ci_failures ?? 0);
    setText("#sum-blocked", summary.blocked ?? 0);
    setText("#sum-ready", summary.ready_for_review ?? 0);
    setText("#sum-safe", summary.safe_next_tasks ?? 0);

    renderRecommended(payload.recommended_next_task);
    renderPrQueue(payload.pull_requests || []);
    renderIssueQueue(payload.issues || []);
    await loadWatchedRepos();

    const errors = payload.errors || [];
    if (errors.length) {
      errorBox.hidden = false;
      errorList.innerHTML = errors
        .map(
          (err) =>
            `<li>${escapeHtml(err.repository || "queue")}: ${escapeHtml(err.error || JSON.stringify(err))}</li>`,
        )
        .join("");
    } else {
      errorBox.hidden = true;
      errorList.innerHTML = "";
    }

    const tokenNote = payload.github_token_configured
      ? "Token configured (never displayed)."
      : "No token — public repos only; rate limits apply.";
    showFeedback("#queue-feedback", `Queue refreshed. ${tokenNote}`, "is-ok");
  } catch (error) {
    prList.innerHTML = `<div class="empty-inline warning">${escapeHtml(error.message)}</div>`;
    issueList.innerHTML = `<div class="empty-inline warning">Queue failed to load.</div>`;
    showFeedback("#queue-feedback", error.message, "is-error");
  }
}

function setText(selector, value) {
  const node = document.querySelector(selector);
  if (node) node.textContent = String(value);
}

function renderRecommended(item) {
  if (!item) return;
  setText("#recommend-headline", item.headline || "No recommendation");
  setText("#recommend-detail", item.detail || "");
  const action = item.recommended_action
    ? item.recommended_action
    : item.title
      ? `${item.repository || ""} #${item.number || ""} · ${item.title}`
      : "";
  setText("#recommend-action", action);
}

function ciClass(status) {
  const value = String(status || "unknown").toLowerCase();
  if (value === "passing") return "ci-passing";
  if (value === "failing") return "ci-failing";
  if (value === "pending") return "ci-pending";
  return "ci-unknown";
}

function riskBadgeHtml(risk) {
  const value = String(risk || "UNKNOWN").toUpperCase();
  return `<span class="risk-badge risk-${value.toLowerCase()}">${escapeHtml(value)}</span>`;
}

function renderPrQueue(items) {
  const list = document.querySelector("#queue-pr-list");
  list.innerHTML = "";
  if (!items.length) {
    list.innerHTML = `<div class="empty-inline">No open pull requests in watched repositories.</div>`;
    return;
  }
  for (const item of items) {
    list.appendChild(renderQueueCard(item, "pull_request"));
  }
}

function renderIssueQueue(items) {
  const list = document.querySelector("#queue-issue-list");
  list.innerHTML = "";
  if (!items.length) {
    list.innerHTML = `<div class="empty-inline">No open issues (excluding PRs) in watched repositories.</div>`;
    return;
  }
  for (const item of items) {
    list.appendChild(renderQueueCard(item, "issue"));
  }
}

function renderQueueCard(item, targetType) {
  const risk = (item.classification || {}).risk || "UNKNOWN";
  const ci = (item.ci || {}).status || "unknown";
  const local = item.local_approval;
  const files = (item.files || []).slice(0, 8);
  const card = document.createElement("article");
  card.className = "queue-item";

  const titleLink = item.html_url
    ? `<a href="${escapeHtml(item.html_url)}" target="_blank" rel="noopener">#${escapeHtml(String(item.number))} ${escapeHtml(item.title || "")}</a>`
    : `#${escapeHtml(String(item.number))} ${escapeHtml(item.title || "")}`;

  const metaBits = [
    `<span>${escapeHtml(item.repository || "")}</span>`,
    `<span>${escapeHtml(item.state || "open")}</span>`,
  ];
  if (targetType === "pull_request") {
    metaBits.push(`<span>draft: ${item.draft ? "yes" : "no"}</span>`);
    if (item.mergeable != null) metaBits.push(`<span>mergeable: ${item.mergeable ? "yes" : "no"}</span>`);
    metaBits.push(`<span>files: ${escapeHtml(String(item.changed_files_count ?? files.length))}</span>`);
    if (item.additions != null) metaBits.push(`<span>+${escapeHtml(String(item.additions))} / −${escapeHtml(String(item.deletions ?? 0))}</span>`);
    metaBits.push(`<span class="${ciClass(ci)}">CI: ${escapeHtml(ci)}</span>`);
  } else {
    const labels = (item.labels || []).join(", ") || "no labels";
    metaBits.push(`<span>${escapeHtml(labels)}</span>`);
    if (item.updated_at) metaBits.push(`<span>updated ${escapeHtml(item.updated_at)}</span>`);
  }
  if (local?.decision) {
    metaBits.push(`<span>local: ${escapeHtml(local.decision)}</span>`);
  }

  card.innerHTML = `
    <div class="queue-item-head">
      <h3 class="queue-item-title">${titleLink}</h3>
      ${riskBadgeHtml(risk)}
    </div>
    <div class="queue-meta">${metaBits.join("")}</div>
    <p class="queue-action"><strong>Action:</strong> ${escapeHtml(item.recommended_action || "—")}</p>
    ${
      files.length
        ? `<p class="queue-files">${files.map((f) => escapeHtml(f)).join(" · ")}${
            (item.files || []).length > files.length ? " · …" : ""
          }</p>`
        : ""
    }
    <div class="queue-actions">
      <input class="queue-note-input" type="text" placeholder="Local note (optional)" data-note />
      <button type="button" class="btn btn-secondary btn-sm" data-decision="reviewed">Mark locally reviewed</button>
      <button type="button" class="btn btn-secondary btn-sm" data-decision="ready_for_shan">Ready for Shan</button>
      <button type="button" class="btn btn-danger btn-sm" data-decision="blocked">Mark blocked</button>
      <button type="button" class="btn btn-ghost btn-sm" data-load-classify>Open in Classify</button>
    </div>
    <p class="toast" hidden data-row-feedback></p>
  `;

  card.querySelectorAll("[data-decision]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const note = card.querySelector("[data-note]").value;
      const feedback = card.querySelector("[data-row-feedback]");
      try {
        await postLocalApproval({
          target_type: targetType,
          repository: item.repository,
          number: item.number,
          decision: btn.getAttribute("data-decision"),
          note,
        });
        feedback.hidden = false;
        feedback.className = "toast is-ok";
        feedback.textContent = "Saved locally (not a GitHub approval).";
        await loadApprovals();
      } catch (error) {
        feedback.hidden = false;
        feedback.className = "toast is-error";
        feedback.textContent = error.message;
      }
    });
  });

  card.querySelector("[data-load-classify]")?.addEventListener("click", () => {
    loadItemIntoClassify(item, targetType);
  });

  return card;
}

function loadItemIntoClassify(item, targetType) {
  const files = item.files || [];
  const objective =
    targetType === "pull_request"
      ? `Review PR #${item.number}: ${item.title || ""}`
      : `Work issue #${item.number}: ${item.title || ""}`;
  fillForm({
    task_id: `GH-${targetType === "pull_request" ? "PR" : "ISSUE"}-${item.number}`,
    operating_mode: targetType === "pull_request" ? "REVIEW" : "IMPLEMENTATION",
    objective,
    allowed_files: files.length ? files.join("\n") : "docs/**\n",
    forbidden_files: ".env\nsecrets/**",
    acceptance_criteria: "Tests/CI considered\nNo secrets exposed\nFinal status reported",
    data_mutation_allowed: false,
  });
  showView("classify");
  showFeedback("#form-feedback", "Loaded from work queue — classify before giving an agent more authority.", "is-ok");
}

async function postLocalApproval(payload) {
  const response = await fetch("/api/approvals", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.error || `HTTP ${response.status}`);
  }
  return data;
}

async function loadApprovals() {
  const list = document.querySelector("#approvals-list");
  const badge = document.querySelector("#approval-count");
  if (!list) return;
  try {
    const response = await fetch("/api/approvals");
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const payload = await response.json();
    const approvals = (payload.approvals || []).slice().reverse();
    if (badge) badge.textContent = String(approvals.length);
    list.innerHTML = "";
    if (!approvals.length) {
      list.innerHTML = `<div class="empty-inline">No local approvals yet. Use Work queue actions to record one.</div>`;
      return;
    }
    for (const item of approvals) {
      const row = document.createElement("article");
      row.className = "approval-row";
      const target =
        item.target_type === "task"
          ? "task"
          : `${item.repository || "?"} #${item.number ?? "?"}`;
      row.innerHTML = `
        <div class="approval-decision">${escapeHtml(item.decision || "")}</div>
        <div>
          <p class="approval-title">${escapeHtml(String(item.target_type || ""))} · ${escapeHtml(target)}</p>
          <div class="approval-meta">${escapeHtml(item.note || "No note")} · ${escapeHtml(item.updated_at || item.created_at || "")}</div>
          <div class="approval-meta">Local only — not a GitHub review or merge approval</div>
        </div>
        <span class="chip">local</span>
      `;
      list.appendChild(row);
    }
  } catch (error) {
    list.innerHTML = `<div class="empty-inline warning">${escapeHtml(error.message)}</div>`;
  }
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

document.querySelector("#watch-repo-form")?.addEventListener("submit", addWatchedRepo);
document.querySelector("#refresh-queue")?.addEventListener("click", () => {
  refreshWorkQueue();
});
document.querySelector("#refresh-approvals")?.addEventListener("click", () => {
  loadApprovals();
});

// —— Agent packets ——
function linesFrom(value) {
  return String(value || "")
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
}

function updatePacketSourceUI() {
  const source = document.querySelector("#packet_source_type")?.value || "manual";
  const taskBlock = document.querySelector("#packet-source-task");
  const prBlock = document.querySelector("#packet-source-pr");
  const issueBlock = document.querySelector("#packet-source-issue");
  if (taskBlock) taskBlock.hidden = source !== "task";
  if (prBlock) prBlock.hidden = source !== "pull_request";
  if (issueBlock) issueBlock.hidden = source !== "issue";
}

function packetFormPayload() {
  const source_type = document.querySelector("#packet_source_type").value;
  const payload = {
    source_type,
    title: document.querySelector("#packet_title").value.trim(),
    target_repository: document.querySelector("#packet_repo").value.trim(),
    target_branch: document.querySelector("#packet_branch").value.trim(),
    operating_mode: document.querySelector("#packet_mode").value,
    objective: document.querySelector("#packet_objective").value.trim(),
    context: document.querySelector("#packet_context").value.trim(),
    allowed_files: linesFrom(document.querySelector("#packet_allowed").value),
    forbidden_files: linesFrom(document.querySelector("#packet_forbidden").value),
    acceptance_criteria: linesFrom(document.querySelector("#packet_acceptance").value),
    required_tests: linesFrom(document.querySelector("#packet_tests").value),
    manual_proof: linesFrom(document.querySelector("#packet_proof").value),
  };
  return payload;
}

function applyPacketToForm(packet) {
  if (!packet) return;
  document.querySelector("#packet_title").value = packet.title || "";
  document.querySelector("#packet_repo").value = packet.target_repository || "shanchaudary/Buildforme";
  document.querySelector("#packet_branch").value = packet.target_branch || "main";
  document.querySelector("#packet_mode").value = packet.operating_mode || "IMPLEMENTATION";
  document.querySelector("#packet_objective").value = packet.objective || "";
  document.querySelector("#packet_context").value = packet.context || "";
  document.querySelector("#packet_allowed").value = (packet.allowed_files || []).join("\n");
  document.querySelector("#packet_forbidden").value = (packet.forbidden_files || []).join("\n");
  document.querySelector("#packet_acceptance").value = (packet.acceptance_criteria || []).join("\n");
  if (packet.required_tests) {
    document.querySelector("#packet_tests").value = (packet.required_tests || []).join("\n");
  }
  if (packet.manual_proof) {
    document.querySelector("#packet_proof").value = (packet.manual_proof || []).join("\n");
  }
}

function showGeneratedPacket(packet) {
  lastGeneratedPacket = packet;
  document.querySelector("#packet-empty").hidden = true;
  document.querySelector("#packet-body").hidden = false;
  document.querySelector("#packet-markdown").textContent = packet.markdown || "";
  const risk = packet.risk || "UNKNOWN";
  const chip = document.querySelector("#packet-risk-chip");
  chip.textContent = risk;
  chip.className = `risk-badge risk-${String(risk).toLowerCase()}`;
}

async function generatePacket(event) {
  event?.preventDefault?.();
  showFeedback("#packet-form-feedback", "");
  const payload = packetFormPayload();
  if (!payload.objective) {
    showFeedback("#packet-form-feedback", "Objective is required.", "is-error");
    return;
  }
  try {
    const response = await fetch("/api/packets/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    showGeneratedPacket(data.packet);
    showFeedback("#packet-form-feedback", `Generated · risk ${data.packet.risk}`, "is-ok");
  } catch (error) {
    showFeedback("#packet-form-feedback", error.message, "is-error");
  }
}

async function loadPacketTaskOptions() {
  const select = document.querySelector("#packet_task_select");
  if (!select) return;
  try {
    const response = await fetch("/api/tasks");
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const payload = await response.json();
    const tasks = payload.tasks || [];
    select.innerHTML = "";
    if (!tasks.length) {
      select.innerHTML = `<option value="">No saved tasks</option>`;
      return;
    }
    select.innerHTML = `<option value="">Select a saved task…</option>`;
    for (const record of tasks.slice().reverse()) {
      const task = record.task || {};
      const opt = document.createElement("option");
      opt.value = task.task_id || "";
      opt.textContent = `${task.task_id || "?"} · ${(task.objective || "").slice(0, 60)}`;
      opt.dataset.record = JSON.stringify(record);
      select.appendChild(opt);
    }
  } catch (error) {
    select.innerHTML = `<option value="">Could not load tasks</option>`;
  }
}

async function importSavedTask() {
  const select = document.querySelector("#packet_task_select");
  const option = select?.selectedOptions?.[0];
  if (!option?.dataset?.record) {
    showFeedback("#packet-form-feedback", "Select a saved task first.", "is-error");
    return;
  }
  try {
    const record = JSON.parse(option.dataset.record);
    const response = await fetch("/api/packets/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source_type: "task", task: record }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    applyPacketToForm(data.packet);
    document.querySelector("#packet_source_type").value = "task";
    updatePacketSourceUI();
    showGeneratedPacket(data.packet);
    showFeedback("#packet-form-feedback", "Imported saved task into packet.", "is-ok");
  } catch (error) {
    showFeedback("#packet-form-feedback", error.message, "is-error");
  }
}

async function importPrForPacket() {
  const repository = document.querySelector("#packet_pr_repo").value.trim();
  const number = document.querySelector("#packet_pr_number").value.trim();
  showFeedback("#packet-form-feedback", "Fetching PR (read-only)…", "is-ok");
  try {
    const response = await fetch("/api/packets/from-pr", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ repository, number: Number(number) }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    applyPacketToForm(data.packet);
    document.querySelector("#packet_source_type").value = "pull_request";
    updatePacketSourceUI();
    showGeneratedPacket(data.packet);
    showFeedback("#packet-form-feedback", `Imported PR #${number}.`, "is-ok");
  } catch (error) {
    showFeedback("#packet-form-feedback", error.message, "is-error");
  }
}

async function importIssueForPacket() {
  const repository = document.querySelector("#packet_issue_repo").value.trim();
  const number = document.querySelector("#packet_issue_number").value.trim();
  showFeedback("#packet-form-feedback", "Fetching issue (read-only)…", "is-ok");
  try {
    const response = await fetch("/api/packets/from-issue", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ repository, number: Number(number) }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    applyPacketToForm(data.packet);
    document.querySelector("#packet_source_type").value = "issue";
    updatePacketSourceUI();
    showGeneratedPacket(data.packet);
    showFeedback("#packet-form-feedback", `Imported issue #${number}.`, "is-ok");
  } catch (error) {
    showFeedback("#packet-form-feedback", error.message, "is-error");
  }
}

async function copyGeneratedPacket() {
  if (!lastGeneratedPacket?.markdown) {
    showFeedback("#packet-action-feedback", "Generate a packet first.", "is-error");
    return;
  }
  try {
    await navigator.clipboard.writeText(lastGeneratedPacket.markdown);
    showFeedback("#packet-action-feedback", "Packet copied to clipboard.", "is-ok");
  } catch (error) {
    // Fallback
    const pre = document.querySelector("#packet-markdown");
    const range = document.createRange();
    range.selectNodeContents(pre);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
    showFeedback("#packet-action-feedback", "Clipboard blocked — packet text selected for manual copy.", "is-error");
  }
}

async function saveGeneratedPacket() {
  if (!lastGeneratedPacket) {
    showFeedback("#packet-action-feedback", "Generate a packet first.", "is-error");
    return;
  }
  try {
    const response = await fetch("/api/packets", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ packet: lastGeneratedPacket }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    lastGeneratedPacket = data.packet;
    showFeedback("#packet-action-feedback", "Saved locally (runtime/packets.json).", "is-ok");
    await loadSavedPackets();
  } catch (error) {
    showFeedback("#packet-action-feedback", error.message, "is-error");
  }
}

function downloadGeneratedPacket() {
  if (!lastGeneratedPacket?.markdown) {
    showFeedback("#packet-action-feedback", "Generate a packet first.", "is-error");
    return;
  }
  const blob = new Blob([lastGeneratedPacket.markdown], { type: "text/markdown;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  const safe = String(lastGeneratedPacket.title || lastGeneratedPacket.id || "packet")
    .replace(/[^\w\-]+/g, "_")
    .slice(0, 60);
  a.href = url;
  a.download = `${safe || "agent-packet"}.md`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
  showFeedback("#packet-action-feedback", "Download started.", "is-ok");
}

function resetPacketForm() {
  document.querySelector("#packet_source_type").value = "manual";
  document.querySelector("#packet_title").value = "Scoped agent handoff";
  document.querySelector("#packet_repo").value = "shanchaudary/Buildforme";
  document.querySelector("#packet_branch").value = "main";
  document.querySelector("#packet_mode").value = "READ_ONLY_AUDIT";
  document.querySelector("#packet_objective").value = "Read-only audit of open documentation and report risks.";
  document.querySelector("#packet_context").value = "Generated for external agent handoff. No production authority.";
  document.querySelector("#packet_allowed").value = "docs/**\ntests/**";
  document.querySelector("#packet_forbidden").value = ".env\nsecrets/**\ncredentials/**";
  document.querySelector("#packet_acceptance").value = "Objective complete\nNo secrets exposed\nFinal report filled";
  document.querySelector("#packet_tests").value = "";
  document.querySelector("#packet_proof").value = "";
  lastGeneratedPacket = null;
  document.querySelector("#packet-empty").hidden = false;
  document.querySelector("#packet-body").hidden = true;
  updatePacketSourceUI();
  showFeedback("#packet-form-feedback", "Form reset.", "is-ok");
  showFeedback("#packet-action-feedback", "");
}

async function loadSavedPackets() {
  const list = document.querySelector("#saved-packets-list");
  const badge = document.querySelector("#packet-count");
  if (!list) return;
  try {
    const response = await fetch("/api/packets");
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const payload = await response.json();
    const packets = (payload.packets || []).slice().reverse();
    if (badge) badge.textContent = String(packets.length);
    list.innerHTML = "";
    if (!packets.length) {
      list.innerHTML = `<div class="empty-inline">No saved packets yet. Generate and save one above.</div>`;
      return;
    }
    for (const packet of packets) {
      const row = document.createElement("article");
      row.className = "queue-item";
      const risk = packet.risk || "UNKNOWN";
      row.innerHTML = `
        <div class="queue-item-head">
          <h3 class="queue-item-title">${escapeHtml(packet.title || packet.id || "packet")}</h3>
          <span class="risk-badge risk-${String(risk).toLowerCase()}">${escapeHtml(risk)}</span>
        </div>
        <div class="queue-meta">
          <span>${escapeHtml(packet.source_type || "manual")}</span>
          <span>${escapeHtml(packet.target_repository || "")}</span>
          <span>${escapeHtml(packet.created_at || packet.updated_at || "")}</span>
        </div>
        <div class="queue-actions">
          <button type="button" class="btn btn-secondary btn-sm" data-view-packet>View</button>
          <button type="button" class="btn btn-secondary btn-sm" data-copy-packet>Copy</button>
          <button type="button" class="btn btn-danger btn-sm" data-delete-packet>Delete</button>
        </div>
      `;
      row.querySelector("[data-view-packet]").addEventListener("click", () => {
        applyPacketToForm(packet);
        showGeneratedPacket(packet);
        showFeedback("#packet-form-feedback", "Loaded saved packet.", "is-ok");
      });
      row.querySelector("[data-copy-packet]").addEventListener("click", async () => {
        try {
          await navigator.clipboard.writeText(packet.markdown || "");
          showFeedback("#packet-form-feedback", "Saved packet copied.", "is-ok");
        } catch (error) {
          showFeedback("#packet-form-feedback", "Could not copy automatically.", "is-error");
        }
      });
      row.querySelector("[data-delete-packet]").addEventListener("click", async () => {
        try {
          const response = await fetch(`/api/packets/${encodeURIComponent(packet.id)}`, { method: "DELETE" });
          const data = await response.json();
          if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
          await loadSavedPackets();
          showFeedback("#packet-form-feedback", "Packet deleted locally.", "is-ok");
        } catch (error) {
          showFeedback("#packet-form-feedback", error.message, "is-error");
        }
      });
      list.appendChild(row);
    }
  } catch (error) {
    list.innerHTML = `<div class="empty-inline warning">${escapeHtml(error.message)}</div>`;
  }
}

document.querySelector("#packet-form")?.addEventListener("submit", generatePacket);
document.querySelector("#packet_source_type")?.addEventListener("change", updatePacketSourceUI);
document.querySelector("#import-saved-task")?.addEventListener("click", importSavedTask);
document.querySelector("#import-pr")?.addEventListener("click", importPrForPacket);
document.querySelector("#import-issue")?.addEventListener("click", importIssueForPacket);
document.querySelector("#copy-packet-md")?.addEventListener("click", copyGeneratedPacket);
document.querySelector("#save-packet")?.addEventListener("click", saveGeneratedPacket);
document.querySelector("#download-packet")?.addEventListener("click", downloadGeneratedPacket);
document.querySelector("#packet-reset")?.addEventListener("click", resetPacketForm);
document.querySelector("#refresh-packets")?.addEventListener("click", loadSavedPackets);

// —— Stage 4 planner / projects ——
async function loadPlannerProjects() {
  const select = document.querySelector("#planner_project");
  const roadmapSelect = document.querySelector("#roadmap_project");
  try {
    const response = await fetch("/api/projects");
    const data = await response.json();
    const projects = (data.projects || []).filter((p) => p.status !== "archived");
    const fill = (el) => {
      if (!el) return;
      const prev = el.value;
      el.innerHTML = "";
      if (!projects.length) {
        el.innerHTML = `<option value="">No projects — load sample</option>`;
        return;
      }
      for (const p of projects) {
        const opt = document.createElement("option");
        opt.value = p.id;
        opt.textContent = `${p.name} (${p.id})`;
        el.appendChild(opt);
      }
      if (prev && projects.some((p) => p.id === prev)) el.value = prev;
    };
    fill(select);
    fill(roadmapSelect);
  } catch (error) {
    if (select) select.innerHTML = `<option value="">Failed to load</option>`;
  }
}

async function loadSampleProject(feedbackSelector) {
  try {
    const response = await fetch("/api/projects/sample", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ replace: true }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    showFeedback(feedbackSelector, `Sample project loaded: ${data.project.id}`, "is-ok");
    await loadPlannerProjects();
    await loadProjectsPage();
    const sel = document.querySelector("#planner_project");
    if (sel) sel.value = data.project.id;
    await refreshPlan();
  } catch (error) {
    showFeedback(feedbackSelector, error.message, "is-error");
  }
}

async function refreshPlan() {
  const projectId = document.querySelector("#planner_project")?.value;
  if (!projectId) {
    showFeedback("#planner-feedback", "Select or load a project first.", "is-error");
    return;
  }
  showFeedback("#planner-feedback", "Planning…", "is-ok");
  try {
    const response = await fetch(`/api/projects/${encodeURIComponent(projectId)}/plan/refresh`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    lastPlan = data.plan;
    renderPlan(data.plan);
    showFeedback("#planner-feedback", `Plan ready · confidence ${data.plan.confidence}`, "is-ok");
  } catch (error) {
    showFeedback("#planner-feedback", error.message, "is-error");
  }
}

function renderPlan(plan) {
  const summary = plan.summary || {};
  setText("#pl-stage", summary.active_stage_name || summary.active_stage_id || "—");
  setText("#pl-ready", summary.ready_tasks ?? "—");
  setText("#pl-blocked", summary.blocked_tasks ?? "—");
  setText("#pl-prs", summary.open_prs ?? "—");
  setText("#pl-ci", summary.failing_ci ?? "—");
  setText("#pl-shan", summary.needs_shan ?? "—");

  const primary = plan.primary_recommendation || {};
  setText("#pl-headline", primary.headline || "No recommendation");
  setText(
    "#pl-detail",
    `${primary.recommendation_type || ""} · risk ${primary.risk || "—"} · score ${primary.total_score ?? "—"} · Shan: ${
      primary.requires_shan ? "YES" : "no"
    }`,
  );
  setText(
    "#pl-meta",
    (primary.explanation || primary.reasoning || []).slice(0, 2).join(" · ") || plan.disclaimer || "",
  );
  const explain = document.querySelector("#pl-explain");
  if (explain) {
    explain.innerHTML = "";
    for (const line of primary.explanation || primary.reasoning || []) {
      const li = document.createElement("li");
      li.textContent = line;
      explain.appendChild(li);
    }
  }
  const genBtn = document.querySelector("#pl-generate-packet");
  if (genBtn) {
    genBtn.hidden = !primary.can_generate_packet;
    genBtn.onclick = () => generatePacketFromRecommendation(plan.project_id, primary.target_id);
  }

  const ranked = document.querySelector("#planner-ranked");
  ranked.innerHTML = "";
  const list = plan.ranked_recommendations || [];
  if (!list.length) {
    ranked.innerHTML = `<div class="empty-inline">No ranked candidates.</div>`;
  } else {
    for (const rec of list) {
      const card = document.createElement("article");
      card.className = "queue-item";
      const risk = rec.risk || "UNKNOWN";
      const bd = rec.score_breakdown || {};
      card.innerHTML = `
        <div class="queue-item-head">
          <h3 class="queue-item-title">#${escapeHtml(String(rec.rank || ""))} ${escapeHtml(rec.headline || "")}</h3>
          <span class="risk-badge risk-${String(risk).toLowerCase()}">${escapeHtml(risk)}</span>
        </div>
        <div class="queue-meta">
          <span>score ${escapeHtml(String(rec.total_score ?? ""))}</span>
          <span>${escapeHtml(rec.recommendation_type || "")}</span>
          <span>${escapeHtml(rec.stage_name || rec.stage_id || "")}</span>
          <span>${rec.requires_shan ? "Needs Shan" : "Agent-eligible"}</span>
        </div>
        <p class="queue-action">${escapeHtml((rec.reasoning || []).join(" · "))}</p>
        <p class="queue-files">blocker=${bd.blocker_impact} stage=${bd.stage_alignment} risk=${bd.risk_suitability} deps=${bd.dependency_readiness} ci=${bd.ci_urgency}</p>
        <div class="queue-actions">
          ${
            rec.can_generate_packet
              ? `<button type="button" class="btn btn-secondary btn-sm" data-pkt>Generate packet</button>`
              : ""
          }
          ${
            rec.html_url
              ? `<a class="btn btn-ghost btn-sm" href="${escapeHtml(rec.html_url)}" target="_blank" rel="noopener">Open source</a>`
              : ""
          }
        </div>
      `;
      card.querySelector("[data-pkt]")?.addEventListener("click", () => {
        generatePacketFromRecommendation(plan.project_id, rec.target_id);
      });
      ranked.appendChild(card);
    }
  }

  const blockers = document.querySelector("#planner-blockers");
  blockers.innerHTML = "";
  const bl = plan.blockers || [];
  if (!bl.length) {
    blockers.innerHTML = `<div class="empty-inline">No blockers detected.</div>`;
  } else {
    for (const b of bl) {
      const card = document.createElement("article");
      card.className = "queue-item";
      card.innerHTML = `
        <div class="queue-item-head">
          <h3 class="queue-item-title">${escapeHtml(b.blocker || "")}</h3>
          <span class="chip">${escapeHtml(b.severity || "")}</span>
        </div>
        <p class="queue-action">Blocks: ${escapeHtml(b.what_it_blocks || "")}</p>
        <p class="queue-action">Resolution: ${escapeHtml(b.recommended_resolution || "")}</p>
        <div class="queue-meta"><span>${b.requires_shan ? "Needs Shan" : "No Shan required"}</span></div>
      `;
      blockers.appendChild(card);
    }
  }
}

async function generatePacketFromRecommendation(projectId, targetId) {
  try {
    const response = await fetch(
      `/api/projects/${encodeURIComponent(projectId)}/recommendation/${encodeURIComponent(targetId)}/packet`,
      { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" },
    );
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    showGeneratedPacket(data.packet);
    showView("packets");
    showFeedback("#packet-form-feedback", "Packet generated from planner recommendation.", "is-ok");
  } catch (error) {
    showFeedback("#planner-feedback", error.message, "is-error");
  }
}

async function generateBriefing() {
  try {
    const response = await fetch("/api/briefing/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    document.querySelector("#planner-briefing").textContent = JSON.stringify(data.briefing, null, 2);
    showFeedback("#planner-feedback", "Briefing generated.", "is-ok");
  } catch (error) {
    showFeedback("#planner-feedback", error.message, "is-error");
  }
}

async function loadProjectsPage() {
  await loadPlannerProjects();
  try {
    const response = await fetch("/api/projects");
    const data = await response.json();
    const list = document.querySelector("#projects-list");
    list.innerHTML = "";
    for (const p of data.projects || []) {
      const card = document.createElement("article");
      card.className = "queue-item";
      card.innerHTML = `
        <div class="queue-item-head">
          <h3 class="queue-item-title">${escapeHtml(p.name)} <span class="muted">(${escapeHtml(p.id)})</span></h3>
          <span class="chip">${escapeHtml(p.status)}</span>
        </div>
        <p class="queue-action">${escapeHtml(p.repository || "")} · ${escapeHtml(p.objective || "")}</p>
        <div class="queue-actions">
          <button type="button" class="btn btn-secondary btn-sm" data-open>Open in planner</button>
          <button type="button" class="btn btn-ghost btn-sm" data-pause>${p.status === "paused" ? "Resume" : "Pause"}</button>
          <button type="button" class="btn btn-danger btn-sm" data-archive>Archive</button>
        </div>
      `;
      card.querySelector("[data-open]").addEventListener("click", async () => {
        showView("planner");
        document.querySelector("#planner_project").value = p.id;
        await refreshPlan();
      });
      card.querySelector("[data-pause]").addEventListener("click", async () => {
        const status = p.status === "paused" ? "active" : "paused";
        await fetch(`/api/projects/${encodeURIComponent(p.id)}`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ id: p.id, status }),
        });
        await loadProjectsPage();
      });
      card.querySelector("[data-archive]").addEventListener("click", async () => {
        await fetch(`/api/projects/${encodeURIComponent(p.id)}`, { method: "DELETE" });
        await loadProjectsPage();
      });
      list.appendChild(card);
    }
    const projectId = document.querySelector("#roadmap_project")?.value;
    if (projectId) await loadRoadmap(projectId);
  } catch (error) {
    showFeedback("#projects-feedback", error.message, "is-error");
  }
}

async function loadRoadmap(projectId) {
  if (!projectId) return;
  try {
    const [stagesRes, tasksRes, truthRes] = await Promise.all([
      fetch(`/api/projects/${encodeURIComponent(projectId)}/stages`),
      fetch(`/api/projects/${encodeURIComponent(projectId)}/planned-tasks`),
      fetch(`/api/projects/${encodeURIComponent(projectId)}/truth`),
    ]);
    const stages = (await stagesRes.json()).stages || [];
    const tasks = (await tasksRes.json()).planned_tasks || [];
    const truth = (await truthRes.json()).truth || [];

    const stagesList = document.querySelector("#stages-list");
    stagesList.innerHTML = stages.length
      ? stages
          .slice()
          .sort((a, b) => (a.order || 0) - (b.order || 0))
          .map(
            (s) => `<article class="queue-item"><strong>${escapeHtml(s.order)}. ${escapeHtml(s.name)}</strong>
          <div class="queue-meta"><span>${escapeHtml(s.id)}</span><span>${escapeHtml(s.status)}</span></div>
          <p class="queue-action">${escapeHtml(s.objective || "")}</p></article>`,
          )
          .join("")
      : `<div class="empty-inline">No stages yet.</div>`;

    const tasksList = document.querySelector("#planned-tasks-list");
    tasksList.innerHTML = tasks.length
      ? tasks
          .map(
            (t) => `<article class="queue-item">
          <div class="queue-item-head"><h3 class="queue-item-title">${escapeHtml(t.id)} · ${escapeHtml(t.title)}</h3>
          <span class="risk-badge risk-${String(t.risk || "yellow").toLowerCase()}">${escapeHtml(t.risk || "")}</span></div>
          <div class="queue-meta"><span>${escapeHtml(t.status)}</span><span>${escapeHtml(t.stage_id || "")}</span>
          <span>deps: ${escapeHtml((t.dependencies || []).join(", ") || "none")}</span></div>
          <p class="queue-action">${escapeHtml(t.objective || "")}</p></article>`,
          )
          .join("")
      : `<div class="empty-inline">No planned tasks yet.</div>`;

    const truthList = document.querySelector("#truth-list");
    truthList.innerHTML = truth.length
      ? truth
          .map(
            (t) => `<article class="queue-item">
          <div class="queue-item-head"><h3 class="queue-item-title">${escapeHtml(t.title)}</h3>
          <span class="chip">${escapeHtml(t.category)}</span></div>
          <p class="queue-action">${escapeHtml(t.description || "")}</p>
          <div class="queue-meta"><span>confidence ${escapeHtml(String(t.confidence))}</span>
          <span>${escapeHtml(t.source || "")}</span></div></article>`,
          )
          .join("")
      : `<div class="empty-inline">No truth items yet.</div>`;
  } catch (error) {
    showFeedback("#projects-feedback", error.message, "is-error");
  }
}

document.querySelector("#planner-refresh")?.addEventListener("click", refreshPlan);
document.querySelector("#planner-sample")?.addEventListener("click", () => loadSampleProject("#planner-feedback"));
document.querySelector("#planner-briefing")?.addEventListener("click", generateBriefing);
document.querySelector("#proj-load-sample")?.addEventListener("click", () => loadSampleProject("#projects-feedback"));
document.querySelector("#roadmap_project")?.addEventListener("change", (e) => loadRoadmap(e.target.value));

document.querySelector("#project-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const response = await fetch("/api/projects", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        id: document.querySelector("#proj_id").value.trim() || undefined,
        name: document.querySelector("#proj_name").value.trim(),
        repository: document.querySelector("#proj_repo").value.trim(),
        default_branch: document.querySelector("#proj_branch").value.trim() || "main",
        objective: document.querySelector("#proj_objective").value.trim(),
        status: "active",
      }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    showFeedback("#projects-feedback", `Saved project ${data.project.id}`, "is-ok");
    await loadProjectsPage();
  } catch (error) {
    showFeedback("#projects-feedback", error.message, "is-error");
  }
});

document.querySelector("#stage-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const projectId = document.querySelector("#roadmap_project").value;
  if (!projectId) return showFeedback("#projects-feedback", "Select a project", "is-error");
  try {
    const response = await fetch(`/api/projects/${encodeURIComponent(projectId)}/stages`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: document.querySelector("#stage_name").value.trim(),
        order: Number(document.querySelector("#stage_order").value || 1),
        status: "not_started",
      }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    document.querySelector("#stage_name").value = "";
    await loadRoadmap(projectId);
  } catch (error) {
    showFeedback("#projects-feedback", error.message, "is-error");
  }
});

document.querySelector("#planned-task-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const projectId = document.querySelector("#roadmap_project").value;
  if (!projectId) return showFeedback("#projects-feedback", "Select a project", "is-error");
  try {
    const response = await fetch(`/api/projects/${encodeURIComponent(projectId)}/planned-tasks`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        title: document.querySelector("#pt_title").value.trim(),
        stage_id: document.querySelector("#pt_stage").value.trim() || null,
        risk: document.querySelector("#pt_risk").value,
        status: document.querySelector("#pt_status").value,
        objective: document.querySelector("#pt_objective").value.trim(),
        dependencies: linesFrom(document.querySelector("#pt_deps").value),
        priority: "medium",
        estimated_effort: "small",
      }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    document.querySelector("#pt_title").value = "";
    await loadRoadmap(projectId);
  } catch (error) {
    showFeedback("#projects-feedback", error.message, "is-error");
  }
});

document.querySelector("#truth-form")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const projectId = document.querySelector("#roadmap_project").value;
  if (!projectId) return showFeedback("#projects-feedback", "Select a project", "is-error");
  try {
    const response = await fetch(`/api/projects/${encodeURIComponent(projectId)}/truth`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        title: document.querySelector("#truth_title").value.trim(),
        category: document.querySelector("#truth_cat").value,
        description: document.querySelector("#truth_desc").value.trim(),
        confidence: Number(document.querySelector("#truth_conf").value || 50),
        source: "manual",
        evidence: [],
      }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    document.querySelector("#truth_title").value = "";
    await loadRoadmap(projectId);
  } catch (error) {
    showFeedback("#projects-feedback", error.message, "is-error");
  }
});

// —— Stage 5 execution control ——
async function refreshExecutionPage() {
  try {
    const [ctrlRes, locksRes, provRes, healthRes, recRes, runsRes, projRes, packetsRes] = await Promise.all([
      fetch("/api/execution/control"),
      fetch("/api/repository-locks?active=true"),
      fetch("/api/providers"),
      fetch("/api/providers/health"),
      fetch("/api/providers/recommend?risk=YELLOW&mode=IMPLEMENTATION"),
      fetch("/api/runs"),
      fetch("/api/projects"),
      fetch("/api/packets"),
    ]);
    const control = (await ctrlRes.json()).control || {};
    const locks = (await locksRes.json()).locks || [];
    const providers = (await provRes.json()).providers || [];
    const health = (await healthRes.json()).providers || [];
    const recommendation = await recRes.json();
    const runs = (await runsRes.json()).runs || [];
    const projects = ((await projRes.json()).projects || []).filter((p) => p.status !== "archived");
    const packets = (await packetsRes.json()).packets || [];

    setText("#ex-kill", control.kill_switch_active ? "ON" : "off");
    setText("#ex-locks", locks.length);
    setText("#ex-runs", runs.length);
    const readyCount = health.filter((h) => h.live_ready).length;
    setText("#ex-providers", `${readyCount}/${health.length || 4} live-ready`);
    setText(
      "#ex-kill-detail",
      control.kill_switch_active
        ? `Active · ${control.reason || "no reason"} · ${control.activated_at || ""}`
        : `Inactive · last update ${control.updated_at || "—"}`,
    );

    const fillProjects = (sel) => {
      if (!sel) return;
      sel.innerHTML = projects.map((p) => `<option value="${escapeHtml(p.id)}">${escapeHtml(p.name)}</option>`).join("") ||
        `<option value="">No projects</option>`;
    };
    fillProjects(document.querySelector("#ex-project"));
    fillProjects(document.querySelector("#ex-run-project"));
    const healthById = Object.fromEntries(health.map((h) => [h.provider_id, h]));
    const provSel = document.querySelector("#ex-run-provider");
    if (provSel) {
      provSel.innerHTML = providers
        .map((p) => {
          const h = healthById[p.provider_id] || {};
          const tag = h.live_ready ? "live-ready" : h.available ? "discovered" : "unavailable";
          return `<option value="${escapeHtml(p.provider_id)}">${escapeHtml(p.display_name)} (${tag})</option>`;
        })
        .join("");
    }
    if (packets[0] && document.querySelector("#ex-run-packet") && !document.querySelector("#ex-run-packet").value) {
      document.querySelector("#ex-run-packet").value = packets[packets.length - 1].id || "";
    }

    const healthEl = document.querySelector("#ex-provider-health");
    if (healthEl) {
      healthEl.innerHTML = `
        <table class="data-table">
          <thead><tr><th>Provider</th><th>Status</th><th>Executable</th><th>Version</th><th>Ack</th><th>Reasons</th></tr></thead>
          <tbody>
            ${health
              .map(
                (h) => `<tr>
                <td>${escapeHtml(h.provider_id || "")}</td>
                <td>${escapeHtml(h.status || "")}</td>
                <td class="mono">${escapeHtml(h.executable || "—")}</td>
                <td>${escapeHtml(String(h.version || "—").slice(0, 40))}</td>
                <td>${h.constitution_acknowledged ? "yes" : "no"}</td>
                <td>${escapeHtml((h.unsupported_reasons || []).join("; ") || "—")}</td>
              </tr>`
              )
              .join("")}
          </tbody>
        </table>`;
    }
    const recEl = document.querySelector("#ex-provider-recommend");
    if (recEl) {
      const top = recommendation.recommendation || {};
      recEl.textContent = top.provider_id
        ? `Recommended: ${top.provider_id} (score ${top.score}) — ${(top.reasons || []).slice(0, 3).join("; ")}`
        : "No recommendation available";
    }

    const provList = document.querySelector("#ex-provider-list");
    provList.innerHTML = providers
      .map((p) => {
        const h = healthById[p.provider_id] || {};
        return `<article class="queue-item">
        <div class="queue-item-head"><h3 class="queue-item-title">${escapeHtml(p.display_name)}</h3>
        <span class="chip">${escapeHtml(h.status || "unknown")}</span></div>
        <div class="queue-meta">
          <span>enabled: ${p.enabled ? "yes" : "no"}</span>
          <span>live_ready: ${h.live_ready ? "yes" : "no"}</span>
          <span>ack: ${p.constitution_acknowledged ? "yes" : "no"}</span>
          <span>risk: ${(p.supported_risk_levels || []).join("/")}</span>
        </div>
        <p class="queue-action">Caps: ${(p.capabilities || []).join(", ")}</p>
      </article>`;
      })
      .join("") || `<div class="empty-inline">No providers</div>`;

    const lockList = document.querySelector("#ex-lock-list");
    lockList.innerHTML = locks.length
      ? locks
          .map(
            (l) => `<article class="queue-item">
          <div class="queue-item-head"><h3 class="queue-item-title">${escapeHtml(l.repository)} · ${escapeHtml(l.lock_scope)}</h3>
          <button type="button" class="btn btn-ghost btn-sm" data-release="${escapeHtml(l.id)}">Release</button></div>
          <p class="queue-action">${escapeHtml(l.reason || "")}</p></article>`,
          )
          .join("")
      : `<div class="empty-inline">No active locks</div>`;
    lockList.querySelectorAll("[data-release]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        await fetch(`/api/repository-locks/${encodeURIComponent(btn.getAttribute("data-release"))}/release`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ reason: "released from UI" }),
        });
        await refreshExecutionPage();
      });
    });

    const runList = document.querySelector("#ex-run-list");
    runList.innerHTML = runs.length
      ? runs
          .slice()
          .reverse()
          .map(
            (r) => `<article class="queue-item">
          <div class="queue-item-head">
            <h3 class="queue-item-title">${escapeHtml(r.id)}</h3>
            <span class="risk-badge risk-${String(r.risk || "yellow").toLowerCase()}">${escapeHtml(r.risk || "")}</span>
          </div>
          <div class="queue-meta">
            <span>${escapeHtml(r.status)}</span>
            <span>${escapeHtml(r.provider_id)}</span>
            <span>${escapeHtml(r.execution_mode || r.mode || "dry_run")}</span>
            <span>${escapeHtml(r.target_branch || "")}</span>
            <span>${escapeHtml(r.project_id || "")}</span>
          </div>
          <div class="queue-actions">
            <button type="button" class="btn btn-secondary btn-sm" data-view-run="${escapeHtml(r.id)}">View</button>
            <button type="button" class="btn btn-secondary btn-sm" data-preflight="${escapeHtml(r.id)}">Preflight</button>
            <button type="button" class="btn btn-secondary btn-sm" data-approve="${escapeHtml(r.id)}">Approve local</button>
            <button type="button" class="btn btn-primary btn-sm" data-dry="${escapeHtml(r.id)}">Dry-run</button>
            <button type="button" class="btn btn-primary btn-sm" data-execute="${escapeHtml(r.id)}">Execute supervised</button>
            <button type="button" class="btn btn-secondary btn-sm" data-review-accept="${escapeHtml(r.id)}">Accept for PR prep</button>
            <button type="button" class="btn btn-danger btn-sm" data-cancel="${escapeHtml(r.id)}">Cancel</button>
          </div>
        </article>`,
          )
          .join("")
      : `<div class="empty-inline">No supervised runs yet.</div>`;

    runList.querySelectorAll("[data-view-run]").forEach((btn) =>
      btn.addEventListener("click", () => viewRun(btn.getAttribute("data-view-run"))),
    );
    runList.querySelectorAll("[data-preflight]").forEach((btn) =>
      btn.addEventListener("click", () => runAction(btn.getAttribute("data-preflight"), "preflight")),
    );
    runList.querySelectorAll("[data-approve]").forEach((btn) =>
      btn.addEventListener("click", () =>
        runAction(btn.getAttribute("data-approve"), "approve", {
          requirement_type: "shan_task_approval",
          note: "Local UI approval",
        }),
      ),
    );
    runList.querySelectorAll("[data-dry]").forEach((btn) =>
      btn.addEventListener("click", () => runAction(btn.getAttribute("data-dry"), "dry-run")),
    );
    runList.querySelectorAll("[data-execute]").forEach((btn) =>
      btn.addEventListener("click", () => {
        if (!confirm("Execute live supervised provider in isolated worktree? No merge/deploy.")) return;
        runAction(btn.getAttribute("data-execute"), "execute");
      }),
    );
    runList.querySelectorAll("[data-review-accept]").forEach((btn) =>
      btn.addEventListener("click", () =>
        runAction(btn.getAttribute("data-review-accept"), "review", {
          decision: "accept_for_pr_prep",
          note: "Founder accept for PR prep only",
        }),
      ),
    );
    runList.querySelectorAll("[data-cancel]").forEach((btn) =>
      btn.addEventListener("click", () => runAction(btn.getAttribute("data-cancel"), "cancel", { reason: "UI cancel" })),
    );
  } catch (error) {
    showFeedback("#ex-feedback", error.message, "is-error");
  }
}

async function viewRun(runId) {
  try {
    const response = await fetch(`/api/runs/${encodeURIComponent(runId)}`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    setText("#ex-detail-title", `Run ${runId}`);
    document.querySelector("#ex-detail").textContent = JSON.stringify(data, null, 2);
  } catch (error) {
    showFeedback("#ex-feedback", error.message, "is-error");
  }
}

async function runAction(runId, action, body = {}) {
  try {
    const response = await fetch(`/api/runs/${encodeURIComponent(runId)}/${action}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    showFeedback("#ex-feedback", `${action} ok for ${runId}`, "is-ok");
    document.querySelector("#ex-detail").textContent = JSON.stringify(data, null, 2);
    await refreshExecutionPage();
  } catch (error) {
    showFeedback("#ex-feedback", error.message, "is-error");
  }
}

document.querySelector("#ex-refresh")?.addEventListener("click", refreshExecutionPage);
document.querySelector("#ex-kill-on")?.addEventListener("click", async () => {
  if (!confirm("Activate global kill switch? All new runs will be blocked.")) return;
  await fetch("/api/execution/control", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      kill_switch_active: true,
      reason: document.querySelector("#ex-kill-reason").value || "Activated from UI",
    }),
  });
  await refreshExecutionPage();
});
document.querySelector("#ex-kill-off")?.addEventListener("click", async () => {
  if (!confirm("Deactivate kill switch?")) return;
  await fetch("/api/execution/control", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      kill_switch_active: false,
      reason: document.querySelector("#ex-kill-reason").value || "Deactivated from UI",
    }),
  });
  await refreshExecutionPage();
});
document.querySelector("#ex-project-save")?.addEventListener("click", async () => {
  const projectId = document.querySelector("#ex-project").value;
  const response = await fetch(`/api/projects/${encodeURIComponent(projectId)}/execution-control`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      execution_status: document.querySelector("#ex-project-status").value,
      reason: document.querySelector("#ex-project-reason").value,
    }),
  });
  const data = await response.json();
  if (!response.ok) showFeedback("#ex-feedback", data.error || "failed", "is-error");
  else showFeedback("#ex-feedback", `Project execution → ${data.control.execution_status}`, "is-ok");
});
document.querySelector("#ex-lock-add")?.addEventListener("click", async () => {
  const response = await fetch("/api/repository-locks", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      repository: document.querySelector("#ex-lock-repo").value,
      lock_scope: document.querySelector("#ex-lock-scope").value,
      reason: document.querySelector("#ex-lock-reason").value,
      project_id: document.querySelector("#ex-project").value || null,
    }),
  });
  const data = await response.json();
  if (!response.ok) showFeedback("#ex-feedback", data.error || "failed", "is-error");
  else await refreshExecutionPage();
});
document.querySelector("#ex-run-create")?.addEventListener("click", async () => {
  try {
    const modeEl = document.querySelector("#ex-run-exec-mode");
    const response = await fetch("/api/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        project_id: document.querySelector("#ex-run-project").value,
        provider_id: document.querySelector("#ex-run-provider").value,
        packet_id: document.querySelector("#ex-run-packet").value.trim(),
        target_branch: document.querySelector("#ex-run-branch").value.trim(),
        operating_mode: document.querySelector("#ex-run-mode").value,
        execution_mode: modeEl ? modeEl.value : "dry_run",
      }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    showFeedback("#ex-feedback", `Draft run ${data.run.id}`, "is-ok");
    await refreshExecutionPage();
    await viewRun(data.run.id);
  } catch (error) {
    showFeedback("#ex-feedback", error.message, "is-error");
  }
});

document.querySelector("#constitution-reload-btn")?.addEventListener("click", () => {
  refreshConstitutionPage();
});
document.querySelector("#constitution-refresh-btn")?.addEventListener("click", async () => {
  try {
    const response = await fetch("/api/constitution/refresh", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ actor: "shan" }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    await refreshConstitutionPage();
  } catch (error) {
    alert(error.message || "Constitution refresh failed");
  }
});

setupNav();
showView("classify");
checkServer();
