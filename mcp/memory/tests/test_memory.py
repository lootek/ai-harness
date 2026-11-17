import json
import multiprocessing as mp
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import memory_engine as me  # noqa: E402

WHY = "Rule text.\n\n**Why:** because.\n\n**How to apply:** always."


@pytest.fixture
def st(tmp_path):
    (tmp_path / "MEMORY.md").write_text("- [Old](feedback_old.md) — old hook\n")
    (tmp_path / "feedback_old.md").write_text(
        "---\nname: cd to repo dir\ndescription: cd into the repo before git work\ntype: feedback\n---\n"
        "cd to the repo directory before git or glab commands, no subshells or git -C.\n\n"
        "**Why:** clean workflow.\n\n**How to apply:** cd first.\n")
    return me.Store(tmp_path)


def test_parse_old_and_new_frontmatter(st):
    m = {x.file: x for x in st.memories()}["feedback_old.md"]
    assert (m.name, m.type) == ("cd to repo dir", "feedback")
    meta, _ = me.parse_frontmatter(me.render_frontmatter("n", 'a "q": b', "project", {"modified": "x"}) + "b\n")
    assert meta["description"] == 'a "q": b' and meta["type"] == "project" and meta["modified"] == "x"


def test_type_inferred_from_filename(tmp_path):
    (tmp_path / "reference_x.md").write_text("---\nname: x\ndescription: d\nmetadata:\n  pinned: false\n---\nbody\n")
    assert me.Store(tmp_path).memories()[0].type == "reference"


def test_create_writes_file_and_index(st):
    r = st.create(name="d2 style", description="prefer d2 sketch elk theme 4", type="feedback", body=WHY)
    assert r["created"] and r["file"] == "feedback_d2_style.md"
    assert "- [d2 style](feedback_d2_style.md) — prefer d2 sketch elk theme 4" in st.index_text()
    assert st.lint()["problems"] == {}


def test_create_validates(st):
    with pytest.raises(me.MemoryError_, match="Why"):
        st.create(name="x", description="d", type="feedback", body="no why")
    with pytest.raises(me.MemoryError_, match="type must be"):
        st.create(name="x", description="d", type="bogus", body="b")
    with pytest.raises(me.MemoryError_, match="already exists"):
        st.create(name="CD to repo dir", description="other", type="reference", body="b")


def test_duplicate_refused_then_forced(st):
    args = dict(name="cd into repo first", description="cd into the repo directory before git work",
                type="feedback", body="cd to the repo directory before git commands, no subshells.\n\n"
                                       "**Why:** x.\n\n**How to apply:** y.")
    r = st.create(**args)
    assert r["created"] is False and r["candidates"][0]["file"] == "feedback_old.md"
    with pytest.raises(me.MemoryError_, match="force_reason"):
        st.create(**args, force=True)
    r = st.create(**args, force=True, force_reason="replaces the old one")
    assert r["created"] and r["forced"]


def test_secret_gate(st):
    with pytest.raises(me.MemoryError_, match="secret"):
        st.create(name="tok", description="d", type="reference", body="token glpat-abcdefghijklmnopqrstuvwx")
    h = st.view("feedback_old.md")["hash"]
    with pytest.raises(me.MemoryError_, match="secret"):
        st.str_replace(file="feedback_old.md", old="cd first.", new="AKIAABCDEFGHIJKLMNOP", expected_hash=h)


def test_path_guard(st):
    for bad in ("../x.md", "sub/x.md", ".hidden.md", "x.txt", "MEMORY"):
        with pytest.raises(me.MemoryError_):
            st.view(bad)


def test_edit_needs_current_hash(st):
    with pytest.raises(me.MemoryError_, match="required"):
        st.str_replace(file="feedback_old.md", old="cd first.", new="cd.", expected_hash="")
    h = st.view("feedback_old.md")["hash"]
    st.str_replace(file="feedback_old.md", old="cd first.", new="cd before anything.", expected_hash=h)
    with pytest.raises(me.MemoryError_, match="changed since"):
        st.str_replace(file="feedback_old.md", old="clean", new="tidy", expected_hash=h)
    txt = (st.root / "feedback_old.md").read_text()
    assert "cd before anything." in txt and "modified:" in txt


def test_edit_must_be_unique_and_keep_frontmatter(st):
    h = st.view("feedback_old.md")["hash"]
    with pytest.raises(me.MemoryError_, match="exactly once"):
        st.str_replace(file="feedback_old.md", old="cd", new="x", expected_hash=h)
    with pytest.raises(me.MemoryError_, match="frontmatter"):
        st.str_replace(file="feedback_old.md", old="type: feedback", new="type: nope", expected_hash=h)


