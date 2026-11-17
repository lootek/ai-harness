#!/usr/bin/env python3
"""memory_engine - a curated, file-based agent memory store shared by several AI CLIs.

One fact per Markdown file, YAML-ish frontmatter, one index file (MEMORY.md) with
one line per memory. Every write goes through this module, which enforces:

  * format      - frontmatter name/description/type, file naming, index line
  * dedup       - create() refuses when an existing memory is too similar and
                  returns the candidates; the caller updates or supersedes instead
  * conflicts   - supersede() retires an outdated memory (superseded_by + date,
                  dropped from the index, file kept for history)
  * concurrency - one advisory lock per store, atomic writes, and edits that
                  carry the sha256 of the content the caller last read
  * hygiene     - path guard (top-level *.md only) and a secret-pattern gate

Stdlib only. Configure with AI_MEMORY_DIR (required for the server/CLI).
Used by memory_mcp.py (MCP stdio server) and runnable as a CLI for lint/search.
"""
from __future__ import annotations

import datetime as _dt
import fcntl
import hashlib
import math
import os
import re
import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

INDEX = "MEMORY.md"
TYPES = ("user", "feedback", "project", "reference")
LOCK = ".memory.lock"
TRASH = ".trash"

# Similarity at or above this blocks create() unless force=True.
DUP_THRESHOLD = float(os.environ.get("AI_MEMORY_DUP_THRESHOLD", "0.35"))
# Candidates at or above this are reported (but do not block).
NEAR_THRESHOLD = float(os.environ.get("AI_MEMORY_NEAR_THRESHOLD", "0.2"))

SECRET_PATTERNS = [
    (r"glpat-[0-9A-Za-z_\-]{20,}", "GitLab personal access token"),
    (r"GR1348941[0-9A-Za-z_\-]{10,}", "GitLab runner registration token"),
    (r"gh[pousr]_[0-9A-Za-z]{30,}", "GitHub token"),
    (r"xox[abposr]-[0-9A-Za-z\-]{10,}", "Slack token"),
    (r"AKIA[0-9A-Z]{16}", "AWS access key id"),
    (r"\bhv[sb]\.[0-9A-Za-z_\-]{20,}", "Vault token"),
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "private key"),
    (r"(?i)\bbearer\s+[0-9A-Za-z._\-]{20,}", "bearer token"),
    (r"\beyJ[0-9A-Za-z_\-]{10,}\.[0-9A-Za-z_\-]{10,}\.[0-9A-Za-z_\-]{10,}", "JWT"),
    (r"(?i)\b(password|passwd|secret|api[_-]?key)\s*[:=]\s*['\"]?[^\s'\"]{8,}", "credential assignment"),
]

STOP = set(
    "a an and are as at be by for from has have in is it its of on or that the this to was "
    "were will with not no do does did can should must when then than into via use used using "
    "only also but if so all any each one per our your their his her them they we you i".split()
)


class MemoryError_(Exception):
    """User-facing error; message is returned to the calling agent verbatim."""


# --------------------------------------------------------------------------- utils

def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def today() -> str:
    return _dt.date.today().isoformat()


def session_id() -> str:
    for k in ("AI_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_THREAD_ID"):
        if os.environ.get(k):
            return os.environ[k]
    return ""


def slugify(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")
    return s[:60] or "memory"


def tokens(text: str) -> list[str]:
    out = []
    for t in re.findall(r"[a-z0-9][a-z0-9_.\-]*[a-z0-9]|[a-z0-9]", text.lower()):
        if t in STOP or len(t) < 2:
            continue
        out.append(t)
    return out


def scan_secrets(text: str) -> list[str]:
    hits = []
    for pat, label in SECRET_PATTERNS:
        if re.search(pat, text):
            hits.append(label)
    return hits


def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1]
        if v and '"' in v:
            v = v.replace('\\"', '"')
    return v


def _quote(v: str) -> str:
    v = v.replace("\n", " ").strip()
    if re.search(r"[:#\"'\[\]{}&*!|>%@`]|^\s|\s$|^-", v):
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return v


# --------------------------------------------------------------------- frontmatter

