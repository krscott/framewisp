# The emergency stop has its own socket and takes a process handle from the kernel

`framewisp --detach` ends a desktop share even when its runner is suspended or
unresponsive. It connects to a socket that is separate from the command socket,
checks that the peer is the same user, takes a pidfd for the runner with
`SO_PEERPIDFD`, signals it, and waits for it to exit. A stop command on the
command socket was rejected because it can queue behind blocked workers or a
stopped process. A PID read from a file was rejected because PIDs are reused.
The other ways to end a desktop share (Ctrl+C, the sharing indicator, and
revocation through the portal) go through the runner and do not use this
socket.

## Consequences

`framewisp --detach` needs Linux 6.5 or newer. The user binds the
command to a desktop shortcut themselves, because not every desktop offers the
GlobalShortcuts portal; COSMIC did not when this was tested.
