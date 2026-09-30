---
name: review-board
description: "Use when a document, plan, RFC, or MR/code needs multi-persona review. Trigger phrases: 'review board', 'run the reviewers', 'get reviews on <file>'."
allowed-tools:
  - Read
  - Write
  - Bash
  - Agent
  - AskUserQuestion
---

## Running on other CLIs

On opencode: `Agent` → `task`, `AskUserQuestion` → `ask`; reviewers go through `task` sequentially or in batches. On codex: no user-defined subagents and no interactive-prompt tool — run the reviewers as sequential passes in the main agent, print any options and STOP, and act only on the user's next reply.

# Review Board

Orchestrates a multi-persona review of a document. Each reviewer is a subagent that produces an independent, opinionated review. The main agent then synthesizes the verdicts into a single `review_synthesis.md`.

After synthesis, two further stages run **by default**:

- **Debate** (§8) — a perspective gremium (Skeptic / Enthusiast / Pragmatic / Fact-Checker) re-litigates the converged blockers across two rounds, so contested findings are stress-tested rather than arbitrated by the lead alone. Output: `review_debate.md`. Disable with `--no-debate`.
- **Autonomous remediation** (§9) — for every critical/blocker-level finding, a remediation-architect subagent runs in **plan mode** and designs one consolidated solution for your approval *before* any change is made. Disable with `--no-fix`.

Reviewer selection is **auto-proposed from the detected artifact kind** (docs / source / infra / mixed). You still confirm — the skill suggests a roster and lets you drop or add anyone (§3).

## Arguments

The skill takes a path to the **artifact under review** as its first positional arg. The artifact can be:

- A **single file** (`.md`, `.rst`, `.txt`, `.py`, `.go`, `.tf`, etc.) — reviewed directly.
- A **directory** — reviewed as a whole: every file within is in scope (recursively). Reviewers should treat the directory as the unit of review (a project, an RFC folder, a PR worktree, a plan + supporting docs) and read whatever files they need to form a verdict.

Examples:
- `/review-board path/to/doc.md`
- `/review-board path/to/project/` — review the whole directory tree
- `/review-board path/to/doc.md --out /tmp/reviews` — override output directory.
- `/review-board path/to/doc.md --no-debate` — skip the debate stage (§8).
- `/review-board path/to/doc.md --no-fix` — skip autonomous remediation (§9).
- `/review-board path/to/doc.md --yes` — accept the auto-proposed reviewer roster without the confirmation prompt (§3).

**Flags:**
- `--out <dir>` — override output directory.
- `--no-debate` — skip the debate gremium; synthesis is final.
- `--no-fix` — skip autonomous remediation; stop after debate.
- `--yes` — skip the roster confirmation prompt; run exactly the auto-proposed set.

**Natural-language switches.** The flags are also expressible in plain English in the invocation — parse the request and map intent to flags before doing anything else. Treat these as equivalent:
- "without debating" / "skip the debate" / "no debate round" / "don't argue it out" → `--no-debate`.
- "don't fix anything" / "review only" / "no remediation" / "just review, don't touch it" → `--no-fix`.
- "review only, no debate or fixes" / "just the reviews" → `--no-debate` **and** `--no-fix`.
- "write the reviews to <dir>" / "output to <dir>" → `--out <dir>`.

**`--yes` requires an explicit skip-confirmation phrase — never infer it from the invocation verb.** The word "run" (as in "run review-board over the doc", "run the reviewers", "run it on X") is just how the skill is invoked; it carries **no** opinion about the roster prompt. Map to `--yes` ONLY on an unambiguous skip-the-prompt phrase, e.g. "don't ask", "skip the prompt", "no questions", "just go ahead without asking", "accept the proposed roster". If such a phrase is absent, `auto_yes` is **false** and step 3a's confirmation gate MUST run. When in doubt, do NOT set `--yes` — show the gate.

When intent is ambiguous (e.g. "quick review"), do not guess silently — proceed with defaults, **run the step-3a confirmation gate**, and state in one line which stages will run, so the user can interrupt.

