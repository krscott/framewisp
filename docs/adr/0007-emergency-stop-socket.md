# The emergency stop has its own socket and takes a process handle from the kernel

`framewisp --stop-sharing` must work when the runner of a desktop share is suspended
or unresponsive. It connects to a socket that is separate from the command
socket, checks that the peer is the same user, takes a pidfd for the runner
with `SO_PEERPIDFD`, signals it, and waits for it to exit. A stop sent over the
command socket was rejected because it can queue behind blocked workers or a
stopped process. A PID read from a file was rejected because PIDs are reused.

## Consequences

`framewisp --stop-sharing` needs Linux 6.5 or newer. The user binds it to a desktop
shortcut themselves, because not every desktop offers the GlobalShortcuts
portal; COSMIC did not when this was tested.
