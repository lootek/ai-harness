// ai-harness safe-command gate for opencode (v2 plugin API).
//
// opencode 2.x plugins are modules with a default export
//   { id: string, setup(ctx) }          (promise flavour; "effect" is the
//                                        Effect-returning twin)
// and register hooks on ctx.<domain>.hook(name, cb). Verified live against
// opencode 2.0.20 (2026-10-03):
//
//   ctx.tool.hook("execute.before", cb)
//     cb input: { tool: "shell", sessionID: "ses_...", agent, messageID,
//                 id: "<call id>", input: { command: "<cmd>" } }
//     throwing from cb blocks the call; the message reaches the model.
//     (the tool is named "shell" in v2, "bash" in v1; both are matched.)
//
//   ctx.permission.hook("evaluate", cb)
//     cb input: { sessionID, agent, action: "shell", resources: [<cmd>],
//                 source: { type: "tool", id: "<call id>" }, effect, message }
//     setting effect = "deny" (+ message) blocks the call.
//
// Both are used: the before-hook runs the engine and throws; the permission
// hook is a second, independent layer that denies any tool-originated shell
// call the before-hook did not explicitly allow (e.g. if hook ordering ever
// changes). User-typed shell (no tool source) is not touched.
//
// The verdict comes from the shared engine
// $AI_HARNESS_HOME/hooks/safe_command.py (default ~/.ai-harness), spawned as
// a subprocess and fed a Claude-Code-shaped PreToolUse envelope, so the exact
// same rule set (and the shared audit log, via $AI_SAFE_COMMAND_AUDIT) applies
// to claude/codex/opencode. ASK keeps its own label here.
// Fail-closed: spawn failure, non-zero exit, unparseable output, or a 2s
// timeout all block the command.

import { spawn } from "node:child_process"
import { join } from "node:path"
import { homedir } from "node:os"

const HARNESS_HOME = process.env.AI_HARNESS_HOME ?? join(homedir(), ".ai-harness")
const ENGINE = join(HARNESS_HOME, "hooks", "safe_command.py")
const TIMEOUT_MS = 2000
const SHELL_TOOLS = new Set(["shell", "bash"])

type Verdict = { decision: "allow" | "deny" | "ask"; reason: string }

function runEngine(envelope: string): Promise<string> {
  return new Promise((resolve, reject) => {
    const proc = spawn("python3", [ENGINE], { stdio: ["pipe", "pipe", "pipe"] })
    let out = ""
    const timer = setTimeout(() => {
      proc.kill("SIGKILL")
      reject(new Error(`engine timed out after ${TIMEOUT_MS}ms`))
    }, TIMEOUT_MS)
    proc.stdout.on("data", (d) => (out += d))
    proc.on("error", (e) => {
      clearTimeout(timer)
      reject(e)
    })
    proc.on("close", (code) => {
      clearTimeout(timer)
      code === 0 ? resolve(out) : reject(new Error(`engine exited ${code}`))
    })
    proc.stdin.on("error", () => {})
    proc.stdin.end(envelope)
  })
}

async function verdict(command: string, sessionID: string, cwd: string): Promise<Verdict> {
  const envelope = JSON.stringify({
    session_id: sessionID,
    transcript_path: "",
    cwd,
    hook_event_name: "PreToolUse",
    tool_name: "Bash",
    tool_input: { command },
  })
  const text = (await runEngine(envelope)).trim()
  if (!text) return { decision: "allow", reason: "" } // engine's silent default
  const out = JSON.parse(text)?.hookSpecificOutput
  const decision = out?.permissionDecision
  if (decision !== "allow" && decision !== "deny" && decision !== "ask")
    throw new Error("unexpected engine envelope")
  return { decision, reason: out?.permissionDecisionReason ?? "" }
}

// call ids the before-hook explicitly allowed (consumed by the permission layer)
const allowed = new Set<string>()

export default {
  id: "ai-harness.safe-command",
  setup: async (ctx: any) => {
    const cwd: string = ctx?.location?.directory ?? process.cwd()

    await ctx.tool.hook("execute.before", async (ev: any) => {
      if (!SHELL_TOOLS.has(ev.tool)) return
      const command = ev.input?.command
      if (typeof command !== "string" || !command) {
        // a shell call we cannot read is a shell call we cannot vet
        throw new Error("[ai-harness] DENY: safe_command could not read the command")
      }
      let v: Verdict
      try {
        v = await verdict(command, String(ev.sessionID ?? ""), cwd)
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err)
        throw new Error(`[ai-harness] DENY: safe_command unavailable (${msg})`)
      }
      if (v.decision !== "allow")
        throw new Error(`[ai-harness] ${v.decision.toUpperCase()}: ${v.reason}`)
      allowed.add(String(ev.id))
    })

    await ctx.permission.hook("evaluate", (ev: any) => {
      if (!SHELL_TOOLS.has(ev.action) || ev.source?.type !== "tool") return
      const id = String(ev.source.id)
      if (allowed.has(id)) return
      ev.effect = "deny"
      ev.message = "[ai-harness] DENY: shell call was not vetted by safe_command"
    })
  },
}
