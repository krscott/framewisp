# Each headless session has two private D-Bus daemons with activation off

Every headless session starts its own session bus and its own accessibility
bus, and neither can activate services. This keeps a session apart from the
user's desktop and from other sessions (R-INSPECT-1), and stops desktop
services from starting inside it.

## Consequences

An app that needs service activation runs under its own nested
`dbus-run-session`, as the README describes. Its services see the variables
that an `env` placed before `dbus-run-session` on the app command injects, and
the bus is part of the process tree that framewisp starts for the app command,
so stopping the session ends it.
