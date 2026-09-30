#!/usr/bin/env python3
"""Write the synthetic fixture corpus for the payload_guard test suite.

Every fixture is invented for the suite: placeholder hosts (example.com),
placeholder ids, no real project, ticket, cluster or host names. None of them
came from the corpus the guard was tuned against, so the suite is a genuine
holdout and is safe for a public repository.

Each fixture that contains a destructive command starts with an inert guard
line, because a published repo must not ship a runnable `dd if=/dev/zero
of=/dev/sda`. The guard reads files as text, so the guard line changes nothing
about what is detected.
"""
import os
import stat

SH_INERT = 'exit 0  # fixture: scanned as text, never executed\n'
PY_INERT = 'raise SystemExit("fixture: scanned as text, never executed")\n'

# name -> (body, expected)  expected in {"ask", "allow"}
FIXTURES: dict[str, tuple[str, str]] = {
    # ── must ASK ────────────────────────────────────────────────────────────
    "dd_wipe_disk.sh": (SH_INERT + '''
set -euo pipefail
TARGET_DISK="/dev/sda"
echo "zeroing ${TARGET_DISK}"
dd if=/dev/zero of=${TARGET_DISK} bs=1M count=100
''', "ask"),

    "mkfs_new_volume.sh": (SH_INERT + '''
set -eu
mkfs.ext4 /dev/sdb1
mount /dev/sdb1 /mnt/data
''', "ask"),

    "sudo_privileged_ops.sh": (SH_INERT + '''
set -eu
sudo usermod -s /sbin/nologin serviceuser
sudo systemctl restart example-agent
''', "ask"),

    "vault_write_secret.sh": (SH_INERT + '''
set -eu
export VAULT_ADDR="https://vault.example.com"
vault write secret/example/app-config retention=30 owner=team-example
''', "ask"),

    "vault_kv_put.sh": (SH_INERT + '''
set -eu
vault kv put kv-example/project0000/environments/example/config value=placeholder
''', "ask"),

    "curl_post_json.sh": (SH_INERT + '''
set -eu
curl -sS -X POST "https://api.example.com/v1/widgets" \\
  -H "Content-Type: application/json" \\
  -d '{"name": "example", "size": 3}'
''', "ask"),

    "glab_post_mr_note.py": (PY_INERT + '''
import json
import subprocess

PROJECT = "0000"
MR = "1"
BODY = {"body": "example review note"}

r = subprocess.run(
    ["env", "-u", "GITLAB_TOKEN", "glab", "api",
     f"projects/{PROJECT}/merge_requests/{MR}/notes",
     "--method", "POST", "-H", "Content-Type: application/json",
     "--field", json.dumps(BODY)],
    capture_output=True, text=True,
)
print(r.returncode)
''', "ask"),

    "kubectl_delete_namespace.sh": (SH_INERT + '''
set -eu
kubectl delete namespace example-ephemeral --wait=false
''', "ask"),

    "terraform_destroy.sh": (SH_INERT + '''
set -eu
cd infra/example
terraform destroy -auto-approve
''', "ask"),

    "aws_s3_upload.sh": (SH_INERT + '''
set -eu
aws s3 cp ./build/output.tar "s3://example-bucket/artifacts/output.tar"
''', "ask"),

    "git_force_push.sh": (SH_INERT + '''
set -eu
git commit -am "rewrite history"
git push --force origin main
''', "ask"),

    "rm_rf_home_dir.sh": (SH_INERT + '''
set -eu
rm -rf "$HOME/example-workspace"
''', "ask"),

    "shutil_rmtree_home.py": (PY_INERT + '''
import os
import shutil

shutil.rmtree(os.path.expanduser("~/example-workspace"))
''', "ask"),

    "slack_post_message.py": (PY_INERT + '''
import os
import requests

requests.post(
    "https://slack.com/api/chat.postMessage",
    headers={"Authorization": f"Bearer {os.environ['EXAMPLE_TOKEN']}"},
    json={"channel": "C00000000", "text": "example status update"},
)
''', "ask"),

    "urllib_close_issue.py": (PY_INERT + '''
import json
import urllib.request

req = urllib.request.Request(
    "https://tracker.example.com/rest/api/3/issue/EXAMPLE-1/transitions",
    data=json.dumps({"transition": {"id": "31"}}).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)
with urllib.request.urlopen(req) as resp:
    print(resp.status)
''', "ask"),

    "urllib_method_from_variable.py": (PY_INERT + '''
import json
import urllib.request

BASE = "https://tracker.example.com/rest/api/3"


def call(path, payload, method):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(req) as resp:
        return resp.status


call("/issue/EXAMPLE-2", {"fields": {"summary": "renamed"}}, "PUT")
''', "ask"),

    "acli_comment_create.sh": (SH_INERT + '''
set -eu
acli jira workitem comment create --key EXAMPLE-3 --body "automated note"
''', "ask"),

    # ── must stay ALLOW ─────────────────────────────────────────────────────
    "curl_get_query.sh": ('''#!/usr/bin/env bash
set -eu
curl -sS -G "https://api.example.com/v1/search" \\
  --data-urlencode 'q=example' \\
  --data-urlencode 'limit=25'
''', "allow"),

    "curl_post_localhost.sh": ('''#!/usr/bin/env bash
set -eu
curl -sS -X POST "http://localhost:8080/api/reload" -d '{}'
''', "allow"),

    "requests_get_only.py": ('''import requests

session = requests.Session()
resp = session.get("https://api.example.com/v1/widgets", timeout=10)
print(resp.status_code, len(resp.json()))
''', "allow"),

    "flask_inbound_method.py": ('''from flask import Blueprint, request

bp = Blueprint("example", __name__)


@bp.route("/v<int:api_ver>/widgets", methods=["GET", "POST"])
def widgets(api_ver):
    if request.method == "POST":
        return {"created": True}, 201
    return {"items": []}
''', "allow"),

    "docstring_mentions_commands.py": ('''"""Notes on the deploy runbook.

The operator runs `vault write secret/example/token value=...` by hand, then
`glab api --method POST projects/0000/merge_requests` to open the release MR.
Neither is performed by this module: it only renders the checklist.
"""


def checklist():
    return ["write the secret", "open the MR"]
''', "allow"),

    "rule_table_fixture.py": ('''CASES = [
    ("rm -rf /", "bare recursive delete"),
    ("dd if=/dev/zero of=/dev/sda", "raw disk write"),
    ("vault write secret/x k=v", "vault write"),
    ("git push --force origin main", "force push"),
]


def labels():
    return [label for _, label in CASES]
''', "allow"),

    "mktemp_cleanup.sh": ('''#!/usr/bin/env bash
set -euo pipefail
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
echo "working in $TMP"
''', "allow"),

    "vault_login_jwt.sh": ('''#!/usr/bin/env bash
set -eu
export VAULT_ADDR="https://vault.example.com"
VAULT_TOKEN=$(vault write -field=token auth/jwt-example/login \\
  role="example-role" jwt="${EXAMPLE_JWT}")
export VAULT_TOKEN
vault kv get kv-example/config
''', "allow"),

    "jql_search_post.sh": ('''#!/usr/bin/env bash
set -eu
curl -sS -X POST "https://tracker.example.com/rest/api/3/search/jql" \\
  -H "Content-Type: application/json" \\
  -d '{"jql": "project = EXAMPLE", "maxResults": 50}'
''', "allow"),

    "acli_comment_list.sh": ('''#!/usr/bin/env bash
set -eu
acli jira workitem comment list --key EXAMPLE-3
''', "allow"),

    "fdisk_list_only.sh": ('''#!/usr/bin/env bash
set -eu
fdisk -l
lsblk
''', "allow"),

    "read_only_report.py": ('''import json
import pathlib


def main():
    rows = json.loads(pathlib.Path("input.json").read_text())
    for row in sorted(rows, key=lambda r: r["name"]):
        print(row["name"], row.get("count", 0))


if __name__ == "__main__":
    main()
''', "allow"),
}


def main() -> None:
    here = os.path.dirname(os.path.abspath(__file__))
    out = os.path.join(here, "tests", "fixtures")
    os.makedirs(out, exist_ok=True)
    expectations = {}
    for name, (body, expected) in FIXTURES.items():
        path = os.path.join(out, name)
        with open(path, "w") as fh:
            fh.write(body.lstrip("\n") if body.startswith("#!") else body)
        # Executable bit only where the suite invokes ./fixture, and only on the
        # inert ones.
        if name.endswith(".sh"):
            os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        expectations[name] = expected
    with open(os.path.join(out, "expected.json"), "w") as fh:
        import json as _json
        _json.dump(expectations, fh, indent=1, sort_keys=True)
    asks = sum(1 for v in expectations.values() if v == "ask")
    print(f"wrote {len(expectations)} fixtures to {out} ({asks} ask / "
          f"{len(expectations) - asks} allow)")


if __name__ == "__main__":
    main()
