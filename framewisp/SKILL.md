# framewisp

Use framewisp to smoke-test GUI interactions, investigate visual bugs, or record
a short demonstration using screenshots and mouse/keyboard input.

Prefer `framewisp` on PATH. Otherwise replace `framewisp` in the examples with
`nix run github:krscott/framewisp --`. No checkout or development shell is needed.
The package includes `framewisp-demo`; other target apps must be installed separately.

Start an isolated demo in a terminal or background tool session. Use a fresh
session directory, wait for `Session ready:`, and keep the runner alive:

```sh
framewisp /tmp/framewisp-demo run -- framewisp-demo
```

From another command session, capture and inspect the PNG, then interact. These
coordinates target the bundled demo's text field:

```sh
framewisp /tmp/framewisp-demo screenshot /tmp/before.png
framewisp /tmp/framewisp-demo click 120 100
framewisp /tmp/framewisp-demo type 'Hello GUI!'
framewisp /tmp/framewisp-demo key Return
framewisp /tmp/framewisp-demo screenshot --delay 0.3 /tmp/after.png
```

Inspect the result before choosing the next action. Add `run --x11` for an X11
app. Stop the isolated runner with Ctrl+C or SIGTERM when finished.

To record just a demonstration, start recording after setup, perform the inputs,
then stop recording. Use a new output filename:

```sh
framewisp /tmp/framewisp-demo record-start /tmp/demo.mp4
# Perform the interactions to demonstrate.
framewisp /tmp/framewisp-demo record-stop
```

For an existing desktop, ask the user to run `framewisp SESSION attach` in their
own terminal and follow its instructions. Never start desktop attachment yourself.

Use `framewisp --help` or `framewisp SESSION COMMAND --help` for more options.
If something goes wrong, ask the user for permission before filing an issue at
https://github.com/krscott/framewisp/issues. Agree on the report and any logs or
screenshots to include before submitting it.