def test_description_change_updates_index_hook(st):
    h = st.view("feedback_old.md")["hash"]
    st.str_replace(file="feedback_old.md", old="cd into the repo before git work",
                   new="always cd into the repo first", expected_hash=h)
    assert "- [Old](feedback_old.md) — always cd into the repo first" in st.index_text()


def test_insert(st):
    v = st.view("feedback_old.md")
    st.insert(file="feedback_old.md", after_line=6, text="Extra line.", expected_hash=v["hash"])
    assert "Extra line." in (st.root / "feedback_old.md").read_text()
    with pytest.raises(me.MemoryError_, match="after_line"):
        st.insert(file="feedback_old.md", after_line=1, text="x", expected_hash=st.view("feedback_old.md")["hash"])


def test_supersede(st):
    st.create(name="git cwd rule", description="git -C is fine now", type="feedback", body=WHY)
    r = st.supersede(old_file="feedback_old.md", new_file="feedback_git_cwd_rule.md", reason="policy changed")
    assert r["removed_from_index"]
    old = (st.root / "feedback_old.md").read_text()
    assert "superseded_by: feedback_git_cwd_rule.md" in old and "**Superseded" in old
    assert "feedback_old.md" not in st.index_text()
    assert all(m["file"] != "feedback_old.md" for m in st.search("cd repo git"))
    assert any(m["file"] == "feedback_old.md" for m in st.search("cd repo git", include_superseded=True))
    assert st.lint()["problems"] == {}


def test_delete_moves_to_trash(st):
    h = st.view("feedback_old.md")["hash"]
    r = st.delete(file="feedback_old.md", expected_hash=h)
    assert not (st.root / "feedback_old.md").exists()
    assert (st.root / r["moved_to"]).exists() and "feedback_old.md" not in st.index_text()


def test_rename(st):
    st.rename(old_file="feedback_old.md", new_file="feedback_cd_repo.md")
    assert "](feedback_cd_repo.md)" in st.index_text() and st.lint()["problems"] == {}


def test_lint_finds_drift(st):
    (st.root / "project_orphan.md").write_text("---\nname: orphan\ndescription: d\n---\nsee [[nowhere]]\n")
    (st.root / "MEMORY.md").write_text(st.index_text() + "- [Gone](feedback_gone.md) — x\n")
    p = st.lint()["problems"]
    assert p["not_in_index"] == ["project_orphan.md"]
    assert p["index_points_to_missing"] == ["feedback_gone.md"]
    assert "dangling_links" not in p
    assert st.lint()["notes"]["dangling_links"] == ["project_orphan.md -> [[nowhere]]"]


def test_index_title_with_brackets_and_md_links(st):
    (st.root / "feedback_pfx.md").write_text(
        "---\nname: pfx\ndescription: d\ntype: feedback\n---\nsee [[feedback_old.md]] and [[cd-to-repo-dir]]\n")
    (st.root / "MEMORY.md").write_text(st.index_text() + '- ["[Claude suggestion]" prefix](feedback_pfx.md) — x\n')
    assert {l.file for l in st.index_lines()} == {"feedback_old.md", "feedback_pfx.md"}
    r = st.lint()
    assert r["problems"] == {} and "notes" not in r or not r["notes"]


def _worker(root, i):
    s = me.Store(Path(root))
    s.create(name=f"fact number {i}", description=f"unique topic zz{i}q", type="reference",
             body=f"body zz{i}q", force=True, force_reason="test")


def test_concurrent_creates_keep_index_consistent(st):
    ps = [mp.Process(target=_worker, args=(str(st.root), i)) for i in range(12)]
    [p.start() for p in ps]
    [p.join() for p in ps]
    assert len(st.index_lines()) == 13 and st.lint()["problems"] == {}


def test_mcp_stdio_roundtrip(st):
    env = {**os.environ, "AI_MEMORY_DIR": str(st.root)}
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "memory_search", "arguments": {"query": "repo git"}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "memory_view", "arguments": {"file": "../etc.md"}}},
        {"jsonrpc": "2.0", "id": 5, "method": "nope"},
    ]
    out = subprocess.run([sys.executable, str(HERE / "memory_mcp.py")], input="\n".join(json.dumps(m) for m in msgs),
                         capture_output=True, text=True, env=env, timeout=20).stdout.splitlines()
    r = [json.loads(l) for l in out]
    assert [x["id"] for x in r] == [1, 2, 3, 4, 5]
    assert r[0]["result"]["protocolVersion"] == "2025-06-18"
    assert {t["name"] for t in r[1]["result"]["tools"]} >= {"memory_create", "memory_supersede"}
    assert "feedback_old.md" in r[2]["result"]["content"][0]["text"]
    assert r[3]["result"]["isError"] is True
    assert r[4]["error"]["code"] == -32601
