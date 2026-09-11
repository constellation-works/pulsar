# Hand-off: pulsar workspace ownership → Mac

pulsar's Orbit workspace was first initialized on dk-server-1 (2026-09-11) by
mistake; the connector, its browser auth, and its callers live on the Mac, so
the Mac is the owner. The box-side registration has been removed. The task
that tracked the v1 implementation is exported here so the Mac's store can
adopt it instead of re-filing.

`ORB-12124.tar.zst` — one task, exported with
`orbit task export --workspace ws_pulsar --ids ORB-12124`. Status `review`,
plan + execution summary + acceptance criteria included. Box ids are 10000+,
so it will not collide with the Mac's 0–9999 range; `--on-conflict=renumber`
is there as a belt-and-braces.

## On the Mac (once, from the constellation umbrella)

```sh
git -C ~/workspace/constellation pull                 # picks up the repos.tsv / INDEX rows
operations/scripts/bootstrap.sh                       # or: git clone https://github.com/constellation-works/pulsar.git codebases/pulsar
cd codebases/pulsar
orbit workspace init --base-branch agent-main --ship-mode local --mcp
orbit task import handoff/ORB-12124.tar.zst --workspace ws_pulsar --on-conflict=renumber
orbit tool run orbit.task.show --input '{"id":"ORB-12124","workspace":"ws_pulsar","model":"claude"}'
uv sync && make check
```

Then the human-only acceptance steps from the README: `uv run pulsar auth
login --client-id <id>` → `uv run pulsar auth status` → first live post.

After a successful import this directory can be deleted in a follow-up commit;
it is a transport container, not a record.
