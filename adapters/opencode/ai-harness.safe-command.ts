// ai-harness safe-command gate for opencode.
//
// opencode has no Claude-Code-style hooks; plugins are the extension point.
// Verified live against opencode 1.18.30 (2026-09-29): the
// "tool.execute.before" hook receives
//   input:  { tool: "bash", sessionID: "ses_...", callID: "call_..." }
//   output: { args: { command: "<cmd>" } }
// and throwing an Error blocks the call — the message is shown to the model
// ("Command was blocked by the environment: ...").
//
// The verdict itself comes from the shared engine
// $AI_HARNESS_HOME/hooks/safe_command.py (default ~/.ai-harness), spawned as
// a subprocess and fed a Claude-Code-shaped PreToolUse envelope, so the exact
// same rule set (and the shared audit log, via $AI_SAFE_COMMAND_AUDIT) applies
// to claude/codex/opencode. Unlike codex, opencode surfaces "ask" verbatim:
// ASK keeps its own label here, so the user sees it was a judgment call.
// Fail-closed: spawn failure, non-zero exit, unparseable output, or a 2s
// timeout all block the command.

import { join } from "node:path"
import { homedir } from "node:os"
import type { Plugin } from "@opencode-ai/plugin"

const HARNESS_HOME = process.env.AI_HARNESS_HOME ?? join(homedir(), ".ai-harness")
const ENGINE = join(HARNESS_HOME, "hooks", "safe_command.py")
const TIMEOUT_MS = 2000

async function verdict(command: string, sessionID: string, cwd: string) {
  const envelope = JSON.stringify({
    session_id: sessionID,
    transcript_path: "",
    cwd,
    hook_event_name: "PreToolUse",
    tool_name: "Bash",
    tool_input: { command },
  })
  const proc = Bun.spawn(["python3", ENGINE], {
    stdin: "pipe",
    stdout: "pipe",
    stderr: "pipe",
  })
  const timer = setTimeout(() => proc.kill(), TIMEOUT_MS)
  try {
    proc.stdin.write(envelope)
    await proc.stdin.end()
    const [stdout, exitCode] = await Promise.all([
      new Response(proc.stdout).text(),
      proc.exited,
    ])
    if (exitCode !== 0) throw new Error(`engine exited ${exitCode}`)
    const text = stdout.trim()
    if (!text) return { decision: "allow", reason: "" } // engine's silent default
    const parsed = JSON.parse(text)
    const out = parsed?.hookSpecificOutput
    const decision = out?.permissionDecision
    if (decision !== "allow" && decision !== "deny" && decision !== "ask")
      throw new Error("unexpected engine envelope")
    return { decision, reason: out?.permissionDecisionReason ?? "" }
  } finally {
    clearTimeout(timer)
  }
}

export const AISafeCommand: Plugin = async ({ directory }) => {
  return {
    "tool.execute.before": async (input, output) => {
      if (input.tool !== "bash") return
      const command = (output.args as { command?: string }).command
      if (!command) return
      try {
        const { decision, reason } = await verdict(
          command,
          input.sessionID ?? "",
          directory ?? process.cwd(),
        )
        if (decision === "allow") return
        throw new Error(`[ai-harness] ${decision.toUpperCase()}: ${reason}`)
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err)
        throw msg.startsWith("[ai-harness]")
          ? err
          : new Error(`[ai-harness] DENY: safe_command unavailable (${msg})`)
      }
    },
  }
}
