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

let lastPacket = null;
let lastClassification = null;

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
  for (const value of values) {
    const item = document.createElement("li");
    item.textContent = value;
    node.appendChild(item);
  }
}

function renderDecision(task, result, source) {
  lastPacket = task;
  lastClassification = result;
  const badge = document.querySelector("#risk-badge");
  badge.textContent = result.risk;
  badge.className = `risk risk-${result.risk.toLowerCase()}`;
  document.querySelector("#auto_run").textContent = result.auto_run_allowed ? "Yes" : "No";
  document.querySelector("#auto_merge").textContent = result.auto_merge_allowed ? "Yes" : "No";
  document.querySelector("#human_approval").textContent = result.required_human_approval ? "Required" : "Not required";
  renderList("#reasons", result.reasons);
  renderList("#actions", result.required_actions);
  document.querySelector("#packet").textContent = JSON.stringify({ ...task, classification: result, source }, null, 2);
}

async function render() {
  const task = packetFromForm();
  try {
    const serverResult = await classifyWithServer(task);
    renderDecision(task, serverResult.classification, "server");
  } catch (error) {
    renderDecision(task, classifyLocally(task), "browser-fallback");
  }
}

async function checkServer() {
  const node = document.querySelector("#server-status");
  try {
    const response = await fetch("/api/health");
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    const payload = await response.json();
    node.textContent = `Server connected: ${payload.service}`;
    node.classList.remove("warning");
    await loadTasks();
  } catch (error) {
    node.textContent = "Static mode: start `python -m buildforme.cli serve` to save tasks and inspect GitHub.";
    node.classList.add("warning");
  }
}

async function saveTask() {
  const task = packetFromForm();
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
  await loadTasks();
}

function taskCard(record) {
  const risk = record.classification?.risk || "UNKNOWN";
  const task = record.task || {};
  const node = document.createElement("article");
  node.className = "mini-card";
  node.innerHTML = `
    <div class="mini-card-head">
      <strong>${escapeHtml(task.task_id || "untitled")}</strong>
      <span class="risk risk-${String(risk).toLowerCase()}">${escapeHtml(risk)}</span>
    </div>
    <p>${escapeHtml(task.objective || "No objective")}</p>
    <small>Status: ${escapeHtml(record.status || "draft")} · Updated: ${escapeHtml(record.updated_at || "unknown")}</small>
  `;
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
    if (!payload.tasks || payload.tasks.length === 0) {
      list.innerHTML = `<p class="muted">No saved tasks yet.</p>`;
      return;
    }
    for (const record of payload.tasks.slice().reverse()) {
      list.appendChild(taskCard(record));
    }
  } catch (error) {
    list.innerHTML = `<p class="muted warning">Task storage unavailable in static mode.</p>`;
  }
}

async function inspectPullRequest(event) {
  event.preventDefault();
  const repository = document.querySelector("#github_repository").value.trim();
  const number = document.querySelector("#github_pr_number").value.trim();
  const output = document.querySelector("#github-output");
  output.textContent = "Loading pull request...";
  try {
    const response = await fetch(`/api/github/pr?repository=${encodeURIComponent(repository)}&number=${encodeURIComponent(number)}`);
    const payload = await response.json();
    output.textContent = JSON.stringify(payload, null, 2);
  } catch (error) {
    output.textContent = `GitHub check failed: ${error.message}`;
  }
}

async function loadIssues() {
  const repository = document.querySelector("#github_repository").value.trim();
  const output = document.querySelector("#github-output");
  output.textContent = "Loading issues...";
  try {
    const response = await fetch(`/api/github/issues?repository=${encodeURIComponent(repository)}&state=open&limit=20`);
    const payload = await response.json();
    output.textContent = JSON.stringify(payload, null, 2);
  } catch (error) {
    output.textContent = `GitHub issue check failed: ${error.message}`;
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

document.querySelector("#task-form").addEventListener("submit", (event) => {
  event.preventDefault();
  render();
});

document.querySelector("#save-task").addEventListener("click", async () => {
  try {
    await saveTask();
  } catch (error) {
    alert(error.message);
  }
});

document.querySelector("#github-form").addEventListener("submit", inspectPullRequest);
document.querySelector("#load-issues").addEventListener("click", loadIssues);

render();
checkServer();
