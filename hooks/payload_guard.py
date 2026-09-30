#!/usr/bin/env python3
"""Content guard for indirectly-executed payloads. NOT WIRED INTO THE HOOK.

safe_command.py inspects the command line. `python3 post.py` tells it nothing,
so a script can do anything the shell was gated for. This scans the payload
instead: the script file for `python3 x.py` / `bash x.sh` / `./x` / `source x`,
or the inline body for `python3 -c` / a heredoc into `python3 -`.

Design constraints, learned from a prototype that reused safe_command's own
rule set over script text (7 false positives in 400 real scripts, 2 of them
`deny`, all from synthesizing commands that were never written):

  * ASK only, never deny. A string in a file is weaker evidence than a command
    on a line, and a hard block on a false positive breaks real work.
  * Every pattern needs two or more ADJACENT tokens. A bare tool name is what
    turned `"vault", "cluster", "alert"` in a word list into a fake command.
  * Punctuation is normalised away so an argv list reads as a command line:
    subprocess.run(['glab','api','--method','POST']) -> glab api --method POST
  * Full-line comments are skipped.
  * Patterns match the NORMALISED line, so they must not contain `=`, quotes,
    parens or brackets — normalisation has already removed them. Writing
    `of=/dev/` and `requests.post\(` against raw text is how a dd rule and
    every requests.post in the corpus silently never fired.

Known and accepted gaps: a command assembled at runtime ('PO'+'ST', endpoint
read from JSON), a script that writes and runs a second script, HTTP calls to
APIs not listed below, `make`/`npm run` indirection, and the MCP tool route
which never reaches Bash at all. This raises the bar; it is not a boundary.
"""
from __future__ import annotations

import os
import re
import shlex
import sys

MAX_BYTES = 262_144
MAX_LINES = 4_000
EXCERPT = 110

# Punctuation that separates argv elements in python/js/json literals. Replaced
# with a space, never removed, so 'PO'+'ST' stays two tokens (see gaps above).
# `subprocess.run` is deliberately preserved: it is the command-position anchor
# for an argv list, and stripping it made the real post_c4.py bypass score
# `allow` — the one case this guard exists to catch.
_PUNCT = re.compile(r"""["'`,\[\]{}()=]|\bself\.""")
_WS = re.compile(r"\s+")


def normalize_line(line: str) -> str:
    return _WS.sub(" ", _PUNCT.sub(" ", line)).strip()


# A tool name only counts at a COMMAND POSITION: start of line, after a shell
# operator, or after a spawn helper. Without this, prose ("a job deleted the
# vault write path") and docstrings match. Backtick is deliberately NOT a
# command prefix here: in this corpus it means markdown code-quote far more
# often than legacy command substitution, so honouring it re-imports the
# docstring false positives. Cost: `vault write` inside backticks is missed.
# The `cmd`/`args` names are included because building an argv list into a
# variable and running it later is the ordinary shape, and the tokens still have
# to be adjacent for a pattern to fire.
# Consuming, not a lookbehind: the tool is often separated from the spawn word
# by an env prefix — subprocess.run(["env","-u","GITLAB_TOKEN","glab","api",…])
# is how the real MR-reply scripts are written, and a fixed-width lookbehind
# cannot skip that, so those scripts scored clean.
CMD_POS = (r"(?:^|[;&|]\s*|"
           r"\b(?:run|exec|system|check_output|check_call|Popen|call|sudo|xargs|"
           r"eval|cmd|args|argv|command)\s+)"
           r"(?:(?:-{1,2}\w[\w-]*|[A-Za-z_]\w*=\S*|[A-Z][A-Z0-9_]{2,}|env|sudo|nohup|"
           r"time|stdbuf|unbuffer)\s+){0,8}")
# `then` and `do` are NOT prefixes: English prose uses them ("and then
# `git push --force` to delete it" in a slide script matched because of it),
# while shell almost always ends the line after them.

