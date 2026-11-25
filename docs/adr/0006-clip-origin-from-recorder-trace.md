# A clip's time origin comes from the recorder's protocol trace

Overlays, the console panel, and compaction need the time of a clip's first
frame on the same clock as the input log. For a clip that uses any of them, the
recorder runs with `WAYLAND_DEBUG=client`, and the first screencopy `ready`
event in its trace supplies that time. The recorder's start time and the
appearance of the MP4 header were rejected as guesses: the header is written
only after the first frame has arrived.

## Consequences

This relies on the pinned Sway stamping frames with the monotonic clock and the
pinned wf-recorder treating its first frame as time zero. Check both when
upgrading either.
