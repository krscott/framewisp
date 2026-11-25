# One input connection for the whole session

The runner of a headless session opens one VNC input connection at startup and
keeps it until the session stops, instead of connecting for each command. With
a connection per command the seat lost its keyboard and pointer between
commands, which makes Qt dismiss an open context menu (R-INPUT-1), and every
command paid a handshake and a focus pause (see
[input latency](../input-latency.md)).

## Consequences

Losing the connection ends the session. No input is replayed, because the
runner cannot tell what the app already received.