# Lines that DEFINE a pattern or tabulate test cases are data, not calls. This
# is what turned safe_command's own fixtures into 60+ flags.
DATA_LINE = re.compile(
    r"re\.compile\(|"                      # a rule definition
    r"^\(\s*r?[\"'].*[\"']\s*,.*\)\s*,?\s*$|"   # ("cmd", "label") / (cmd, label, False),
    r"^r?[\"'].*[\"']\s*,?\s*$|"           # a bare string literal in a list of cases
    r"^r[\"'].*[\"']\s*(,\s*(True|False)\s*)*\)?,?\s*$|"  # a raw-string regex = a rule, not a call
    r"^[-*]\s|^\d+\.\s"                    # markdown bullet inside a docstring
)

# Line-level exemptions, applied before the patterns. Each mirrors a decision
# safe_command already makes, or puts something outside this guard's scope.
EXEMPT: list[tuple[str, re.Pattern]] = [
    # `vault write auth/<x>/login` is authentication, not a state change.
    ("vault login", re.compile(r"\bvault\s+write\b[^|;&]*\bauth/[\w.-]+/login\b", re.I)),
    # curl -G sends the data as a query string: it is a GET. safe_command's own
    # _http_method_from_curl treats it the same way.
    ("curl -G is a GET", re.compile(r"\bcurl\b(?=[^|;&]*\s(-G|--get)\b)", re.I)),
    # Nothing outside this machine is touched.
    ("localhost target", re.compile(r"https?://(localhost|127\.0\.0\.1|0\.0\.0\.0)[:/]", re.I)),
    # POST endpoints that are semantically reads. Without this, a JQL search and
    # a CI lint are indistinguishable from a write.
    ("read-via-POST endpoint",
     re.compile(r"/rest/api/\d+(\.\d+)?/search|/search/jql|/ci/lint|/_search\b|/_msearch\b", re.I)),
    # A server inspecting its own inbound request, not an outbound call.
    ("inbound request.method", re.compile(r"\brequest\s*\.\s*method\s*(==|!=|\bin\b)", re.I)),
]

# Some scripts put the method in a parameter — `def call(url, payload, method)`
# with `Request(..., method=method)` — so the verb and the call never share a
# line. Two real Jira/Confluence writers scored clean that way. This correlates
# per FILE instead: a method-from-variable HTTP call plus a mutating verb
# literal somewhere in the same file. Weaker evidence, hence its own reason.
VAR_METHOD_CALL = re.compile(
    r"\b(urllib\.request\.Request|Request|make_request|requests\.request)\b[\s\S]{0,200}?"
    r"\bmethod\s*=\s*(?!['\"])\w+", re.I)
MUTATING_LITERAL = re.compile(r"['\"](POST|PUT|PATCH|DELETE)['\"]")

# `rm -rf` is only interesting when the target is not scratch. safe_command
# allows `rm -rf /tmp/x` on the command line; flagging every mktemp cleanup
# here would be stricter than the hook it is meant to complement.
SENSITIVE_TARGET = re.compile(
    r"~|\$HOME|/Users/|\$DOTFILES|\.claude|\.git\b|/projects/|"
    r"\brm\s+-\w*[rf]\w*\s+/(?:\s|$|\*)|\$\{?(?:REPO|ROOT|DEST|TARGET|DIR)\b", re.I)


