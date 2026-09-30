"""Notes on the deploy runbook.

The operator runs `vault write secret/example/token value=...` by hand, then
`glab api --method POST projects/0000/merge_requests` to open the release MR.
Neither is performed by this module: it only renders the checklist.
"""


def checklist():
    return ["write the secret", "open the MR"]
