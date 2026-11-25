# A supervisor per managed process

Each managed process runs under its own supervisor: a small subreaper that
finds its children through procfs and signals them through pidfds. Detached and
double-forked descendants reparent to it, so stopping a session ends them too
(R-LIFE-1), and a supervisor finishes the job when the runner is killed.

## Considered options

- cgroups or a systemd user service: rejected because they need root, cgroup
  delegation, or a running user service, and framewisp must work without any of
  them.
- A process group: rejected because a descendant that starts its own session
  leaves the group.

## Consequences

Headless sessions need Linux 5.3 or newer, readable procfs, and permission for
`prctl`, `pidfd_open`, and `pidfd_send_signal`. Work that an app asks an
already running external service to do is outside the tree and is not ended.