**Default output directory:** `./review-board-<YYYYMMDD-HHMMSS>/` relative to the current working directory (timestamp in local time). Create it if missing. This keeps each run's reviews isolated and avoids clobbering prior runs.

**Rejection rules:**
- File path: reject if the file does not exist or is empty.
- Directory path: reject if the directory does not exist, is empty, or contains no readable files.
- Also reject if the target is the same as, or contained by, `<out-dir>` (prevents reviewers re-reading their own output).

## Available reviewers

Each reviewer is a subagent defined under `agents/reviewer-*.md`. Currently shipped:

| subagent_type | Persona | One-liner |
|---|---|---|
| `reviewer-security` | Security Engineer | Secrets-management posture, audit trail, blast radius, least privilege. |
| `reviewer-terraform` | Terraform / IaC Architect | Provider upgrade hygiene, state/plan-leak risk, ordering, rollback. |
| `reviewer-skeptic` | Skeptic / Migration-Ops | Challenges premise, per-phase operational risk, rollback rigor. |
| `reviewer-enthusiast` | Enthusiast | Optimistic counterweight: what's good here, hidden upside, cost of not shipping. |
| `reviewer-pragmatic` | Pragmatic / seasoned engineer | Weighs tradeoffs, cuts yak-shaving, asks "smallest thing that works?". |
| `reviewer-golang` | Go expert | Idiomatic Go, concurrency, error handling, testing, module hygiene. |
| `reviewer-python` | Python expert | PEP 8/20/257, typing, packaging, async, testing, common traps. |
| `reviewer-web` | Web / UI-UX expert | Frontend architecture, accessibility, performance, UX flow. |
| `reviewer-cloud-architect` | Cloud architect & K8s expert | Cluster topology, workload patterns, isolation, scaling. |
| `reviewer-cloud-sre` | Cloud SRE (AWS/Azure/GCP) | Reliability, IAM, cost, observability, on-call readiness. |
| `reviewer-devops` | DevOps / DevSecOps | CI/CD pipelines, release mechanics, supply chain, secret-scanning gates. |
| `reviewer-ai` | AI expert | LLM/agent design, prompt/context engineering, evals, safety, cost, model choice, Claude Code primitives. |
| `reviewer-cybersec-ai` | Cybersec + AI analyst | AI/LLM security, modern cloud sec, tooling trends, threat intel — grounded in TL;DR Sec and Daniel Miessler's UL. |
| `reviewer-vuln-db` | Vulnerability DB analyst | CVE-level precision: NVD, CISA KEV, CVSS/EPSS, affected versions, exploitation status, patch availability. |
| `reviewer-redteam` | Red Teamer / Offensive Security | Adversarial review: kill chains, attack paths, OWASP/ATT&CK abuse cases, control bypasses. |
| `reviewer-iam` | IAC/IAM Expert | Identity & access — grounded in FusionAuth articles: OAuth/OIDC, SAML, JWTs, sessions, MFA, passwordless/passkeys, SSO, federation, token storage, AI agent identity. |
| `reviewer-secrets-mgmt` | Secrets Management Expert | Secrets lifecycle — grounded in Infisical/GitGuardian/Doppler/Akeyless/Vault/OWASP/NIST: storage backends, dynamic secrets, rotation, secret zero, K8s/CSI, CI/CD, PKI, NHI, MCP/AI agents, leak detection. |
| `reviewer-fact-checker` | Fact-Checker | Asserts credibility of factual claims — verifies stats, citations, version numbers, CVEs, quotes, and historical events against authoritative sources; flags Unsupported/Misleading/False/Stale claims. |

## Orchestrator flow

The main agent (the one running this skill) follows these steps in order. Do not skip.

### 1. Validate args