def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse the small YAML subset memories use: top-level scalars plus one
    nested `metadata:` map. Returns (meta, body). Flattens metadata.* into the
    top level when the key is not already present (old files keep `type` at the
    top level, new ones under metadata)."""
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end < 0:
        return {}, text
    head = text[4:end]
    body = text[end + 4:].lstrip("\n")
    meta: dict = {}
    nested: dict | None = None
    for line in head.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = re.match(r"^(\s*)([A-Za-z_][\w\-]*):\s*(.*)$", line)
        if not m:
            continue
        indent, key, val = m.groups()
        if indent:
            if nested is not None:
                nested[key] = _unquote(val)
            continue
        if val == "":
            nested = {}
            meta[key] = nested
        else:
            nested = None
            meta[key] = _unquote(val)
    md = meta.get("metadata")
    if isinstance(md, dict):
        for k, v in md.items():
            meta.setdefault(k, v)
    return meta, body


def render_frontmatter(name: str, description: str, mtype: str, extra: dict | None = None) -> str:
    lines = ["---", f"name: {_quote(name)}", f"description: {_quote(description)}", "metadata:",
             "  node_type: memory", f"  type: {mtype}"]
    for k, v in (extra or {}).items():
        if v:
            lines.append(f"  {k}: {_quote(str(v))}")
    lines += ["---", ""]
    return "\n".join(lines)


def set_meta_field(text: str, key: str, value: str) -> str:
    """Set metadata.<key> (or a top-level key if the file has no metadata map)
    by editing lines in place, so the rest of the frontmatter stays verbatim."""
    if not text.startswith("---\n"):
        raise MemoryError_("file has no frontmatter")
    end = text.find("\n---", 4)
    head, rest = text[4:end].split("\n"), text[end:]
    qv = _quote(value)
    has_md = any(re.match(r"^metadata:\s*$", l) for l in head)
    pat = re.compile(rf"^(\s+){re.escape(key)}:" if has_md else rf"^{re.escape(key)}:")
    for i, l in enumerate(head):
        if pat.match(l):
            head[i] = (f"  {key}: {qv}" if has_md else f"{key}: {qv}")
            break
    else:
        if has_md:
            idx = next(i for i, l in enumerate(head) if re.match(r"^metadata:\s*$", l))
            j = idx + 1
            while j < len(head) and head[j].startswith((" ", "\t")):
                j += 1
            head.insert(j, f"  {key}: {qv}")
        else:
            head.append(f"{key}: {qv}")
    return "---\n" + "\n".join(head) + rest


# --------------------------------------------------------------------------- model

@dataclass
class Memory:
    file: str
    text: str
    meta: dict
    body: str

    @property
    def name(self) -> str:
        return str(self.meta.get("name", "")).strip()

    @property
    def description(self) -> str:
        return str(self.meta.get("description", "")).strip()

    @property
    def type(self) -> str:
        t = str(self.meta.get("type", "")).strip()
        if not t:  # older files: the type lives only in the file-name prefix
            t = self.file.split("_", 1)[0]
        return t

    @property
    def superseded_by(self) -> str:
        return str(self.meta.get("superseded_by", "")).strip()

    def doc(self) -> str:
        # name/description weigh more than the body
        return " ".join([self.name] * 3 + [self.description] * 2 + [self.body])


@dataclass
class IndexLine:
    lineno: int
    title: str
    file: str
    hook: str
    raw: str


# Titles may contain brackets ('"[Claude suggestion]" prefix scope'), so match lazily
# up to the first "](<file>.md)".
INDEX_RE = re.compile(r"^\s*-\s*\[(?P<title>.+?)\]\((?P<file>[^()\s]+\.md)\)\s*(?:[—:\-–]+\s*(?P<hook>.*))?$")


@dataclass
class Store:
    root: Path
    _cache: dict = field(default_factory=dict, repr=False)

    def __post_init__(self):
        self.root = Path(self.root).expanduser().resolve()
        if not self.root.is_dir():
            raise MemoryError_(f"memory dir does not exist: {self.root}")

    # ---------------------------------------------------------------- paths/io
    def path(self, file: str, *, must_exist: bool = True) -> Path:
        f = (file or "").strip()
        if not f or "/" in f or "\\" in f or f.startswith(".") or not f.endswith(".md"):
            raise MemoryError_(f"invalid memory file {file!r}: use a top-level <name>.md in the store")
        p = (self.root / f).resolve()
        if p.parent != self.root:
            raise MemoryError_(f"path escapes the store: {file!r}")
        if must_exist and not p.is_file():
            raise MemoryError_(f"no such memory: {f}")
        return p

    def read(self, file: str) -> str:
        return self.path(file).read_text()

    def _write(self, p: Path, text: str) -> None:
        fd, tmp = tempfile.mkstemp(dir=self.root, prefix=".tmp.", suffix=".md")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(text)
            os.replace(tmp, p)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    @contextmanager
    def lock(self):
        with open(self.root / LOCK, "a+") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def _check_hash(self, file: str, current: str, expected: str | None) -> None:
        if not expected:
            raise MemoryError_(
                f"expected_hash is required for edits; view {file} first and pass its hash")
        if sha(current) != expected:
            raise MemoryError_(
                f"{file} changed since you read it (hash {sha(current)} != {expected}); "
                "view it again and redo the edit against the current content")

    def _gate(self, text: str) -> None:
        hits = scan_secrets(text)
        if hits:
            raise MemoryError_("refused: content looks like it contains a secret ("
                               + ", ".join(sorted(set(hits))) + "). Memories must never hold secrets.")

    # ------------------------------------------------------------------ loading
    def memories(self, include_superseded: bool = False) -> list[Memory]:
        out = []
        for p in sorted(self.root.glob("*.md")):
            if p.name == INDEX:
                continue
            text = p.read_text(errors="replace")
            meta, body = parse_frontmatter(text)
            m = Memory(p.name, text, meta, body)
            if m.superseded_by and not include_superseded:
                continue
            out.append(m)
        return out

    def index_text(self) -> str:
        p = self.root / INDEX
        return p.read_text() if p.exists() else ""

    def index_lines(self) -> list[IndexLine]:
        out = []
        for i, line in enumerate(self.index_text().splitlines()):
            m = INDEX_RE.match(line)
            if m:
                out.append(IndexLine(i, m["title"].strip(), m["file"].strip(), (m["hook"] or "").strip(), line))
        return out

    def _index_rewrite(self, fn) -> None:
        p = self.root / INDEX
        lines = self.index_text().splitlines()
        lines = fn(lines)
        self._write(p, "\n".join(lines).rstrip("\n") + "\n")

    def _index_upsert(self, file: str, title: str, hook: str) -> None:
        new = f"- [{title.strip()}]({file}) — {hook.strip()}"

        def fn(lines):
            for i, l in enumerate(lines):
                m = INDEX_RE.match(l)
                if m and m["file"].strip() == file:
                    lines[i] = new
                    return lines
            return lines + [new]
        self._index_rewrite(fn)

    def _index_drop(self, file: str) -> bool:
        dropped = []

        def fn(lines):
            keep = []
            for l in lines:
                m = INDEX_RE.match(l)
                if m and m["file"].strip() == file:
                    dropped.append(l)
                    continue
                keep.append(l)
            return keep
        self._index_rewrite(fn)
        return bool(dropped)

    # ------------------------------------------------------------------ search
    def _tfidf(self, docs: list[list[str]]):
        df: dict[str, int] = {}
        for d in docs:
            for t in set(d):
                df[t] = df.get(t, 0) + 1
        n = len(docs) or 1

        def vec(toks):
            tf: dict[str, float] = {}
            for t in toks:
                tf[t] = tf.get(t, 0) + 1
            v = {t: (1 + math.log(c)) * math.log(1 + n / df.get(t, 1)) for t, c in tf.items()}
            norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
            return {t: x / norm for t, x in v.items()}
        return vec

    def similar(self, text: str, *, exclude: str | None = None, limit: int = 5,
                include_superseded: bool = False) -> list[tuple[float, Memory]]:
        mems = [m for m in self.memories(include_superseded) if m.file != exclude]
        if not mems:
            return []
        docs = [tokens(m.doc()) for m in mems]
        q = tokens(text)
        vec = self._tfidf(docs + [q])
        qv = vec(q)
        scored = []
        for m, d in zip(mems, docs):
            dv = vec(d)
            s = sum(w * dv.get(t, 0.0) for t, w in qv.items())
            if s > 0:
                scored.append((round(s, 3), m))
        scored.sort(key=lambda x: -x[0])
        return scored[:limit]

    def search(self, query: str, limit: int = 10, include_superseded: bool = False) -> list[dict]:
        out = []
        for s, m in self.similar(query, limit=limit, include_superseded=include_superseded):
            out.append({"file": m.file, "name": m.name, "description": m.description,
                        "type": m.type, "score": s,
                        **({"superseded_by": m.superseded_by} if m.superseded_by else {})})
        return out

    # ------------------------------------------------------------------- reads
    def view(self, file: str | None = None) -> dict:
        if not file or file == INDEX:
            text = self.index_text()
            return {"file": INDEX, "hash": sha(text), "content": text}
        text = self.read(file)
        numbered = "\n".join(f"{i + 1:4d}\t{l}" for i, l in enumerate(text.splitlines()))
        return {"file": file, "hash": sha(text), "content": numbered}

    # ------------------------------------------------------------------ writes
    def create(self, *, name: str, description: str, type: str, body: str,
               title: str | None = None, hook: str | None = None, file: str | None = None,
               force: bool = False, force_reason: str = "") -> dict:
        name, description, body = name.strip(), description.strip(), body.strip()
        if type not in TYPES:
            raise MemoryError_(f"type must be one of {', '.join(TYPES)}")
        if not name or not description or not body:
            raise MemoryError_("name, description and body are all required")
        if "\n" in description:
            raise MemoryError_("description must be one line")
        if type in ("feedback", "project") and not ("**Why:**" in body and "**How to apply:**" in body):
            raise MemoryError_("feedback/project memories need a '**Why:**' and a '**How to apply:**' line")
        if force and not force_reason.strip():
            raise MemoryError_("force=true needs force_reason: say why this is not a duplicate")
        file = file or f"{type}_{slugify(name)}.md"
        if not file.startswith(f"{type}_"):
            raise MemoryError_(f"file name must start with '{type}_'")
        p = self.path(file, must_exist=False)
        text = render_frontmatter(name, description, type,
                                  {"originSessionId": session_id(), "modified": now_iso()}) + body + "\n"
        self._gate(text)
        with self.lock():
            if p.exists():
                raise MemoryError_(f"{file} already exists; view it and edit it instead")
            for m in self.memories(include_superseded=True):
                if m.name.lower() == name.lower():
                    raise MemoryError_(f"a memory named {name!r} already exists in {m.file}; update it instead")
            cands = self.similar(f"{name} {description} {body}", limit=5)
            blocking = [(s, m) for s, m in cands if s >= DUP_THRESHOLD]
            near = [{"file": m.file, "name": m.name, "description": m.description, "score": s}
                    for s, m in cands if s >= NEAR_THRESHOLD]
            if blocking and not force:
                return {"created": False, "reason": "possible duplicate", "threshold": DUP_THRESHOLD,
                        "candidates": near,
                        "next": "view the top candidate. Same fact: edit it (str_replace). Contradicts it: "
                                "create with force=true + force_reason, then supersede the old one. "
                                "Different fact: create with force=true + force_reason."}
            self._write(p, text)
            self._index_upsert(file, title or name, hook or description)
        return {"created": True, "file": file, "hash": sha(text), "near_matches": near,
                **({"forced": force_reason} if force else {})}

    def str_replace(self, *, file: str, old: str, new: str, expected_hash: str) -> dict:
        if file == INDEX:
            return self._edit_index(old, new, expected_hash)
        p = self.path(file)
        with self.lock():
            cur = p.read_text()
            self._check_hash(file, cur, expected_hash)
            n = cur.count(old)
            if n != 1:
                raise MemoryError_(f"old text must match exactly once in {file} (matched {n} times)")
            text = cur.replace(old, new, 1)
            self._gate(text)
            meta, _ = parse_frontmatter(text)
            if not meta.get("name") or not meta.get("description") or \
                    Memory(file, text, meta, "").type not in TYPES:
                raise MemoryError_("edit would break the frontmatter (name/description/type)")
            text = set_meta_field(text, "modified", now_iso())
            self._write(p, text)
            # keep the index hook in step with a changed description
            old_meta, _ = parse_frontmatter(cur)
            if old_meta.get("description") != meta.get("description"):
                il = next((l for l in self.index_lines() if l.file == file), None)
                self._index_upsert(file, il.title if il else meta["name"], meta["description"])
        return {"file": file, "hash": sha(text)}

    def insert(self, *, file: str, after_line: int, text: str, expected_hash: str) -> dict:
        p = self.path(file)
        with self.lock():
            cur = p.read_text()
            self._check_hash(file, cur, expected_hash)
            lines = cur.splitlines()
            meta_end = cur[4:].find("\n---") + 1
            fm_lines = cur[:4 + meta_end + 4].count("\n") if cur.startswith("---\n") else 0
            if after_line < fm_lines or after_line > len(lines):
                raise MemoryError_(f"after_line must be between {fm_lines} (end of frontmatter) and {len(lines)}")
            lines[after_line:after_line] = text.rstrip("\n").split("\n")
            out = "\n".join(lines) + "\n"
            self._gate(out)
            out = set_meta_field(out, "modified", now_iso())
            self._write(p, out)
        return {"file": file, "hash": sha(out)}

    def _edit_index(self, old: str, new: str, expected_hash: str) -> dict:
        p = self.root / INDEX
        with self.lock():
            cur = self.index_text()
            self._check_hash(INDEX, cur, expected_hash)
            if cur.count(old) != 1:
                raise MemoryError_(f"old text must match exactly once in {INDEX}")
            text = cur.replace(old, new, 1)
            self._gate(text)
            self._write(p, text)
        return {"file": INDEX, "hash": sha(text)}

    def supersede(self, *, old_file: str, new_file: str, reason: str) -> dict:
        if not reason.strip():
            raise MemoryError_("reason is required: say what changed")
        po, pn = self.path(old_file), self.path(new_file)
        if po == pn:
            raise MemoryError_("old_file and new_file must differ")
        with self.lock():
            cur = po.read_text()
            new_name = parse_frontmatter(pn.read_text())[0].get("name", new_file)
            text = set_meta_field(cur, "superseded_by", new_file)
            text = set_meta_field(text, "superseded_on", today())
            text = set_meta_field(text, "modified", now_iso())
            meta, body = parse_frontmatter(text)
            head = text[: len(text) - len(body)]
            note = f"> **Superseded {today()}** by [[{new_name}]] ({new_file}): {reason.strip()}\n\n"
            text = head + note + body
            self._gate(text)
            self._write(po, text)
            dropped = self._index_drop(old_file)
        return {"superseded": old_file, "by": new_file, "removed_from_index": dropped}

    def delete(self, *, file: str, expected_hash: str) -> dict:
        p = self.path(file)
        with self.lock():
            self._check_hash(file, p.read_text(), expected_hash)
            trash = self.root / TRASH
            trash.mkdir(exist_ok=True)
            dest = trash / f"{_dt.datetime.now().strftime('%Y%m%d-%H%M%S')}-{file}"
            shutil.move(str(p), dest)
            self._index_drop(file)
        return {"deleted": file, "moved_to": str(dest.relative_to(self.root))}

    def rename(self, *, old_file: str, new_file: str) -> dict:
        po = self.path(old_file)
        pn = self.path(new_file, must_exist=False)
        with self.lock():
            if pn.exists():
                raise MemoryError_(f"{new_file} already exists")
            os.rename(po, pn)

            def fn(lines):
                for i, l in enumerate(lines):
                    m = INDEX_RE.match(l)
                    if m and m["file"].strip() == old_file:
                        lines[i] = l.replace(f"]({old_file})", f"]({new_file})", 1)
                return lines
            self._index_rewrite(fn)
        return {"renamed": old_file, "to": new_file}

    # -------------------------------------------------------------------- lint
    def lint(self) -> dict:
        mems = self.memories(include_superseded=True)
        files = {m.file for m in mems}
        live = {m.file for m in mems if not m.superseded_by}
        idx = self.index_lines()
        indexed = [l.file for l in idx]
        names: dict[str, list[str]] = {}
        for m in mems:
            names.setdefault(m.name.lower(), []).append(m.file)
        def norm(x):
            x = re.sub(r"\.md$", "", x.strip().lower())
            return re.sub(r"[\s_\-]+", "-", x)
        all_names = {norm(k) for m in mems for k in (m.name, m.file[:-3], m.file[:-3].split("_", 1)[-1])}
        # A [[link]] to a memory that does not exist yet is allowed (it marks one worth
        # writing), so dangling links are reported as notes, not problems.
        notes = {
            "dangling_links": sorted({f"{m.file} -> [[{t}]]" for m in mems
                                      for t in re.findall(r"\[\[([^\]]+)\]\]", re.sub(r"`[^`\n]*`", "", m.body))
                                      if norm(t) not in all_names}),
        }
        problems = {
            "not_in_index": sorted(live - set(indexed)),
            "index_points_to_missing": sorted(set(indexed) - files),
            "superseded_still_indexed": sorted({m.file for m in mems if m.superseded_by} & set(indexed)),
            "duplicate_index_lines": sorted({f for f in indexed if indexed.count(f) > 1}),
            "bad_frontmatter": sorted(m.file for m in mems
                                      if not m.name or not m.description or m.type not in TYPES),
            "duplicate_names": sorted(fs[0] if len(fs) == 1 else ", ".join(fs)
                                      for n, fs in names.items() if n and len(fs) > 1),
            "secret_hits": sorted(f"{m.file}: {', '.join(h)}" for m in mems if (h := scan_secrets(m.text))),
        }
        return {"memories": len(mems), "live": len(live), "index_lines": len(idx),
                "problems": {k: v for k, v in problems.items() if v},
                "notes": {k: v for k, v in notes.items() if v}}

    def near_duplicates(self, threshold: float | None = None) -> list[dict]:
        th = DUP_THRESHOLD if threshold is None else threshold
        mems = self.memories()
        seen, out = set(), []
        for m in mems:
            for s, o in self.similar(m.doc(), exclude=m.file, limit=3):
                key = tuple(sorted((m.file, o.file)))
                if s >= th and key not in seen:
                    seen.add(key)
                    out.append({"a": key[0], "b": key[1], "score": s})
        return sorted(out, key=lambda x: -x["score"])


def store_from_env() -> Store:
    d = os.environ.get("AI_MEMORY_DIR")
    if not d:
        raise MemoryError_("AI_MEMORY_DIR is not set")
    return Store(Path(d))


def _cli(argv: list[str]) -> int:
    import argparse
    import json
    ap = argparse.ArgumentParser(prog="memory_engine", description=__doc__.split("\n")[0])
    ap.add_argument("--dir", default=os.environ.get("AI_MEMORY_DIR"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("lint")
    s = sub.add_parser("search"); s.add_argument("query"); s.add_argument("-n", type=int, default=10)
    d = sub.add_parser("dups"); d.add_argument("--threshold", type=float)
    a = ap.parse_args(argv)
    if not a.dir:
        ap.error("--dir or AI_MEMORY_DIR required")
    st = Store(Path(a.dir))
    if a.cmd == "lint":
        r = st.lint()
        print(json.dumps(r, indent=2))
        return 1 if r["problems"] else 0
    if a.cmd == "search":
        print(json.dumps(st.search(a.query, a.n), indent=2))
    if a.cmd == "dups":
        print(json.dumps(st.near_duplicates(a.threshold), indent=2))
    return 0


if __name__ == "__main__":
    import sys
    try:
        sys.exit(_cli(sys.argv[1:]))
    except MemoryError_ as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)
