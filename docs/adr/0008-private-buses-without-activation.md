# Each headless session has two private D-Bus daemons with activation off

Every headless session starts its own session bus and its own accessibility
bus, and neither can activate services. This keeps a session apart from the
user's desktop and from other sessions (R-INSPECT-1), and stops desktop
services from starting inside it.

## Consequences

An app that needs service activation runs under its own nested
`dbus-run-session`, as the README describes. That bus starts after any `env`
on the app command, so its services see the variables the caller injected, and
it belongs to the app's process tree, so stopping the session ends it.