- **Parse flags first**, accepting both the explicit forms and the natural-language switches described under "Arguments". Resolve to three booleans + one path: `debate` (default true), `fix` (default true), `auto_yes` (default false), and `out-dir`. If the invocation is plain English, map intent to these before continuing.
- Resolve the artifact path to an absolute path. Detect whether it is a **file** or a **directory**. Call this the `<target>` from here on.
- For a file: reject if missing or empty.
- For a directory: reject if missing; run a quick listing (`find <target> -type f`) to confirm it contains at least one readable file; skip noise (`.git/`, `node_modules/`, `.venv/`, build artifacts, lockfiles beyond a reasonable limit) when forming the inventory but let reviewers decide what to read.
- Determine `<out-dir>`:
  - If `--out` is provided, use that (resolved to absolute).
  - Otherwise, default to `./review-board-<YYYYMMDD-HHMMSS>/` relative to the current working directory. Generate the timestamp once (e.g. `date +%Y%m%d-%H%M%S`) and reuse it for all reviewers in this run.
- Create `<out-dir>` if missing. Confirm it is writable.
- Reject if `<target>` is the same as, or contained by, `<out-dir>` (prevents reviewers re-reading their own output).

### 2. Read the target once

- **File target:** read the full file into your own context for the synthesis step.
- **Directory target:** do not read every file. Build an inventory (`find <target> -type f`, note sizes and extensions) and skim the obvious entry points (top-level `README.md`, `PLAN.md`, `RFC.md`, `main.tf`, `main.go`, `pyproject.toml`, etc.) so you have enough context to synthesize verdicts later. For large trees, capture the file inventory as a string you can reference.

**Do not** paste contents into subagent prompts — pass the `<target>` path and the file inventory (for directories) and let each subagent read whatever it needs independently.

### 2.5. Detect artifact kind and auto-propose a roster

Classify the target into one of four **kinds** from extensions/content, then pre-select a default roster. This is a proposal — step 3 lets the user adjust.

