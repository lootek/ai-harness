// ai-harness command/prompt history for opencode.
//
// Deployed alongside ai-harness.safe-command.ts as a separate plugin file.
// Appends to the SAME per-alias history files the claude/codex hooks write:
//
//   ~/tmp/claude/ctx/<alias>/.commands_history.md   (executed bash commands)
//   ~/tmp/claude/ctx/<alias>/.prompts_history.md    (submitted prompts)
//
// Entry formats mirror hooks/log_commands.py + hooks/prompt_history.py
// byte-for-byte (same headers, same "### <ts>" / "## <ts>" blocks) so all
// three CLIs append to one history.
//
// opencode 2.x plugin API (default export { id, setup(ctx) }), verified live
// against opencode 2.0.20 (2026-10-03):
//
//   ctx.session.hook("prompt", cb) — fires once per submitted user message:
//     cb input: { sessionID: "ses_...", messageID, prompt: { text, files },
//                 delivery }
//     (`opencode run "x"` delivers the text JSON-quoted: "\"x\""; the TUI
//     delivers it verbatim. Logged as received.)
//
//   ctx.tool.hook("execute.after", cb) — once per tool call:
//     cb input: { tool: "shell", sessionID, id, input: { command,
//                 description? }, status: "completed" | "error", ... }
//     A call denied by the safety gate surfaces as status "error" with a
//     Permission.BlockedError and is not logged (it never ran).
//     (tool is "shell" in v2, "bash" in v1; both are matched.)
//
// Only the shell tool is logged (the claude engine also formats Edit/Write;
// opencode's edit/write tool arg shapes are unverified — bash-only until
// probed). Best-effort: every fault is swallowed — history must never break
// an opencode session.
//
// Appends go through node:fs/promises appendFile (Bun.file has no append
// primitive; read-modify-write via Bun.write would race concurrent CLIs).

import { appendFile, readdir, readFile, stat, writeFile } from "node:fs/promises"
import { basename, join } from "node:path"
import { homedir } from "node:os"

const CTX_ROOT = join(homedir(), "tmp", "claude", "ctx")

// ── session-dir resolution (mirrors the engines: cwd → session-id → recency)
function byCwd(directory: string): string | null {
  if (!directory.startsWith(CTX_ROOT + "/")) return null
  const alias = directory.slice(CTX_ROOT.length + 1).split("/")[0]
  return alias ? join(CTX_ROOT, alias) : null
}

async function bySessionId(sessionID: string): Promise<string | null> {
  if (!sessionID) return null
  let entries: string[]
  try {
    entries = await readdir(CTX_ROOT)
  } catch {
    return null
  }
  for (const e of entries) {
    try {
      const cur = (await readFile(join(CTX_ROOT, e, ".session"), "utf8")).trim()
      if (cur === sessionID) return join(CTX_ROOT, e)
    } catch {
      continue
    }
  }
  return null
}

async function byRecency(): Promise<string | null> {
  let entries: string[]
  try {
    entries = await readdir(CTX_ROOT)
  } catch {
    return null
  }
  let best: { mtime: number; dir: string } | null = null
  for (const e of entries) {
    const p = join(CTX_ROOT, e, ".session")
    try {
      const s = await stat(p)
      if (!best || s.mtimeMs > best.mtime) best = { mtime: s.mtimeMs, dir: join(CTX_ROOT, e) }
    } catch {
      continue
    }
  }
  return best?.dir ?? null
}

async function resolveSession(
  directory: string,
  sessionID: string,
): Promise<string | null> {
  // order matters: cwd wins, exactly like log_commands.py
  return byCwd(directory) ?? (await bySessionId(sessionID)) ?? (await byRecency())
}

// the engines rewrite .session with the invoking session id when they resolve
// the dir by cwd/recency — mirror that so claude/codex by-id lookups survive
async function refreshSessionFile(dir: string, sessionID: string): Promise<void> {
  if (!sessionID) return
  const p = join(dir, ".session")
  try {
    const cur = (await readFile(p, "utf8")).trim()
    if (cur === sessionID) return
  } catch {
    // absent -> write below
  }
  await writeFile(p, sessionID + "\n").catch(() => {})
}

// ── entry formats — byte-identical to the engines' formatters ──
function ts(): string {
  const d = new Date()
  const p = (n: number) => String(n).padStart(2, "0")
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ` +
    `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

function formatBash(command: string, description?: string): string {
  const header = description ? `**Bash** — ${description}` : "**Bash**"
  return `${header}\n\`\`\`bash\n${command}\n\`\`\``
}

async function append(file: string, headerIfNew: string, block: string): Promise<void> {
  try {
    let out = ""
    try {
      await stat(file)
    } catch {
      out = headerIfNew
    }
    out += block
    await appendFile(file, out)
  } catch {
    // best-effort: unwritable target etc.
  }
}

const SHELL_TOOLS = new Set(["shell", "bash"])

export default {
  id: "ai-harness.history",
  setup: async (ctx: any) => {
    const dir: string = ctx?.location?.directory ?? process.cwd()

    const sessionDir = async (sessionID: string) => {
      const s = await resolveSession(dir, sessionID)
      if (!s) return null
      await refreshSessionFile(s, sessionID)
      return s
    }

    await ctx.session.hook("prompt", async (ev: any) => {
      try {
        const prompt: string = ev?.prompt?.text ?? ""
        if (!prompt) return
        const s = await sessionDir(String(ev.sessionID ?? ""))
        if (!s) return
        await append(
          join(s, ".prompts_history.md"),
          `# Session: ${basename(s)} — Prompts History\n`,
          `\n## ${ts()}\n${prompt}\n`,
        )
      } catch {
        // best-effort
      }
    })

    await ctx.tool.hook("execute.after", async (ev: any) => {
      try {
        if (!SHELL_TOOLS.has(ev.tool)) return
        if (ev.status === "error" && ev.error?.error?._tag === "Permission.BlockedError") return
        const args = (ev.input ?? {}) as { command?: string; description?: string }
        if (!args.command) return
        const s = await sessionDir(String(ev.sessionID ?? ""))
        if (!s) return
        await append(
          join(s, ".commands_history.md"),
          "# Commands History\n\n",
          `### ${ts()}\n${formatBash(args.command, args.description)}\n\n`,
        )
      } catch {
        // best-effort
      }
    })
  },
}