# (reason, body, anchored, needs_sensitive_target). `anchored` bodies are shell
# commands and only count at a command position; unanchored ones are library
# calls, where there is no shell position to anchor to.
_SPECS: list[tuple[str, str, bool, bool]] = [
    ("glab/gh api mutating method",
     r"(glab|gh)\s+api\b.*?\b(--method|-X)\s+(POST|PUT|PATCH|DELETE)\b", True, False),
    ("glab/gh api mutating body",
     r"(glab|gh)\s+api\b(?=.*\b(POST|PUT|PATCH|DELETE)\b).*?--(method|input|field)\b", True, False),
    ("glab/gh mr|pr mutation",
     r"(glab|gh)\s+(mr|pr|issue|release|repo)\s+"
     r"(create|update|merge|close|reopen|approve|note|comment|delete|edit|rebase)\b", True, False),
    ("acli mutating op",
     r"acli\s+(jira|confluence)\b.*?\b"
     r"(create|update|edit|delete|remove|assign|transition|archive|publish|upload|import)\b", True, False),
    ("vault write/delete",
     r"vault\s+(write|delete|patch)\b|"
     r"vault\s+kv\s+(put|patch|delete|destroy|undelete)\b|"
     r"vault\s+(policy|auth|secrets|audit)\s+(write|enable|disable|tune)\b|"
     r"vault\s+token\s+(create|revoke)\b|vault\s+lease\s+revoke\b|"
     r"vault\s+operator\s+(step-down|seal|unseal|raft)\b", True, False),
    ("aws mutating op",
     r"aws\s+s3\s+(cp|mv|sync|rm|rb)\b|"
     r"aws\s+\w+\s+(create|delete|put|update|attach|detach|modify|terminate)[\w-]*\b|"
     r"gsutil\s+(cp|mv|rm)\b", True, False),
    ("kubectl/helm mutation",
     r"kubectl\s+(apply|create|delete|patch|replace|scale|drain|cordon|uncordon|rollout|exec)\b|"
     r"helm\s+(install|upgrade|uninstall|rollback)\b", True, False),
    ("terraform state change",
     r"terraform\s+(apply|destroy|import|taint|state\s+\w+)\b", True, False),
    ("az/gcloud mutation",
     r"(az|gcloud)\s+[\w-]+\s+[\w-]*(create|delete|update|set|deploy|add|remove)[\w-]*\b", True, False),
    ("git force push",
     r"git\s+push\b.*?(--force\b|--force-with-lease\b|--mirror\b|\+refs/)", True, False),
    # safe_command DENIES these on a command line; inside a payload they were
    # invisible, so the guard has to carry its own copy.
    ("raw disk write / filesystem create",
     r"dd\s+[^|;&]*\bof\s|mkfs(\.\w+)?\s+[^|;&]*/dev/|newfs\w*\s+[^|;&]*/dev/|"
     r"wipefs\b|shred\s+[^|;&]*-\w*[nuz]|diskutil\s+(erase|reformat|partitionDisk)\b|"
     r"fdisk\s+[^|;&]*/dev/(?!.*\s-l\b)", True, False),
    ("privileged command in a payload",
     r"sudo\s+(-\w+\s+)*\S+", True, False),
    ("recursive delete of a non-scratch path",
     r"rm\s+-\w*r\w*f\w*\b|rm\s+-\w*f\w*r\w*\b|rm\s+-r\s+-f\b|find\b.*?\s-delete\b", True, True),
    ("curl with body or mutating method",
     r"curl\b.*?(-X\s*(POST|PUT|PATCH|DELETE)\b|--request\s+(POST|PUT|PATCH|DELETE)\b|"
     r"\s(-d|--data|--data-binary|--upload-file)\b)", True, False),
    # Trailing `(` must stay OPTIONAL: normalisation strips parens, so a pattern
    # requiring one never fires. That silently exempted every requests.post in
    # the corpus, including an issue-tracker write and a vendor-portal login.
    ("http write via library",
     r"\b(requests|httpx|session|client|conn)\s*\.\s*(post|put|patch|delete)\b", False, False),
    # `request` and `api` are NOT in this alternation: `if request.method ==
    # "POST"` is a server reading its own inbound request, and `api_ver` in a
    # route path matched too.
    ("urllib/wrapper call with a mutating method",
     r"\b(Request|urlopen|make_request|glab|gh)\b[^\n]{0,160}?"
     r"\bmethod\s+(POST|PUT|PATCH|DELETE)\b|"
     r"\b(make_request|call)\s+(POST|PUT|PATCH|DELETE)\b", False, False),
    ("recursive tree delete of a non-scratch path",
     r"\bshutil\.rmtree\b", False, True),
    ("message send (never on my behalf)",
     r"\bchat\.postMessage\b|\bslack_(send|post)\w*\b|\bsmtplib\b|\boutlook_send\b|\bsendmail\b", False, False),
]

PATTERNS: list[tuple[str, "re.Pattern", bool]] = [
    (reason, re.compile((CMD_POS if anchored else "") + f"(?:{body})", re.I), sensitive)
    for reason, body, anchored, sensitive in _SPECS
]

INTERPRETERS = {"python", "python2", "python3", "perl", "ruby", "node", "osascript",
                "bash", "sh", "zsh", "ksh", "dash"}
INLINE_FLAGS = {"-c", "-e"}
# Flags that take a value, so the value is not the script path.
VALUE_FLAGS = {"-m", "--eval", "-r", "-I", "-X"}


