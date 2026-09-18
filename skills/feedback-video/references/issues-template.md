# <slug> review, <date>: open issues from the recording

TL;DR: <one paragraph: how many points came out of the recording, how many are
open, how many are already fixed, and whether any are blocked by the platform rather
than by the code.>

Source: `<date>-<slug>-transcript.txt` and `<date>-<slug>-frames-N.jpg` in this
folder. Recording: `<source video filename>`.

## Status

| # | Feedback (time in recording) | Status | Notes |
|---|---|---|---|
| A | <PLACEHOLDER: short description> (<mm:ss>) | Open | <PLACEHOLDER, or "cause unknown"> |
| B | Save button too small to hit on the settings screen (0:20 to 0:50) | Fixed on this branch | Root cause below. |
| C | <PLACEHOLDER, item carried from an earlier review: keep its letter and status, never guess a new one> | <carried status> | <carried note> |
| D | <PLACEHOLDER, one row per point, lettered in the order raised in the transcript. A new point always starts "Open".> | Open | |

## What was said

- <mm:ss> "<exact quote from the transcript, word for word>" → item <letter>
- 0:20-0:50 "the save button is too small" → item B

## Also visible, not said (reader's observation)

- <mm:ss>, tile `sheet-1 #4`: <what the frame shows that the transcript does not
  mention. Mark this as your own read, not something the user said.>

## <letter>. <short title of the issue>

<PLACEHOLDER for any item that needs more than the Status row: what the
timestamp/quote or tile shows, the fix if one exists already, why it is blocked if
it is. No root cause unless code was actually read; "cause unknown" is a fine
entry on its own.>

### Root cause of B

<PLACEHOLDER, filled in only when code was read. Example:>
The button's hit area used the icon's bounds instead of the 44 pt minimum, so taps
near the label missed.

## Suggested order

1. <PLACEHOLDER: what to look at or fix first, in priority order, referencing the
   lettered items above.>
