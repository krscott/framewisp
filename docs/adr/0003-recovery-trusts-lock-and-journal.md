# Recovery trusts the session lock and the recovery journal, never stored PIDs

`recover` treats a session as dead only when it can take the session lock,
which the runner and every supervisor hold. It finds the runtime directory only
through the recovery journal, which records the device and inode of the session
directory, the lock, and the runtime directory. It never signals a PID read
from a file and never uses session metadata to authorise cleanup, because PIDs
are reused and metadata can be copied into another directory (R-LIFE-5,
R-LIFE-6).

## Consequences

When recovery cannot verify what it reads, it removes nothing and prints manual
steps (R-LIFE-7). The lock file must stay in place between sessions, because
replacing it leaves recovery holding a different inode from a live session.
