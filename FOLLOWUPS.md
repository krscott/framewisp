# Follow-up candidates

These are notes for later issues, not requirements for this MVP.

## Observed during implementation

- Swell Foop 50.0 runs as a Flatpak without runtime changes. An immediate
  screenshot after clicking Let's Play still showed the welcome screen; a later
  capture showed the board. Subsequent move, Undo, and Redo captures worked with
  a 0.5-second wait. Use this game to investigate input/render synchronization
  when a fixed delay becomes insufficient.

- Replace the 100 ms VNC connection delays if broader reliability is
  needed. Without it, Sway dropped the first character and single named-key
  operations on new connections. The MVP waits before sending input. Consider
  an explicit focus/readiness signal or a persistent input connection later.
  Scroll commands also wait 100 ms before disconnecting: GTK otherwise sometimes
  dropped queued wheel events after the transient pointer device was removed,
  reporting `gdk_seat_get_pointer: GDK_IS_SEAT (seat)` failures.
  The runner now keeps an idle VNC connection open because Qt context menus
  disappeared when the last client disconnected. Reassess the existing delays
  with this persistent keyboard and pointer before changing them.

- Investigate VNC screenshots with Sway 1.12, wlroots 0.20.2, wayvnc 0.10.1, and
  vncdotool 1.2.0 under Pixman. The RFB handshake and input worked, but full-frame
  capture timed out. wayvnc logged capture setup without delivering image data.
  Setting an explicit output refresh rate and toggling output power did not fix
  it. grim works, so the MVP uses grim rather than a dependency patch.
- Decide whether to standardize on direct Wayland tools for input as well as
  screenshots, or return to a common RFB interface if X11 support becomes useful.
- GTK warns that it cannot acquire a session bus. The demo works without one.
  Add a private D-Bus session when a real target app requires it.
- vncdotool uses `bsp` for BackSpace. Unsupported names can leave the operation
  waiting until its timeout. Keep public key names explicitly mapped when
  expanding keyboard support.
- Test other applications and environments before expanding compatibility claims.
  The current implementation deliberately uses the host's font/theme setup.

## Capabilities

- X11 plus a window manager; Cage and other compositors; more toolkit coverage
  beyond the GTK demo, Swell Foop, and KolourPaint.
- MCP, RPC, background sessions, persistent framebuffer management, and concurrent sessions.
- Smooth movement before clicks (including tracking the pointer across CLI calls),
  distance-based speed, easing, press durations, and random
  timing or paths if a real test needs them.
- Configurable double-click timing, smooth scrolling, mouse buttons beyond left
  and right, more named keys, held keys,
  Unicode, input methods, clipboard support, and broader input validation.
- Audio, streaming, recording profiles, encoder backpressure, video/event timestamp alignment,
  cursor rendering policy, and structured action/process event logs.
- Screenshot frame metadata, input/render synchronization, visual stability,
  and application-specific readiness checks.
- Desktop portals, GPU features, accessibility queries, and multiple monitors.

## Reliability and packaging

- Contain arbitrary descendants and applications that detach from their parent.
- Recover from runner crashes, SIGKILL, stale session metadata, or interrupted
  startup; define ownership and cancellation during overlapping operations.
- Add private app profiles, config isolation, and a D-Bus policy for real apps.
- Decide authentication and isolation requirements for shared machines or remote use.
- Configure DPI, fonts, themes, locale, and keyboard layout for
  reproducible screenshots across hosts.
- Measure startup/input/capture costs before adding caches or persistent connections.
- Broaden package compatibility beyond the tested x86_64 Linux environment.
