"""Plan auth-health follow-ups from offline status and a complete Orbit task list.

This skill helper reads JSON on stdin and prints tool arguments on stdout.
It never reads files, calls a service, or executes the human remedy.
"""

from __future__ import annotations

import json
import sys
from typing import Any


def plan(status: dict[str, Any], tasks: dict[str, Any]) -> dict[str, Any]:
    """Return task additions, escalations, and existing per-account task IDs."""
    if tasks["truncated"] is not False or tasks["total"] != len(tasks["tasks"]):
        raise ValueError("a complete task list is required for auth-health dedupe")
    followups = []
    updates = []
    skipped = []
    for account in status["accounts"]:
        health = account["health"]
        if health == "healthy":
            continue
        if health not in ("unverified", "unhealthy"):
            raise ValueError("unknown account health")
        alias = account["alias"]
        tag = f"pulsar-auth-health:{alias}"
        attention = next(
            (note for note in status["attention"] if note.startswith(f"{alias}: ")),
            None,
        )
        if attention is None:
            raise ValueError(f"no attention remedy returned for {alias}")
        reauth = account["reauth_required"]
        if not isinstance(reauth, bool):
            raise ValueError("reauth_required must be a boolean")
        if reauth:
            title = f"Re-authorize pulsar account {alias} (reauth_required)"
        elif health == "unverified":
            title = f"Verify pulsar account {alias} (unverified)"
        else:
            title = f"Repair pulsar account {alias} (unhealthy)"
        evidence = "\n".join(
            f"- {key}: {json.dumps(account[key], ensure_ascii=False)}"
            for key in ("alias", "health", "token_state", "reason", "reauth_required")
        )
        description = (
            "Offline pulsar.status reported an account needing human attention.\n\n"
            f"{evidence}\n\nExact attention returned by pulsar:\n{attention}\n\n"
            "A human must perform the remedy at a terminal on the posting host, using "
            "the home and account named above. An agent must never run the remedy, "
            "refresh, log in, publish, or approve. Record human confirmation and the "
            "subsequent offline pulsar.status result on this task; write no files."
        )
        existing = [
            task for task in tasks["tasks"] if tag in task["tags"] and task["terminal"] is False
        ]
        if existing:
            skipped.append({"alias": alias, "task_ids": [task["id"] for task in existing]})
            for task in existing:
                if reauth and (task["priority"] != "high" or task["title"] != title):
                    updates.append(
                        {
                            "id": task["id"],
                            "title": title,
                            "priority": "high",
                            "comment": description,
                        }
                    )
            continue
        followups.append(
            {
                "title": title,
                "description": description,
                "priority": "high" if reauth else "medium",
                "type": "chore",
                "complexity": "low",
                "tags": ["pulsar", "pulsar-auth-attention", tag, "no-diff-expected"],
                "required_tools": ["pulsar.status"],
                "acceptance_criteria": [
                    "A human performed the exact attention remedy for the named account "
                    "on the posting host, or documented why it cannot be completed.",
                    "Human confirmation and a subsequent offline pulsar.status result "
                    "are recorded on the task; no agent refreshed, logged in, published, "
                    "approved, or wrote files.",
                ],
            }
        )
    return {"followups": followups, "updates": updates, "skipped": skipped}


def main() -> None:
    request = json.load(sys.stdin)
    result = plan(request["status"], request["tasks"])
    json.dump(result, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
