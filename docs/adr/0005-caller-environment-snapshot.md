# The caller's environment is snapshotted before the Nix wrapper runs

The installed `framewisp` command first runs a launcher that needs only the
standard library. It copies its exec environment from `/proc/self/environ` into
an anonymous memfd and then enters the Nix wrapper. The runner later builds the
app's environment from that snapshot, not from its own, and then applies the
session settings and, by default, the sanitized settings (R-ENV-1). Reading the
exec environment also leaves out the locale variables Python adds at startup.

## Considered options

- Undoing the wrapper's changes one variable at a time: rejected because it
  breaks whenever Nix adds, prefixes, or defaults another variable, and it
  cannot tell an unset variable from an empty one.
