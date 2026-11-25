# Ending a desktop share uses its own socket and a process handle from the kernel

`framewisp --detach` connects to a socket that is separate from the command
socket, checks that the peer is the same user, takes a pidfd for the runner
with `SO_PEERPIDFD`, signals it, and waits for it to exit. A stop request on
the command socket was rejected because it can queue behind blocked workers or
a stopped process. A PID read from a file was rejected because PIDs are reused.

## Consequences

Ending a desktop share this way needs Linux 6.5 or newer. The user binds the
command to a desktop shortcut themselves, because not every desktop offers the
GlobalShortcuts portal; COSMIC did not when this was tested.
