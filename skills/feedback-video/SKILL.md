---
name: feedback-video
description: Turn a spoken feedback recording into a timestamped transcript and stamped contact sheets, then write an issues doc from them, at small-model cost instead of frontier-model cost. Pulls the newest recording from a Meta Quest over adb, or takes any local video file. Use when the user says "check the video on the oculus", "I recorded feedback", "new feedback video", "review my recording", "latest video on the device", "watch my quest recording", "I left you a video", or points at a fresh recording of their app.
---

# Feedback video

The user records a spoken walkthrough of their app instead of typing bug reports. This
skill turns that recording into a transcript and a handful of stamped frames, then has
one cheap model read them and write an issues doc. A five-minute video costs one Sonnet
pass and a few images instead of the session model watching video.

TL;DR: run the script, wait for it in the background, hand the transcript and sheets
to a Sonnet subagent to write the issues doc, then read only that doc yourself and
check its quotes before replying with the link.

## Steps

1. State the target: the project path, and which recording the script picked. If the
   newest recording on the device belongs to a different app than the current
   project, ask before going on.
2. Run the script from the project root, in the background. Whisper takes a minute or
   two even on a short clip, so wait for the exit instead of polling. Start it with
   the tool's background option or with a shell `&`, never both: doubled up, the tool
   reports "exited 0" while whisper is still running.
3. Hand the reading to one Sonnet subagent. Give it the transcript, the sheets, the
   manifest, `references/issues-template.md`, and the project's earlier feedback docs
   so known issues keep their letters and status. If the session itself is already
   running on Sonnet, read inline instead of spawning a subagent.
4. Read only the finished issues doc yourself. Check every quote in it against the
   transcript text before trusting it. Reply with a link to the doc and the lettered
   list.

## Command

Run from the project root:

```
python3 ${CLAUDE_PLUGIN_ROOT}/skills/feedback-video/scripts/feedback_video.py \
    [SOURCE] [--out docs/feedback] [--slug NAME] [--package com.example.app] \
    [--language en] [--cap N] [--redo STAGE]
```

If `$CLAUDE_PLUGIN_ROOT` is unset, fall back to the newest version under
`~/.claude/plugins/cache/feedback-tool-marketplace/feedback-tool/*/skills/feedback-video/scripts/feedback_video.py`.

`SOURCE` defaults to `latest`, the newest mp4 in `/sdcard/Oculus/VideoShots` on the
device, narrowed by `--package` if given. It can also be a filename on the device or
a local path to any video file ffmpeg can read (mp4, mov, mkv, webm). The script
prints the file it picked before doing anything else.

`--out` defaults to `docs/feedback` under the current directory. The script refuses
to guess when the working directory is not inside a git repo, so pass `--out`
explicitly when running from somewhere that isn't one.

Requirements: `ffmpeg`, the `whisper` CLI (openai-whisper), Python 3 with Pillow, and
`adb` only when pulling from a headset.

## Rules for the reader

These bind whoever writes the issues doc, subagent or inline session.

- Every item carries a timestamp, plus either a quote from the transcript or a tile
  reference.
- Two separate lists: what the user said, and what is visible but was not said. The
  second list is marked as the reader's own observation.
- No root causes unless code was actually read. "Cause unknown" is a valid entry.
- New items start as Open. An older item keeps its letter and carries its status
  forward; never guess a status for it.
- Open a full-size frame from the cache only when a tile is unreadable, and at most
  six frames per review.
- Prose: TL;DR first, plain hyphens, no filler.

## Privacy

Recordings often show a private space: a home, an unreleased app. Frames, video and
audio go to no artifact host and no outside service; they stay on this machine.

Before committing the sheets, check `gh repo view --json visibility`. In a public
repo, the sheets stay uncommitted, and the reply says so.

## Troubleshooting

| Situation | What to do |
|---|---|
| No device, or device unauthorized | Exit 2. Pass a local video path as `SOURCE` instead of pulling from the headset. |
| No mp4 matches | Exit 2. The output lists the five newest files on the device; check the app name and `--package`. |
| Script exits 2 asking for `--out` | The working directory is not inside a git repo. Pass `--out` explicitly. |
| ffmpeg, whisper or Pillow missing | Exit 3, naming the install command. Install it and rerun. |
| Whisper fails or finds no speech | The pipeline still runs on scene and fill picks; `manifest.json` has `"transcript": null`. Say plainly in the doc that the recording had no usable speech. |
