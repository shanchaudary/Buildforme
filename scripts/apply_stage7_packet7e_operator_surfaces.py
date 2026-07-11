from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def replace_once(text: str, old: str, new: str, *, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one anchor, found {count}")
    return text.replace(old, new, 1)


# ---------------------------------------------------------------------------
# HTTP API: read-only views + founder-authenticated fixed repair actions.
# ---------------------------------------------------------------------------
path = ROOT / "buildforme" / "server.py"
text = path.read_text(encoding="utf-8")
text = replace_once(
    text,
    '''from buildforme.review_execution import execute_independent_review_assignment
from buildforme.review_service import (
''',
    '''from buildforme.review_execution import execute_independent_review_assignment
from buildforme.repair_service import (
    admit_governed_repair_run,
    create_governed_repair_packet,
    create_repair_review_cycle,
    execute_governed_repair_and_open_review,
)
from buildforme.review_service import (
''',
    label="server repair imports",
)
text = replace_once(
    text,
    '''        if path == "/api/runs":
            project_id = _first_query_value(parsed, "project_id")
''',
    '''        if path == "/api/repair-packets":
            self._json(
                HTTPStatus.OK,
                {"repair_packets": self._store().list_repair_packets()},
            )
            return
        if path.startswith("/api/repair-packets/") and path.count("/") == 3:
            repair_packet_id = path.removeprefix("/api/repair-packets/").strip("/")
            self._stage7_repair_view(repair_packet_id)
            return
        if path == "/api/runs":
            project_id = _first_query_value(parsed, "project_id")
''',
    label="server repair GET routes",
)
text = replace_once(
    text,
    '''        if path.startswith("/api/runs/") and path.endswith("/reviews"):
            self._stage7_review_action(path, "create")
            return
''',
    '''        if path.startswith("/api/review-cycles/") and path.endswith("/repair-packet"):
            self._stage7_repair_action(path, "create")
            return
        if path.startswith("/api/repair-packets/") and path.endswith("/admit"):
            self._stage7_repair_action(path, "admit")
            return
        if path.startswith("/api/repair-packets/") and path.endswith("/review-cycle"):
            self._stage7_repair_action(path, "review-cycle")
            return
        if path.startswith("/api/repair-packets/") and path.endswith("/execute"):
            self._stage7_repair_action(path, "execute")
            return
        if path.startswith("/api/runs/") and path.endswith("/reviews"):
            self._stage7_review_action(path, "create")
            return
''',
    label="server repair POST routes",
)
anchor = '''    def _stage7_review_action(self, path: str, action: str) -> None:
'''
methods = r'''    def _stage7_repair_view(self, repair_packet_id: str) -> None:
        try:
            store = self._store()
            packet = store.get_repair_packet(repair_packet_id)
            admission = None
            link = None
            child_run = None
            source_run = None
            try:
                admission = store.get_repair_admission(repair_packet_id)
                child_run = store.get_run(str(admission.get("child_run_id") or ""))
            except KeyError:
                admission = None
            try:
                link = store.get_repair_review_link(repair_packet_id)
            except KeyError:
                link = None
            try:
                source_run = store.get_run(str(packet.get("source_run_id") or ""))
            except KeyError:
                source_run = None
            self._json(
                HTTPStatus.OK,
                {
                    "repair_packet": packet,
                    "repair_admission": admission,
                    "repair_review_link": link,
                    "source_run": source_run,
                    "child_run": child_run,
                },
            )
        except KeyError as exc:
            self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
        except ValueError as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    def _stage7_repair_action(self, path: str, action: str) -> None:
        try:
            payload = self._read_json() if int(self.headers.get("Content-Length") or "0") else {}
            if not isinstance(payload, dict):
                payload = {}
            auth = self._require_founder_mutation(payload)
            actor = str(payload.get("actor") or auth.get("actor") or "shan")
            forbidden = {
                "argv",
                "command",
                "executable",
                "repo_root",
                "repository_local_path",
                "local_path",
                "allowed_files",
                "forbidden_files",
                "reviewers",
                "policy",
                "scope_fingerprint",
                "repair_fingerprint",
                "seed_commit",
                "seed_ref",
                "child_run",
                "lease",
            }
            supplied = sorted(key for key in forbidden if key in payload)
            if supplied:
                raise ValueError(
                    "repair authority is storage-owned; forbidden fields supplied: "
                    + ", ".join(supplied)
                )
            if action == "create":
                cycle_id = path.removeprefix("/api/review-cycles/").removesuffix(
                    "/repair-packet"
                ).strip("/")
                provider_id = str(payload.get("repair_provider_id") or "").strip()
                if not provider_id:
                    raise ValueError("repair_provider_id required")
                result = {
                    "repair_packet": create_governed_repair_packet(
                        self._store(),
                        cycle_id,
                        repair_provider_id=provider_id,
                        actor=actor,
                    )
                }
            else:
                suffix = {
                    "admit": "/admit",
                    "review-cycle": "/review-cycle",
                    "execute": "/execute",
                }[action]
                repair_packet_id = path.removeprefix("/api/repair-packets/").removesuffix(
                    suffix
                ).strip("/")
                if action == "admit":
                    result = admit_governed_repair_run(
                        self._store(), repair_packet_id, actor=actor
                    )
                elif action == "review-cycle":
                    result = create_repair_review_cycle(
                        self._store(), repair_packet_id, actor=actor
                    )
                elif action == "execute":
                    result = execute_governed_repair_and_open_review(
                        self._store(), repair_packet_id, actor=actor
                    )
                else:
                    raise ValueError("unknown Stage 7 repair action")
            self._json(HTTPStatus.OK, result)
        except KeyError as exc:
            self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
        except (ValueError, json.JSONDecodeError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    def _stage7_review_action(self, path: str, action: str) -> None:
'''
text = replace_once(text, anchor, methods, label="server repair handlers")
path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI commands: local operator wrappers; no arbitrary path/command/scope input.
# ---------------------------------------------------------------------------
path = ROOT / "buildforme" / "cli.py"
text = path.read_text(encoding="utf-8")
anchor = '''def build_parser() -> argparse.ArgumentParser:
'''
functions = r'''def repair_list_command(args: argparse.Namespace) -> int:
    from buildforme.storage import LocalStore

    packets = LocalStore(args.state).list_repair_packets()
    print(json.dumps({"repair_packets": packets}, indent=2, sort_keys=True, default=str))
    return 0


def repair_show_command(args: argparse.Namespace) -> int:
    from buildforme.storage import LocalStore

    store = LocalStore(args.state)
    packet = store.get_repair_packet(args.repair_packet_id)
    try:
        admission = store.get_repair_admission(args.repair_packet_id)
    except KeyError:
        admission = None
    try:
        link = store.get_repair_review_link(args.repair_packet_id)
    except KeyError:
        link = None
    payload = {
        "repair_packet": packet,
        "repair_admission": admission,
        "repair_review_link": link,
    }
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    return 0


def repair_create_command(args: argparse.Namespace) -> int:
    from buildforme.repair_service import create_governed_repair_packet
    from buildforme.storage import LocalStore

    packet = create_governed_repair_packet(
        LocalStore(args.state),
        args.cycle_id,
        repair_provider_id=args.provider,
        actor=args.actor,
    )
    print(json.dumps({"repair_packet": packet}, indent=2, sort_keys=True, default=str))
    return 0


def repair_admit_command(args: argparse.Namespace) -> int:
    from buildforme.repair_service import admit_governed_repair_run
    from buildforme.storage import LocalStore

    result = admit_governed_repair_run(
        LocalStore(args.state), args.repair_packet_id, actor=args.actor
    )
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0


def repair_review_cycle_command(args: argparse.Namespace) -> int:
    from buildforme.repair_service import create_repair_review_cycle
    from buildforme.storage import LocalStore

    result = create_repair_review_cycle(
        LocalStore(args.state), args.repair_packet_id, actor=args.actor
    )
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0


def repair_execute_command(args: argparse.Namespace) -> int:
    from buildforme.repair_service import execute_governed_repair_and_open_review
    from buildforme.storage import LocalStore

    result = execute_governed_repair_and_open_review(
        LocalStore(args.state), args.repair_packet_id, actor=args.actor
    )
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    status = str((result.get("run") or {}).get("status") or "")
    return 0 if status == "needs_review" else 2


def build_parser() -> argparse.ArgumentParser:
'''
text = replace_once(text, anchor, functions, label="CLI repair functions")
anchor = '''    rev = subparsers.add_parser("run-evidence", help="Show evidence bundle for a run")
    rev.add_argument("run_id")
    rev.add_argument("--state", default="runtime/buildforme_state.json")
    rev.set_defaults(func=run_evidence_command)
    return parser
'''
parsers = '''    rev = subparsers.add_parser("run-evidence", help="Show evidence bundle for a run")
    rev.add_argument("run_id")
    rev.add_argument("--state", default="runtime/buildforme_state.json")
    rev.set_defaults(func=run_evidence_command)

    repair_list = subparsers.add_parser("repair-list", help="List governed Stage 7 repair packets")
    repair_list.add_argument("--state", default="runtime/buildforme_state.json")
    repair_list.set_defaults(func=repair_list_command)

    repair_show = subparsers.add_parser("repair-show", help="Show repair packet, admission, and re-review link")
    repair_show.add_argument("repair_packet_id")
    repair_show.add_argument("--state", default="runtime/buildforme_state.json")
    repair_show.set_defaults(func=repair_show_command)

    repair_create = subparsers.add_parser("repair-create", help="Create one governed repair packet from a finalized review cycle")
    repair_create.add_argument("cycle_id")
    repair_create.add_argument("--provider", required=True, help="Repair implementation provider id")
    repair_create.add_argument("--actor", default="shan")
    repair_create.add_argument("--state", default="runtime/buildforme_state.json")
    repair_create.set_defaults(func=repair_create_command)

    repair_admit = subparsers.add_parser("repair-admit", help="Create exact seed and atomically admit repair child")
    repair_admit.add_argument("repair_packet_id")
    repair_admit.add_argument("--actor", default="shan")
    repair_admit.add_argument("--state", default="runtime/buildforme_state.json")
    repair_admit.set_defaults(func=repair_admit_command)

    repair_cycle = subparsers.add_parser("repair-review-cycle", help="Open mandatory fresh review cycle after verified repair")
    repair_cycle.add_argument("repair_packet_id")
    repair_cycle.add_argument("--actor", default="shan")
    repair_cycle.add_argument("--state", default="runtime/buildforme_state.json")
    repair_cycle.set_defaults(func=repair_review_cycle_command)

    repair_execute = subparsers.add_parser("repair-execute", help="Execute approved repair child and open fresh review")
    repair_execute.add_argument("repair_packet_id")
    repair_execute.add_argument("--actor", default="shan")
    repair_execute.add_argument("--state", default="runtime/buildforme_state.json")
    repair_execute.set_defaults(func=repair_execute_command)
    return parser
'''
text = replace_once(text, anchor, parsers, label="CLI repair parsers")
path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Dashboard: fixed repair controls + status. Founder credentials stay in memory inputs.
# ---------------------------------------------------------------------------
path = ROOT / "public" / "index.html"
text = path.read_text(encoding="utf-8")
anchor = '''          <button type="button" class="nav-item" data-view="planner" role="tab" aria-selected="false">
'''
nav = '''          <button type="button" class="nav-item" data-view="repairs" role="tab" aria-selected="false">
            <span class="nav-icon" aria-hidden="true">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none"><path d="M14.7 6.3a4 4 0 01-5 5L4 17v3h3l5.7-5.7a4 4 0 005-5l-2.3 2.3-3-3L14.7 6.3z" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>
            </span>
            Reviews &amp; repairs
          </button>
          <button type="button" class="nav-item" data-view="planner" role="tab" aria-selected="false">
'''
text = replace_once(text, anchor, nav, label="dashboard repair nav")
anchor = '''        <!-- VIEW: Chief planner -->
'''
panel = '''        <!-- VIEW: Stage 7 reviews and repairs -->
        <section id="view-repairs" class="view" data-view-panel="repairs" hidden>
          <div class="local-only-banner">
            Stage 7 independent review and repair control. All mutations require a founder session and CSRF token.
            Commands, paths, scope, reviewer lists, seed refs, merge, and deployment authority are never accepted here.
          </div>
          <div class="metrics queue-metrics">
            <article class="metric"><span class="metric-label">Repair packets</span><strong class="metric-value" id="rp-packets">—</strong></article>
            <article class="metric"><span class="metric-label">Children admitted</span><strong class="metric-value" id="rp-admitted">—</strong></article>
            <article class="metric"><span class="metric-label">Fresh reviews</span><strong class="metric-value" id="rp-rereviews">—</strong></article>
            <article class="metric"><span class="metric-label">Runs needing review</span><strong class="metric-value" id="rp-needs-review">—</strong></article>
          </div>
          <div class="split packet-split">
            <div class="card">
              <div class="card-head"><div><h2>Founder authorization</h2><p class="card-sub">Paste the one-time local session values. They are not saved by the page.</p></div><span class="chip chip-accent">Stage 7</span></div>
              <div class="field"><label for="rp-founder-token">Founder token</label><input id="rp-founder-token" type="password" autocomplete="off" /></div>
              <div class="field"><label for="rp-csrf-token">CSRF token</label><input id="rp-csrf-token" type="password" autocomplete="off" /></div>
              <div class="button-row"><button type="button" id="rp-refresh" class="btn btn-secondary">Refresh status</button></div>
              <p id="rp-feedback" class="toast" hidden></p>
            </div>
            <div class="card">
              <div class="card-head"><div><h2>Create repair packet</h2><p class="card-sub">Only a finalized repair_required cycle is accepted.</p></div></div>
              <div class="field"><label for="rp-cycle-id">Review cycle id</label><input id="rp-cycle-id" placeholder="rc-..." /></div>
              <div class="field"><label for="rp-provider-id">Repair provider id</label><input id="rp-provider-id" placeholder="glm" /></div>
              <button type="button" id="rp-create" class="btn btn-primary btn-sm">Create governed repair packet</button>
            </div>
          </div>
          <div class="card" style="margin-top:16px">
            <div class="card-head"><div><h2>Repair packets</h2><p class="card-sub">Fixed actions only: inspect, admit child, execute approved child, or open mandatory fresh review.</p></div></div>
            <div id="rp-list" class="queue-list"><div class="empty-inline">No repair packets.</div></div>
          </div>
          <div class="card" style="margin-top:16px">
            <div class="card-head"><div><h2>Selected repair detail</h2><p class="card-sub" id="rp-detail-title">Select a repair packet</p></div></div>
            <pre id="rp-detail" class="code-block packet-md">No repair packet selected.</pre>
          </div>
        </section>

        <!-- VIEW: Chief planner -->
'''
text = replace_once(text, anchor, panel, label="dashboard repair panel")
path.write_text(text, encoding="utf-8")

path = ROOT / "public" / "app.js"
text = path.read_text(encoding="utf-8")
text = replace_once(
    text,
    '''  planner: {
    kicker: "Control plane",
''',
    '''  repairs: {
    kicker: "Stage 7",
    title: "Independent reviews & repairs",
    desc: "Blind reviewer quorum, governed repair packets, exact seeds, fresh evidence, and mandatory re-review.",
  },
  planner: {
    kicker: "Control plane",
''',
    label="dashboard repair meta",
)
text = replace_once(
    text,
    '''  if (name === "execution" && serverOnline) {
    refreshExecutionPage();
  }
''',
    '''  if (name === "execution" && serverOnline) {
    refreshExecutionPage();
  }
  if (name === "repairs" && serverOnline) {
    refreshRepairsPage();
  }
''',
    label="dashboard repair view load",
)
anchor = '''// —— Stage 5 execution control ——
'''
js = r'''// —— Stage 7 independent reviews and repairs ——
function repairMutationBody(extra = {}) {
  return {
    founder_token: document.querySelector("#rp-founder-token")?.value || "",
    csrf_token: document.querySelector("#rp-csrf-token")?.value || "",
    actor: "shan",
    ...extra,
  };
}

async function loadRepairDetail(repairPacketId) {
  try {
    const response = await fetch(`/api/repair-packets/${encodeURIComponent(repairPacketId)}`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    setText("#rp-detail-title", `Repair ${repairPacketId}`);
    document.querySelector("#rp-detail").textContent = JSON.stringify(data, null, 2);
  } catch (error) {
    showFeedback("#rp-feedback", error.message, "is-error");
  }
}

async function repairMutation(url, body, message) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(repairMutationBody(body)),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  showFeedback("#rp-feedback", message, "is-ok");
  document.querySelector("#rp-detail").textContent = JSON.stringify(data, null, 2);
  await refreshRepairsPage();
  return data;
}

async function refreshRepairsPage() {
  const list = document.querySelector("#rp-list");
  if (!list) return;
  try {
    const [packetsResponse, runsResponse] = await Promise.all([
      fetch("/api/repair-packets"),
      fetch("/api/runs"),
    ]);
    const packetsPayload = await packetsResponse.json();
    const runsPayload = await runsResponse.json();
    if (!packetsResponse.ok) throw new Error(packetsPayload.error || `HTTP ${packetsResponse.status}`);
    if (!runsResponse.ok) throw new Error(runsPayload.error || `HTTP ${runsResponse.status}`);
    const packets = packetsPayload.repair_packets || [];
    const runs = runsPayload.runs || [];
    const details = await Promise.all(
      packets.map(async (packet) => {
        try {
          const response = await fetch(`/api/repair-packets/${encodeURIComponent(packet.repair_packet_id)}`);
          return response.ok ? await response.json() : { repair_packet: packet };
        } catch (_error) {
          return { repair_packet: packet };
        }
      }),
    );
    setText("#rp-packets", packets.length);
    setText("#rp-admitted", details.filter((item) => item.repair_admission).length);
    setText("#rp-rereviews", details.filter((item) => item.repair_review_link).length);
    setText("#rp-needs-review", runs.filter((run) => run.status === "needs_review").length);
    list.innerHTML = details.length
      ? details
          .slice()
          .reverse()
          .map((item) => {
            const packet = item.repair_packet || {};
            const admission = item.repair_admission;
            const link = item.repair_review_link;
            const child = item.child_run;
            const id = escapeHtml(packet.repair_packet_id || "");
            return `<article class="queue-item">
              <div class="queue-item-head"><h3 class="queue-item-title">${id}</h3><span class="chip">${escapeHtml(link ? "re-review" : admission ? "admitted" : "packet-ready")}</span></div>
              <div class="queue-meta"><span>${escapeHtml(packet.repair_provider_id || "")}</span><span>source ${escapeHtml(packet.source_run_id || "")}</span><span>child ${escapeHtml(child?.id || "—")}</span></div>
              <p class="queue-action">Blocking findings: ${escapeHtml(String((packet.source_blocking_findings || []).length))} · Allowed files: ${escapeHtml(String((packet.allowed_files || []).length))}</p>
              <div class="queue-actions">
                <button type="button" class="btn btn-secondary btn-sm" data-rp-view="${id}">Inspect</button>
                ${admission ? "" : `<button type="button" class="btn btn-primary btn-sm" data-rp-admit="${id}">Admit child</button>`}
                ${admission && !link ? `<button type="button" class="btn btn-secondary btn-sm" data-rp-cycle="${id}">Open fresh review</button><button type="button" class="btn btn-danger btn-sm" data-rp-execute="${id}">Execute approved repair</button>` : ""}
              </div>
            </article>`;
          })
          .join("")
      : `<div class="empty-inline">No governed repair packets.</div>`;
    list.querySelectorAll("[data-rp-view]").forEach((button) =>
      button.addEventListener("click", () => loadRepairDetail(button.getAttribute("data-rp-view"))),
    );
    list.querySelectorAll("[data-rp-admit]").forEach((button) =>
      button.addEventListener("click", async () => {
        try {
          const id = button.getAttribute("data-rp-admit");
          await repairMutation(`/api/repair-packets/${encodeURIComponent(id)}/admit`, {}, `Repair child admitted for ${id}`);
        } catch (error) {
          showFeedback("#rp-feedback", error.message, "is-error");
        }
      }),
    );
    list.querySelectorAll("[data-rp-cycle]").forEach((button) =>
      button.addEventListener("click", async () => {
        try {
          const id = button.getAttribute("data-rp-cycle");
          await repairMutation(`/api/repair-packets/${encodeURIComponent(id)}/review-cycle`, {}, `Fresh review opened for ${id}`);
        } catch (error) {
          showFeedback("#rp-feedback", error.message, "is-error");
        }
      }),
    );
    list.querySelectorAll("[data-rp-execute]").forEach((button) =>
      button.addEventListener("click", async () => {
        if (!confirm("Execute the approved repair child, verify it, and open mandatory fresh review? No merge or deploy.")) return;
        try {
          const id = button.getAttribute("data-rp-execute");
          await repairMutation(`/api/repair-packets/${encodeURIComponent(id)}/execute`, {}, `Repair execution completed for ${id}`);
        } catch (error) {
          showFeedback("#rp-feedback", error.message, "is-error");
        }
      }),
    );
  } catch (error) {
    list.innerHTML = `<div class="empty-inline warning">${escapeHtml(error.message)}</div>`;
    showFeedback("#rp-feedback", error.message, "is-error");
  }
}

// —— Stage 5 execution control ——
'''
text = replace_once(text, anchor, js, label="dashboard repair JS")
text = replace_once(
    text,
    '''document.querySelector("#ex-refresh")?.addEventListener("click", refreshExecutionPage);
''',
    '''document.querySelector("#rp-refresh")?.addEventListener("click", refreshRepairsPage);
document.querySelector("#rp-create")?.addEventListener("click", async () => {
  try {
    const cycleId = document.querySelector("#rp-cycle-id")?.value.trim();
    const providerId = document.querySelector("#rp-provider-id")?.value.trim();
    if (!cycleId || !providerId) throw new Error("Review cycle id and repair provider id are required.");
    await repairMutation(
      `/api/review-cycles/${encodeURIComponent(cycleId)}/repair-packet`,
      { repair_provider_id: providerId },
      `Governed repair packet created from ${cycleId}`,
    );
  } catch (error) {
    showFeedback("#rp-feedback", error.message, "is-error");
  }
});

document.querySelector("#ex-refresh")?.addEventListener("click", refreshExecutionPage);
''',
    label="dashboard repair listeners",
)
path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Strict real two-provider smoke evaluator and executable script.
# ---------------------------------------------------------------------------
smoke_module = r'''"""Machine-verifiable Stage 7 real reviewer smoke acceptance."""

from __future__ import annotations

from typing import Any

STAGE7_SMOKE_SCHEMA = "buildforme.stage7_real_two_provider_smoke.v1"


def evaluate_stage7_smoke(observed: dict[str, Any]) -> dict[str, Any]:
    attempts = observed.get("review_execution_attempts") or []
    provider_ids = sorted({str(item.get("provider_id") or "") for item in attempts if item.get("status") == "succeeded"})
    checks = {
        "controlled_implementation_fixture_disclosed": observed.get("controlled_implementation_fixture") is True,
        "real_reviewer_processes_only": bool(attempts)
        and all(item.get("process_started") is True and int((item.get("process") or {}).get("pid") or 0) > 0 for item in attempts),
        "codex_and_claude_succeeded": provider_ids == ["claude", "codex"],
        "auth_probes_verified": bool(attempts)
        and all(item.get("auth_probe_verified") is True for item in attempts),
        "process_exit_zero": bool(attempts)
        and all((item.get("process") or {}).get("exit_code") == 0 for item in attempts),
        "process_cleanup_confirmed": bool(attempts)
        and all((item.get("process") or {}).get("cleanup_ok") is True for item in attempts),
        "review_workspaces_unchanged": bool(attempts)
        and all(item.get("worktree_unchanged") is True and item.get("post_snapshot_proven") is True for item in attempts),
        "two_provider_quorum": observed.get("distinct_provider_count") == 2
        and sorted(observed.get("provider_ids") or []) == ["claude", "codex"],
        "aggregate_clear": observed.get("aggregate_status") == "clear",
        "verification_passed": observed.get("verification_passed") is True,
        "source_head_unchanged": observed.get("source_head_before") == observed.get("source_head_after"),
        "source_branch_unchanged": observed.get("source_branch_before") == observed.get("source_branch_after"),
        "source_patch_unchanged": observed.get("source_patch_before") == observed.get("source_patch_after"),
        "merge_not_performed": observed.get("merge_performed") is False,
        "no_synthetic_report_submission": observed.get("direct_report_submission_used") is False,
    }
    failed = sorted(name for name, passed in checks.items() if not passed)
    return {
        "schema": STAGE7_SMOKE_SCHEMA,
        "passed": not failed,
        "checks": checks,
        "failed_checks": failed,
        "provider_ids": provider_ids,
        "aggregate_status": observed.get("aggregate_status"),
        "controlled_implementation_fixture": bool(observed.get("controlled_implementation_fixture")),
        "note": "Reviewer processes are real. The implementation evidence is a disclosed controlled fixture, not a claimed third-provider execution.",
    }
'''
(ROOT / "buildforme" / "stage7_smoke.py").write_text(smoke_module, encoding="utf-8")

smoke_script = r'''from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from buildforme.changed_files import collect_changed_file_manifest, collect_patch_evidence
from buildforme.evidence import build_evidence_bundle
from buildforme.governance import compute_run_scope_fingerprint
from buildforme.review_execution import execute_independent_review_assignment
from buildforme.review_service import aggregate_independent_review_cycle, create_independent_review_cycle
from buildforme.stage7_smoke import evaluate_stage7_smoke
from buildforme.storage import LocalStore
from governance.constitution_engine import get_engine


def git(root: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr or proc.stdout or f"git {' '.join(args)} failed")
    return (proc.stdout or "").strip()


def main() -> int:
    smoke_root = Path(tempfile.mkdtemp(prefix="buildforme-stage7-real-smoke-"))
    repo = smoke_root / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.email", "stage7-smoke@buildforme.local")
    git(repo, "config", "user.name", "Buildforme Stage 7 Smoke")
    git(repo, "remote", "add", "origin", "https://github.com/shanchaudary/Buildforme.git")
    (repo / "README.md").write_text("# Stage 7 smoke fixture\n", encoding="utf-8")
    (repo / "math_util.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "baseline")
    baseline = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-b", "feature/stage7-real-review")
    (repo / "math_util.py").write_text(
        "def add(a, b):\n    return a + b\n\ndef subtract(a, b):\n    return a - b\n",
        encoding="utf-8",
    )
    (repo / "test_math_util.py").write_text(
        "import unittest\nimport math_util\n\nclass MathTests(unittest.TestCase):\n"
        "    def test_add(self): self.assertEqual(math_util.add(2, 3), 5)\n"
        "    def test_subtract(self): self.assertEqual(math_util.subtract(5, 3), 2)\n\n"
        "if __name__ == '__main__': unittest.main()\n",
        encoding="utf-8",
    )
    verify = subprocess.run(
        ["python", "-m", "unittest", "discover", "-s", ".", "-p", "test_*.py"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if verify.returncode != 0:
        raise RuntimeError(f"controlled fixture verification failed: {verify.stdout}\n{verify.stderr}")

    source_head_before = git(repo, "rev-parse", "HEAD")
    source_branch_before = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    source_patch_before = collect_patch_evidence(repo, baseline_commit=baseline)["patch_fingerprint"]

    store = LocalStore(smoke_root / "runtime" / "state.json")
    store.upsert_project(
        {
            "id": "stage7-smoke",
            "name": "Stage 7 real reviewer smoke",
            "repository": "shanchaudary/Buildforme",
            "status": "active",
            "local_repository_root": str(repo),
        }
    )
    store.register_repository_binding(
        {
            "repository": "shanchaudary/Buildforme",
            "local_path": str(repo),
            "project_id": "stage7-smoke",
        }
    )
    engine = get_engine(force_reload=True)
    packet = engine.attach_to_packet(
        {
            "id": "pkt-stage7-real-smoke",
            "objective": "Independently review a small verified Python subtraction implementation.",
            "target_repository": "shanchaudary/Buildforme",
            "target_branch": "feature/stage7-real-review",
            "operating_mode": "REVIEW",
            "risk": "GREEN",
            "allowed_files": ["README.md", "math_util.py", "test_math_util.py"],
            "forbidden_files": [".env", "secrets/**"],
            "acceptance_criteria": [
                "subtract(5, 3) returns 2",
                "unit tests pass",
                "no file mutation during review",
            ],
        }
    )
    lease = engine.issue_run_lease(
        run_id="run-stage7-real-smoke",
        provider_id="glm",
        packet_id=packet["id"],
        actor="stage7-smoke",
    )
    store.save_constitution_lease(lease)
    run = {
        "id": "run-stage7-real-smoke",
        "project_id": "stage7-smoke",
        "task_id": "stage7-real-review",
        "packet_id": packet["id"],
        "packet": packet,
        "provider_id": "glm",
        "repository": "shanchaudary/Buildforme",
        "repository_local_path": str(repo),
        "baseline_ref": baseline,
        "baseline_commit": baseline,
        "requested_target_branch": "feature/stage7-real-review",
        "execution_branch": "feature/stage7-real-review",
        "target_branch": "feature/stage7-real-review",
        "operating_mode": "REVIEW",
        "risk": "GREEN",
        "status": "needs_review",
        "execution_mode": "live_supervised",
        "mode": "live_supervised",
        "transport": "controlled_fixture",
        "requested_capabilities": ["read_repository", "run_tests"],
        "attempt": 0,
        "max_attempts": 1,
        "timeout_minutes": 30,
        "budget": {"max_cost_usd": 0},
        "review": {"hard_blocks": []},
        "worktree_path": str(repo),
        "evidence_ids": [],
        "controlled_implementation_fixture": True,
    }
    run = engine.attach_to_run(run, lease=lease, actor="stage7-smoke")
    run["scope_fingerprint"] = compute_run_scope_fingerprint(run, packet)
    run = store.save_run_for_setup(run)
    manifest = collect_changed_file_manifest(repo, baseline_commit=baseline)
    patch = collect_patch_evidence(repo, baseline_commit=baseline)
    evidence = build_evidence_bundle(
        run=run,
        packet=packet,
        process_result={
            "ok": True,
            "exit_code": 0,
            "pid": 1,
            "stdout": verify.stdout,
            "stderr": verify.stderr,
            "cleanup_ok": True,
            "process_group_isolated": True,
            "argv": ["controlled-fixture-verification"],
        },
        worktree={
            "worktree_path": str(repo),
            "baseline_commit": baseline,
            "head_commit": baseline,
            "branch": "feature/stage7-real-review",
        },
        diff={"manifest": manifest, "patch_fingerprint": patch["patch_fingerprint"]},
        provider_health={"version": "controlled-fixture", "executable": "controlled-fixture"},
        verification={"passed": True, "blocking_reasons": [], "checks": [{"name": "unittest", "status": "pass"}]},
        constitution_result={"passed": True},
        approved_baseline_sha=baseline,
        final_head_sha=baseline,
        execution_branch="feature/stage7-real-review",
        patch_fingerprint=patch["patch_fingerprint"],
        manifest_fingerprint=manifest["manifest_fingerprint"],
    )
    evidence = store.save_run_evidence(evidence)
    for provider_id in ("codex", "claude"):
        store.set_provider_constitution_ack(
            provider_id,
            {
                "constitution_supported": True,
                "constitution_acknowledged": True,
                "constitution_version": engine.version(),
                "constitution_hash": engine.content_hash(),
                "constitution_last_refresh": "stage7-smoke",
                "constitution_acknowledged_at": "stage7-smoke",
                "constitution_ack_actor": "stage7-smoke",
            },
        )
    created = create_independent_review_cycle(
        store,
        run["id"],
        reviewers=[
            {"reviewer_id": "codex-real-reviewer", "provider_id": "codex", "role": "correctness"},
            {"reviewer_id": "claude-real-reviewer", "provider_id": "claude", "role": "security"},
        ],
        actor="stage7-smoke",
    )
    attempts = []
    for assignment in created["assignments"]:
        execute_independent_review_assignment(
            store,
            created["cycle"]["cycle_id"],
            assignment["assignment_id"],
            actor=assignment["reviewer_id"],
            timeout_seconds=900,
        )
        attempts.extend(store.list_review_execution_attempts(assignment["assignment_id"]))
    finalized = aggregate_independent_review_cycle(
        store, created["cycle"]["cycle_id"], actor="stage7-smoke"
    )
    aggregate = finalized.get("aggregate") or {}
    observed = {
        "controlled_implementation_fixture": True,
        "review_execution_attempts": attempts,
        "distinct_provider_count": aggregate.get("distinct_provider_count"),
        "provider_ids": aggregate.get("provider_ids"),
        "aggregate_status": aggregate.get("status"),
        "verification_passed": True,
        "source_head_before": source_head_before,
        "source_head_after": git(repo, "rev-parse", "HEAD"),
        "source_branch_before": source_branch_before,
        "source_branch_after": git(repo, "rev-parse", "--abbrev-ref", "HEAD"),
        "source_patch_before": source_patch_before,
        "source_patch_after": collect_patch_evidence(repo, baseline_commit=baseline)["patch_fingerprint"],
        "merge_performed": False,
        "direct_report_submission_used": False,
    }
    acceptance = evaluate_stage7_smoke(observed)
    print("STAGE7_SMOKE_DIR", smoke_root)
    print("STAGE7_SMOKE_ACCEPTANCE_JSON", json.dumps(acceptance, sort_keys=True))
    print("MERGE no")
    return 0 if acceptance["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
'''
(ROOT / "scripts" / "stage7_real_two_provider_smoke.py").write_text(smoke_script, encoding="utf-8")


# ---------------------------------------------------------------------------
# Permanent tests/contracts.
# ---------------------------------------------------------------------------
test = r'''from __future__ import annotations

import ast
import unittest
from pathlib import Path

from buildforme.cli import build_parser
from buildforme.stage7_smoke import evaluate_stage7_smoke


class Stage7OperatorSurfaceTests(unittest.TestCase):
    def test_server_repair_routes_are_founder_gated_and_fixed(self):
        source = Path("buildforme/server.py").read_text(encoding="utf-8")
        self.assertIn("_stage7_repair_action", source)
        self.assertIn("_require_founder_mutation(payload)", source)
        for route in (
            "/repair-packet",
            "/admit",
            "/review-cycle",
            "/execute",
        ):
            self.assertIn(route, source)
        for forbidden in ("argv", "repo_root", "reviewers", "seed_commit", "scope_fingerprint"):
            self.assertIn(f'"{forbidden}"', source)

    def test_cli_exposes_repair_workflow_without_authority_overrides(self):
        parser = build_parser()
        commands = (
            ["repair-list"],
            ["repair-show", "rpair-test"],
            ["repair-create", "rc-test", "--provider", "glm"],
            ["repair-admit", "rpair-test"],
            ["repair-review-cycle", "rpair-test"],
            ["repair-execute", "rpair-test"],
        )
        for argv in commands:
            args = parser.parse_args(argv)
            self.assertTrue(callable(args.func))
        source = Path("buildforme/cli.py").read_text(encoding="utf-8")
        for unsafe in ("--repo-root", "--command", "--seed-commit", "--reviewers"):
            self.assertNotIn(unsafe, source)

    def test_browser_has_stage7_status_and_founder_inputs(self):
        html = Path("public/index.html").read_text(encoding="utf-8")
        js = Path("public/app.js").read_text(encoding="utf-8")
        for token in (
            'data-view="repairs"',
            'data-view-panel="repairs"',
            'id="rp-founder-token"',
            'id="rp-csrf-token"',
            'id="rp-list"',
        ):
            self.assertIn(token, html)
        self.assertIn("repairMutationBody", js)
        self.assertIn("/api/repair-packets", js)
        self.assertNotIn("localStorage.setItem", js)

    def test_smoke_acceptance_requires_real_two_provider_proof(self):
        attempt = lambda provider: {
            "provider_id": provider,
            "status": "succeeded",
            "process_started": True,
            "auth_probe_verified": True,
            "post_snapshot_proven": True,
            "worktree_unchanged": True,
            "process": {"pid": 10, "exit_code": 0, "cleanup_ok": True},
        }
        observed = {
            "controlled_implementation_fixture": True,
            "review_execution_attempts": [attempt("codex"), attempt("claude")],
            "distinct_provider_count": 2,
            "provider_ids": ["codex", "claude"],
            "aggregate_status": "clear",
            "verification_passed": True,
            "source_head_before": "a",
            "source_head_after": "a",
            "source_branch_before": "feature/x",
            "source_branch_after": "feature/x",
            "source_patch_before": "p",
            "source_patch_after": "p",
            "merge_performed": False,
            "direct_report_submission_used": False,
        }
        result = evaluate_stage7_smoke(observed)
        self.assertTrue(result["passed"], result)
        observed["review_execution_attempts"] = [attempt("codex")]
        result = evaluate_stage7_smoke(observed)
        self.assertFalse(result["passed"])
        self.assertIn("codex_and_claude_succeeded", result["failed_checks"])

    def test_smoke_script_discloses_controlled_fixture_and_no_merge(self):
        source = Path("scripts/stage7_real_two_provider_smoke.py").read_text(encoding="utf-8")
        self.assertIn('"controlled_implementation_fixture": True', source)
        self.assertIn("execute_independent_review_assignment", source)
        self.assertIn("STAGE7_SMOKE_ACCEPTANCE_JSON", source)
        self.assertIn('print("MERGE no")', source)
        tree = ast.parse(source)
        direct_submit = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "submit_review_report_atomic"
        ]
        self.assertEqual(direct_submit, [])


if __name__ == "__main__":
    unittest.main()
'''
(ROOT / "tests" / "test_stage7_packet7e_operator_surfaces.py").write_text(test, encoding="utf-8")

path = ROOT / "docs" / "STAGE_7_INDEPENDENT_MULTI_AGENT_REVIEW.md"
text = path.read_text(encoding="utf-8")
text += '''\n\n## Packet 7E — operator surfaces and real reviewer smoke\n\n- Founder-authenticated HTTP actions create repair packets, admit exact-seed children, execute approved repairs, and open mandatory fresh review cycles. The API rejects command, path, scope, reviewer, seed, and policy overrides.\n- Local CLI commands expose the same governed repair workflow without adding a second authority.\n- The dashboard adds a Stage 7 review/repair status panel with in-memory founder token and CSRF inputs; the page does not persist credentials.\n- `scripts/stage7_real_two_provider_smoke.py` runs real Codex and Claude blind reviewer processes against a disposable, deterministically verified implementation fixture. The output explicitly discloses that the implementation is controlled rather than claiming a third-provider execution. Acceptance requires two real authenticated process records, a clear two-provider aggregate, unchanged source identity/patch, no direct report submission, and no merge.\n'''
path.write_text(text, encoding="utf-8")

print("Stage 7 Packet 7E operator surfaces applied")