**Kind detection** (for a directory, classify by the dominant file population; if it's a genuine mix of prose docs *and* source/infra, use `mixed`):

| Kind | Signals | Examples |
|---|---|---|
| `docs` | Prose: `.md`, `.rst`, `.txt`, `.adoc`; design docs, RFCs, plans, runbooks | `PLAN.md`, `RFC.md`, a doc folder |
| `source` | Application code: `.go`, `.py`, `.js`/`.ts`, `.java`, `.rb`, `.rs`, etc. | a service repo, a PR worktree |
| `infra` | IaC / pipeline / config: `.tf`, `.tfvars`, `.yaml`/`.yml` k8s manifests, `Dockerfile`, `.gitlab-ci.yml`, Helm charts | a terraform module |
| `mixed` | A meaningful blend of `docs` + (`source` or `infra`) | a project dir with README + code |

**Default roster by kind.** Always-on baseline for every kind: **Pragmatic**, **Security Engineer**. Then add:

| Kind | Add to baseline |
|---|---|
| `docs` | **Category 1 — Perspective in full** (Skeptic, Enthusiast, Pragmatic, Fact-Checker) + AI expert |
| `source` | Language reviewer(s) matching the detected language(s) (Go / Python / Web), DevOps, Red Teamer. **Do NOT auto-add Category 1 — Perspective** beyond the Pragmatic baseline (perspective reviewers debate *premises*, which is doc-shaped; for raw source they mostly add noise). Skeptic only if the source includes a migration/rollout. |
| `infra` | Terraform, Cloud architect & K8s, Cloud SRE, DevOps, Secrets Management Expert |
| `mixed` | Union of the matching `docs` + `source`/`infra` defaults |

The deeper security tracks (Cybersec+AI, Vuln-DB, IAM, Secrets-Mgmt) are **content-triggered**: auto-add when the artifact mentions their domain (e.g. secrets/PKI → Secrets-Mgmt; auth/OIDC/SAML/JWT → IAM; CVE/dependency versions → Vuln-DB). When unsure, leave them out of the default but list them as easy adds in step 3.

Record the detected `<kind>` and the proposed roster (as a set of reviewer labels) for the next step.

### 3. Confirm the roster (auto-proposed, user adjusts)

**The confirmation gate (3a) is mandatory unless `auto_yes` is true.** `auto_yes` is true ONLY when the user passed `--yes` or an explicit skip-the-prompt phrase (see "Arguments" — the invocation verb "run" does NOT qualify). If you are not certain `auto_yes` was explicitly requested, treat it as false and run the gate.

If `auto_yes` is set, **skip all prompts** and use the proposed roster from step 2.5 verbatim.

Otherwise, **confirm first — do not dump the full picker by default.** `AskUserQuestion` options always render as *unchecked* boxes (there is no pre-check / default-selected capability), so showing the seven-category checklist up front would force the user to re-tick a roster you already proposed — defeating the autoselection. Avoid that.

**3a. Single confirmation gate.** Print the detected `<kind>` and the proposed roster as an explicit list (one line per reviewer, with a one-clause reason each). Then issue **one** `AskUserQuestion` (single-select, not multi) with header `Roster`, question "Run this proposed roster?":
  - **Run it** — accept the proposed roster verbatim → skip to step 4. *(List this option first; it is the recommended path.)*
  - **Edit it** — open the full per-category picker (3b) so I can drop/add anyone.
  - **Cancel** — abort the run.

The user can also just answer in free text ("drop the enthusiast", "add the red teamer") via the "Other" field — if they do, apply the delta to the proposed roster and re-confirm with a one-line summary rather than opening the full picker.

**3b. Full picker (only if the user chose "Edit it").** Now run the seven-category checklists below. Because boxes start unchecked, **state the proposed set in the question text** for each category (e.g. "Proposed: Skeptic, Enthusiast, Pragmatic, Fact-Checker — adjust as needed") so the user knows the baseline they're editing. The union of their selections is the roster.

The reviewer roster is split into **6 categories** (using a 5a/5b split for the two security tracks), each with ≤4 reviewers. `AskUserQuestion` caps options at 4 per question and questions at 4 per call — so issue **two `AskUserQuestion` tool calls in sequence**: the first containing 3 questions (categories 1–3), the second containing 3 questions (categories 5a, 5b, 6). Each question is `multiSelect: true`.

**Category 1 — Perspective (4):**
  - Skeptic / Migration-Ops — Challenges premise, per-phase operational risk, rollback rigor.
  - Enthusiast — Optimistic counterweight: what's good here, hidden upside, cost of not shipping.
  - Pragmatic / seasoned engineer — Weighs tradeoffs, cuts yak-shaving, smallest-thing-that-works.
  - Fact-Checker — Asserts credibility of factual claims; verifies stats, citations, version numbers, CVEs, quotes, and historical events against authoritative sources.

**Category 2 — Languages & Frontend (3):**
  - Go expert — Idiomatic Go, concurrency, error handling, testing, module hygiene.
  - Python expert — PEP 8/20/257, typing, packaging, async, testing, common traps.
  - Web / UI-UX expert — Frontend architecture, accessibility, performance, UX flow.

**Category 3 — Infra & Cloud (4):**
  - Terraform / IaC Architect — Provider upgrade hygiene, state/plan-leak risk, ordering, rollback.
  - Cloud architect & K8s expert — Cluster topology, workload patterns, isolation, scaling.
  - Cloud SRE (AWS/Azure/GCP) — Reliability, IAM, cost, observability, on-call readiness.
  - DevOps / DevSecOps — CI/CD pipelines, release mechanics, supply chain, secret-scanning gates.

**Category 5a — Security I (3):**
  - Security Engineer — Secrets-management posture, audit trail, blast radius, least privilege.
  - Cybersec + AI analyst — AI/LLM security, modern cloud sec, tooling trends (TL;DR Sec + Daniel Miessler's UL).
  - Vulnerability DB analyst — CVE-level precision: NVD, CISA KEV, CVSS/EPSS, affected versions, patches.

**Category 5b — Security II (3):**
  - Red Teamer / Offensive Security — Adversarial review: kill chains, attack paths, OWASP/ATT&CK abuse cases, control bypasses.
  - IAC/IAM Expert — Identity & access (FusionAuth-grounded): OAuth/OIDC, SAML, JWTs, sessions, MFA, passwordless/passkeys, SSO, federation, token storage, AI agent identity.
  - Secrets Management Expert — Secrets lifecycle (Infisical/GitGuardian/Vault/OWASP/NIST-grounded): storage backends, dynamic secrets, rotation, secret zero, K8s/CSI, CI/CD, PKI, NHI, MCP/AI agents, leak detection.

**Category 6 — General (1):**
  - AI expert — LLM/agent design, prompt/context engineering, evals, safety, cost, model choice, Claude Code primitives.

**Prompt flow (3b — full picker only):**

First `AskUserQuestion` call — 3 questions, each `multiSelect: true`:
1. Header `Perspective`, question "Which perspective reviewers?" → options from Category 1.
2. Header `Languages`, question "Which language/frontend reviewers?" → options from Category 2.
3. Header `Infra`, question "Which infra & cloud reviewers?" → options from Category 3.

Second `AskUserQuestion` call — 3 questions, each `multiSelect: true`:
5. Header `Security I`, question "Which security reviewers (set 1)?" → options from Category 5a.
6. Header `Security II`, question "Which security reviewers (set 2)?" → options from Category 5b.
7. Header `General`, question "Which general reviewers?" → options from Category 6.

After both calls return, union the selections across all six categories. If the union is empty, abort with a short message. Otherwise proceed.

Map each chosen label back to its `subagent_type`:

| Label | subagent_type | Output filename |
|---|---|---|
| Security Engineer | `reviewer-security` | `review_security.md` |
| Terraform / IaC Architect | `reviewer-terraform` | `review_terraform.md` |
| Skeptic / Migration-Ops | `reviewer-skeptic` | `review_skeptic.md` |
| Enthusiast | `reviewer-enthusiast` | `review_enthusiast.md` |
| Pragmatic / seasoned engineer | `reviewer-pragmatic` | `review_pragmatic.md` |
| Go expert | `reviewer-golang` | `review_golang.md` |
| Python expert | `reviewer-python` | `review_python.md` |
| Web / UI-UX expert | `reviewer-web` | `review_web.md` |
| Cloud architect & K8s expert | `reviewer-cloud-architect` | `review_cloud_architect.md` |
| Cloud SRE (AWS/Azure/GCP) | `reviewer-cloud-sre` | `review_cloud_sre.md` |
| DevOps / DevSecOps | `reviewer-devops` | `review_devops.md` |
| AI expert | `reviewer-ai` | `review_ai.md` |
| Cybersec + AI analyst | `reviewer-cybersec-ai` | `review_cybersec_ai.md` |
| Vulnerability DB analyst | `reviewer-vuln-db` | `review_vuln_db.md` |
| Red Teamer / Offensive Security | `reviewer-redteam` | `review_redteam.md` |
| IAC/IAM Expert | `reviewer-iam` | `review_iam.md` |
| Secrets Management Expert | `reviewer-secrets-mgmt` | `review_secrets_mgmt.md` |
| Fact-Checker | `reviewer-fact-checker` | `review_fact_check.md` |

If the user selects zero reviewers, abort with a short message.

### 4. Spawn selected reviewers in parallel

Send a **single assistant message** containing one `Agent` tool call per selected reviewer. Do NOT serialize them. (On opencode the reviewers go through `task` sequentially or in batches; on codex, which has no subagents, run them as sequential passes in the main agent — same prompts, same output files.)

Prompt template for each (substitute `<role>`, `<abs-target-path>`, `<target-kind>`, `<abs-out-path>`, and — for directories — `<file-inventory>`):

> You are reviewing the **<target-kind>** at `<abs-target-path>`.
>
> - If it is a **file**, read it and review its contents.
> - If it is a **directory**, treat the whole tree as the unit of review. A file inventory is provided below. Read whichever files are relevant to your scope (READMEs, plans/RFCs, source files, config, tests). You are not required to read every file; focus on what your persona cares about. Note in your review which files you relied on.
>
> Produce your review following the format in your agent definition. Write it to `<abs-out-path>`. When you reply to me, include ONLY two lines:
> - `verdict: <one-line verdict>`
> - `path: <absolute path you wrote>`
>
> Do not write to any other path. Do not include the review body in your reply — it goes in the file.
>
> File inventory (directory targets only):
> ```
> <file-inventory>
> ```

### 5. Collect verdicts

Each subagent returns a short response with its verdict + path. Record them.

### 6. Synthesize in the main agent

Read each `review_<role>.md` the subagents wrote. Produce `<out-dir>/review_synthesis.md` matching this structure:

```markdown
# Plan Review Synthesis — <doc title>

<N> reviewers. <verdict distribution one-liner>.
<Direction/bottom-line one-liner>.

---

## Verdicts

| Reviewer | Verdict |
|---|---|
| <persona> | <verdict + short count of blockers/must-fixes> |
| ... | ... |

---

## Blockers converged across ≥2 reviewers

### B1. <title> (<which reviewers>)
<body — what the blocker is, options to resolve, required action>

### B2. ...

---

## Must-fixes (single reviewer but substantive)

| # | Issue | From | Action |
|---|---|---|---|
| M1 | ... | ... | ... |

---

## Decisions still open

1. ...

---

## Coordination required

- ...

---

## Bottom line

<2–4 sentence synthesis: is the plan ready? what is the single biggest open question?>
```

Guidelines for synthesis:
- A blocker goes in **B-section** only if ≥2 reviewers flagged the same underlying issue (even if under different names).
- Single-reviewer findings go in the **M-table**, not the B-section.
- Preserve reviewer-specific citations (e.g., `(SEC C1)`, `(TF I2)`) in the B/M entries so the reader can trace back.
- Keep the bottom line ruthless: what's the one unresolved question?
- **Tag severity** on every B-entry and M-row with one of `[critical]` / `[blocker]` / `[major]` / `[minor]`. The debate (§8) and remediation (§9) stages key off these tags: only `[critical]` and `[blocker]` findings are auto-remediated. Every B-entry is at least `[blocker]` by definition (≥2 reviewers converged); escalate to `[critical]` when a reviewer called out data-loss, security exposure, or irreversibility.

### 7. Decide which downstream stages run

- If `debate` is true → run **§8 Debate**, then re-read the (possibly updated) synthesis.
- If `fix` is true → run **§9 Autonomous remediation**.
- Then proceed to **§10 Report**.

If both are false, skip straight to §10.

### 8. Debate the contested blockers (default on; `--no-debate` to skip)

The synthesis lead arbitrates convergence alone, which can rubber-stamp a plausible-but-wrong blocker or wave off a real one. The debate stage hands the converged blockers to a **perspective gremium** that argues them out across two rounds. This reuses Category 1 — Perspective: **Skeptic**, **Enthusiast**, **Pragmatic**, **Fact-Checker** (the same `subagent_type`s). If the user dropped any of them from the roster in step 3, still convene the full four for the debate unless they passed `--no-debate` — the debate panel is fixed, independent of the review roster.

**Scope of debate:** every `[critical]` and `[blocker]` finding from §6 (the B-section, plus any M-row tagged that high). Skip `[major]`/`[minor]` — debating those is not worth the tokens.

**Round 1 — independent positions (parallel).** Spawn all four panelists in a single message. Each receives: the `<target>` path, the absolute path to `review_synthesis.md`, and the list of contested finding IDs. Prompt each to read the synthesis + relevant artifact and write an opening position to `<out-dir>/debate_r1_<persona>.md`: for each contested finding, take a stance — **uphold / downgrade / dismiss / escalate** — with a one-paragraph argument grounded in the artifact (not vibes). Reply with only `path: <abs path>`.

**Round 2 — rebuttal (parallel).** Spawn the same four again, each given the paths to **all four** Round-1 position files plus the synthesis. Prompt: read the other three panelists' positions, then write a rebuttal to `<out-dir>/debate_r2_<persona>.md` — where do you concede, where do you hold, and *why*? Each panelist must explicitly address at least one point they disagree with. Reply with only `path: <abs path>`.

**Convergence.** The lead reads all eight debate files and writes `<out-dir>/review_debate.md`:

```markdown
# Debate — <doc title>

Panel: Skeptic, Enthusiast, Pragmatic, Fact-Checker. 2 rounds.

## Per-finding outcome

### <FID> — <title>
- **Going in:** <severity + one-line claim from synthesis>
- **Positions:** Skeptic <stance>; Enthusiast <stance>; Pragmatic <stance>; Fact-Checker <stance>
- **Where they landed:** <consensus or persistent disagreement, 2–3 sentences>
- **Revised severity:** `[critical|blocker|major|minor|dismissed]` (<why changed, if it changed>)

## Net effect on the synthesis
- Upheld: <FIDs>
- Downgraded: <FIDs → new severity>
- Dismissed: <FIDs + reason>
- Escalated: <FIDs → new severity>
```

Then **patch `review_synthesis.md`** to reflect the revised severities: update each affected finding's severity tag and append a short `> Debate: <outcome>` note under it. This revised synthesis is what §9 consumes. Do not delete dismissed findings — re-tag them `[dismissed]` with the debate rationale so the trail is auditable.

### 9. Autonomously address critical/blocker findings (default on; `--no-fix` to skip)

For findings that survive debate at `[critical]` or `[blocker]` severity, design **one consolidated fix** for the user's approval before touching anything. This mirrors the ralph-loop pattern in `tech-doc-assist` but with a mandatory plan-mode gate: the remediation architect plans, you approve, *then* it executes.

**9a. Select the work.** From the (debate-revised) synthesis, collect every `[critical]`/`[blocker]` finding. If none survive, print "No critical/blocker findings to remediate." and skip to §10.

**9b. Plan in plan mode (single architect subagent).** Spawn **one** `reviewer-pragmatic` subagent (it already has the seasoned-engineer lens and is read-capable) in **plan mode** — pass `mode: "plan"` to the Agent tool so it cannot edit. Prompt it with: the `<target>` path, the synthesis path, the debate path (if any), and the list of findings to resolve. Instruct it to:
   - Read the artifact and each finding.
   - Design a **single consolidated remediation** covering all of them — not one patch per finding in isolation, but a coherent set of edits that resolves them together without contradiction.
   - Surface ordering, coordination needs, and any finding it judges *should not* be auto-fixed (e.g. needs a human decision, crosses repo boundaries, or requires a Jira ID per the workspace rules).
   - Write the plan to `<out-dir>/remediation_plan.md` and return its path. It must NOT make edits — it is in plan mode.

**9c. Present for approval.** Show the user the consolidated plan (path + a tight summary: what will change, in which files, and what is deliberately *excluded* and why). Ask for explicit approval via `AskUserQuestion`:
   - **Approve & execute** — proceed to 9d.
   - **Approve subset** — user names which findings to fix now; the rest are deferred.
   - **Revise** — collect feedback, re-run 9b with it appended.
   - **Skip remediation** — stop, leave the plan on disk for manual handling.

   Never edit the artifact without an explicit approve. This is a hard gate.

**9d. Execute (only after approval).** Apply the approved plan. If the target is a **git repo and edits change code**, honor the workspace rules: ask for the Jira ID before changing tracked code, branch as `<JIRA-ID>-<short-name>`, one commit per issue/fix via a foreground sub-agent `cd`'d to the repo, and obey the GPG-signing mandate. For a plain doc target (not under git, or a scratch file), edit in place and note the diff. After executing, write `<out-dir>/remediation_result.md`: per finding — `done` / `partial` / `deferred` + what changed (file:line) or why deferred.

### 10. Report to the user

Print, in this order:
1. The verdict table.
2. If debate ran: a one-line debate delta (e.g. "Debate: 3 upheld, 1 downgraded, 1 dismissed").
3. If remediation ran: per-finding `done`/`partial`/`deferred` status and the branch/commit refs (if a repo).
4. The absolute paths to `review_synthesis.md`, `review_debate.md` (if any), and `remediation_plan.md` / `remediation_result.md` (if any).

Nothing else. No running commentary on what you just did.

## Posting reviews as inline MR comments

When the user asks to post review findings **on a GitLab MR** (not just write the synthesis file), each finding MUST be posted as an **inline comment tied to a source line** — a position-anchored discussion, never a plain MR-level note. Follow this exactly; it encodes hard-won gotchas.

### Hard rules

1. **Prefix every comment body with a single line `[<model name>]`** (e.g. `[Claude Opus]`), followed by a blank line, then the finding. So the author knows it's AI-authored.
2. **Send a raw JSON body via `--input -`. Do NOT use `glab api -f 'position[...]=...'`.** The `-f` flag flattens the `position[...]` bracket keys and silently drops the position object — the comment posts as a plain discussion note with no line anchor. This is the #1 failure mode.
3. **Verify each post landed inline:** the response's first note must have `position != null` (`has_position=True`). If it's null, the anchor was dropped — delete and retry.
4. **Verify file/line against the MR head commit first**, not your local working copy — the MR may have been rebased (`git fetch origin merge-requests/<iid>/head:mr-<iid> --force`, then `git show mr-<iid>:<path>`).

### Required position fields

Fetch the MR's `diff_refs` once:

```bash
glab api "projects/<proj-url-encoded>/merge_requests/<iid>" | python3 -c "import json,sys; d=json.load(sys.stdin)['diff_refs']; print(d['base_sha'], d['start_sha'], d['head_sha'])"
```

Line-anchor rules (this trips people up):
- **Added line** (green `+` in diff): set `new_line` only, omit `old_line`.
- **Deleted line** (red `-`): set `old_line` only, omit `new_line`.
- **Unchanged context line:** GitLab needs a *valid line_code*, which requires **both** `old_line` and `new_line`. Posting a context line with only `new_line` fails with `400 ... line_code ... must be a valid line code`. Either map both line numbers, or move the anchor to a nearby changed line.

When commenting on the *consequence* of a deletion (e.g. a removed helper), anchor to the deleted line itself (`old_line`) rather than a downstream context line.

### Poster pattern

Write the comments to a JSON file in the session dir, then post with a small Python script (save-and-review, per workspace rules). Per comment:

```python
payload = {
    "body": f"[{MODEL_NAME}]\n\n{finding_md}",
    "position": {
        "position_type": "text",
        "base_sha": BASE, "start_sha": BASE, "head_sha": HEAD,
        "new_path": path, "old_path": path,
        "new_line": n,            # and/or "old_line": m per the rules above
    },
}
subprocess.run(["glab","api","--method","POST",
    "--header","Content-Type: application/json",
    f"projects/{PROJ}/merge_requests/{IID}/discussions",
    "--input","-"], input=json.dumps(payload), ...)
```

Then assert `json.loads(resp)["notes"][0]["position"]` is non-null for each.

### If you posted plain notes by mistake

Delete them before re-posting (don't leave duplicates): for each discussion, `glab api --method DELETE projects/<proj>/merge_requests/<iid>/notes/<note_id>`.

## Adding a new reviewer

To add a 5th persona (Staff Engineer, Product, Legal, etc.):

1. Create `agents/reviewer-<role>.md` following the skeleton of the existing ones (frontmatter with `name`, `description`, `model: sonnet`, `tools`; body with Scope / Verdict taxonomy / Output format / Response-to-caller sections).
2. Add a row to the "Available reviewers" table above.
3. Add an option to the `AskUserQuestion` checklist in step 3.
4. Add the label→`subagent_type`→filename mapping to step 3's table.
5. If the persona should be auto-proposed for a given artifact kind, add it to the default-roster table in step 2.5 (or to the content-triggered list).

No other changes needed. The orchestrator dispatches dynamically based on what the user selected.
