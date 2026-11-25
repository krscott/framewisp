# Clips are published by hard link, with no copy fallback

The recorder and finalization work on a private file in a directory beside the
clip destination, and the finished clip reaches the destination through one
hard link. wf-recorder truncates its target when the first frame arrives, and
finalization renames over the file it reads, so neither may touch the
destination. `link(2)` fails rather than replaces, so a file created after the
destination was checked is never overwritten (R-REC-1).

## Considered options

- Copying the finished clip when hard links are unavailable: rejected because a
  copy makes a partly written file visible at the destination. A destination
  that cannot be linked is refused when the clip starts (R-REC-19).