class Finding:
    __slots__ = ("lineno", "reason", "excerpt", "source")

    def __init__(self, lineno: int, reason: str, excerpt: str, source: str):
        self.lineno, self.reason, self.excerpt, self.source = lineno, reason, excerpt, source

    def __repr__(self) -> str:
        return f"{self.source}:{self.lineno}: {self.reason} | {self.excerpt}"


_OPEN = "([{"
_CLOSE = ")]}"
MAX_JOIN = 8


def logical_lines(text: str) -> list[tuple[int, str]]:
    """(start_lineno, joined_text) with bracket-continued lines merged.

    The real bypass wrote its argv across three physical lines, so `glab api`
    and `--method POST` never shared one. Depth counting is naive about brackets
    inside string literals; over-joining only widens a window that still
    requires adjacent tokens to match.
    """
    out: list[tuple[int, str]] = []
    lines = text.splitlines()[:MAX_LINES]
    i = 0
    while i < len(lines):
        start, buf, depth, joined = i + 1, [lines[i]], 0, 0
        while True:
            cur = buf[-1]
            depth += sum(cur.count(c) for c in _OPEN) - sum(cur.count(c) for c in _CLOSE)
            cont = depth > 0 or cur.rstrip().endswith("\\")
            if not cont or joined >= MAX_JOIN or i + 1 >= len(lines):
                break
            i += 1
            joined += 1
            buf.append(lines[i])
        out.append((start, " ".join(b.strip() for b in buf)))
        i += 1
    return out


def docstring_lines(text: str) -> set[int]:
    """1-indexed line numbers inside a triple-quoted block.

    Prose in docstrings is the single biggest false-positive source: this
    module's own usage example, safe_command's rule commentary, and a
    `md2adf.py` docstring naming an acli write all matched. Docstring content
    is never executed, so skipping it costs nothing.
    """
    inside: set[int] = set()
    delim = None
    for n, line in enumerate(text.splitlines(), start=1):
        rest = line
        while True:
            if delim is None:
                m = re.search(r'"""|\'\'\'', rest)
                if not m:
                    break
                delim = m.group(0)
                inside.add(n)
                rest = rest[m.end():]
                if delim in rest:            # opened and closed on one line
                    rest = rest[rest.index(delim) + 3:]
                    delim = None
                    continue
                break
            inside.add(n)
            if delim in rest:
                rest = rest[rest.index(delim) + 3:]
                delim = None
                continue
            break
        if delim is not None:
            inside.add(n)
    return inside


def _code_only(text: str, docs: set[int]) -> str:
    """Text with comments, docstrings and data/rule lines blanked out, keeping
    line numbering. The file-level rule searches raw text, so without this it
    reads this module's own rule definitions as a call."""
    keep = []
    for n, line in enumerate(text.splitlines(), start=1):
        s = line.strip()
        keep.append("" if (n in docs or s.startswith("#") or DATA_LINE.search(s)) else line)
    return "\n".join(keep)


def scan_text(text: str, source: str = "<payload>") -> list[Finding]:
    out: list[Finding] = []
    docs = docstring_lines(text)
    for i, raw in logical_lines(text):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or DATA_LINE.search(stripped):
            continue
        if i in docs:
            continue
        norm = normalize_line(stripped)
        # Exemptions see the raw line too: `request.method == "POST"` loses its
        # `==` to normalisation, and that is the whole tell.
        if any(pat.search(norm) or pat.search(stripped) for _, pat in EXEMPT):
            continue
        for reason, pat, needs_sensitive in PATTERNS:
            if not pat.search(norm):
                continue
            if needs_sensitive and not SENSITIVE_TARGET.search(stripped):
                continue
            out.append(Finding(i, reason, stripped[:EXCERPT], source))
            break
    if not out:
        code = _code_only(text, docs)
        m = VAR_METHOD_CALL.search(code)
        if m and MUTATING_LITERAL.search(code):
            lineno = code[:m.start()].count("\n") + 1
            out.append(Finding(lineno, "http call with method from a variable, "
                                       "and a mutating verb elsewhere in the file",
                               text.splitlines()[lineno - 1].strip()[:EXCERPT], source))
    return out


def scan_file(path: str) -> list[Finding]:
    try:
        with open(path, errors="ignore") as fh:
            text = fh.read(MAX_BYTES)
    except OSError:
        return []
    return scan_text(text, source=os.path.basename(path))


def _resolve(arg: str, cwd: str | None, command: str) -> str | None:
    """Resolve a script argument to a readable path. Falls back to the `cd`
    target in the same command, since the hook payload may carry no cwd."""
    cands = []
    if os.path.isabs(arg):
        cands.append(arg)
    else:
        if cwd:
            cands.append(os.path.join(cwd, arg))
        m = re.search(r"\bcd\s+(\S+)", command)
        if m:
            cands.append(os.path.join(os.path.expanduser(m.group(1).strip("'\"")), arg))
        cands.append(os.path.abspath(arg))
    for c in cands:
        c = os.path.expanduser(c)
        if os.path.isfile(c):
            return c
    return None


_SEGMENT_CHARS = {";", "|", "&", "\n"}


def split_segments(command: str) -> list[str]:
    """Quote-aware split on ; && || | & newline.

    A naive re.split cut `python3 -c "import x; x.run(...)"` at the semicolon
    inside the quoted payload, so the inline body was never scanned at all.
    """
    out: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i, n = 0, len(command)
    while i < n:
        c = command[i]
        if quote:
            if c == "\\" and i + 1 < n:
                buf.append(c); buf.append(command[i + 1]); i += 2; continue
            if c == quote:
                quote = None
            buf.append(c); i += 1; continue
        if c in ("'", '"'):
            quote = c; buf.append(c); i += 1; continue
        if c in _SEGMENT_CHARS:
            seg = "".join(buf).strip()
            if seg:
                out.append(seg)
            buf = []
            while i < n and command[i] in _SEGMENT_CHARS:
                i += 1
            continue
        buf.append(c); i += 1
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out


def scan_command(command: str, cwd: str | None = None) -> list[Finding]:
    """Findings for the payload a command would execute indirectly."""
    if not command:
        return []
    # A heredoc body already sits in the command text; scan the whole thing.
    if "<<" in command:
        return scan_text(command, source="<heredoc>")

    findings: list[Finding] = []
    for segment in split_segments(command):
        segment = segment.strip()
        if not segment:
            continue
        try:
            argv = shlex.split(segment)
        except ValueError:
            argv = segment.split()
        if not argv:
            continue
        head = os.path.basename(argv[0]).lstrip("\\")

        if head in ("source", ".") and len(argv) > 1:
            p = _resolve(argv[1], cwd, command)
            if p:
                findings += scan_file(p)
            continue

        if head not in INTERPRETERS:
            # ./script or a bare relative path with an exec bit
            if argv[0].startswith("./") or argv[0].startswith("../"):
                p = _resolve(argv[0], cwd, command)
                if p:
                    findings += scan_file(p)
            continue

        inline = None
        i = 1
        while i < len(argv):
            tok = argv[i]
            if tok in INLINE_FLAGS and i + 1 < len(argv):
                inline = argv[i + 1]
                break
            if tok in VALUE_FLAGS:
                i += 2
                continue
            if tok.startswith("-"):
                i += 1
                continue
            p = _resolve(tok, cwd, command)
            if p:
                findings += scan_file(p)
            break
        if inline:
            findings += scan_text(inline, source="<inline>")
    return findings


def decide(findings: list[Finding]) -> tuple[str, str]:
    if not findings:
        return "allow", ""
    f = findings[0]
    extra = f" (+{len(findings) - 1} more)" if len(findings) > 1 else ""
    return "ask", f"payload {f.source}:{f.lineno} — {f.reason}{extra}: {f.excerpt}"


def main(argv: list[str]) -> int:
    if len(argv) >= 3 and argv[1] == "--command":
        findings = scan_command(argv[2], cwd=os.getcwd())
    elif len(argv) >= 3 and argv[1] == "--file":
        findings = scan_file(argv[2])
    else:
        print(__doc__.strip().splitlines()[0])
        print("usage: payload_guard.py --command '<cmd>' | --file <script>")
        return 2
    decision, reason = decide(findings)
    print(decision, "|", reason)
    for f in findings[:20]:
        print("   ", f)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
