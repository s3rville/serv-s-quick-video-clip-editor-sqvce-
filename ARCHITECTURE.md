# QuickCut — module map (v48 split, patch 2)

**Version: 49.5**

This is `SQVCEversion47.py` split into modules so an AI assistant (or you) only has
to open the 1–2 files relevant to a task instead of a 6,300-line file. **No code was
rewritten** — every line was moved verbatim; only import statements were added.
Diff it against v47 if you want to confirm behavior is identical.

Paste **this file** at the start of a dev session instead of the code. Then say
which feature/bug you're touching and share only the matching file(s) below.

## Working conventions (read every session)

- **Bump the version line above by +0.1 on every change** (bug fix, refactor, new
  feature — anything that touches a module file), and add a one-line entry to the
  changelog at the bottom of this file describing what changed and in which
  module(s).
- **Only output/send the module file(s) actually changed or added in a given
  turn** — never resend the whole project or re-zip everything unless the user
  explicitly asks for the full project or a full zip. This is the main reason
  this file exists: keep token spend proportional to the size of the change, not
  the size of the project.

## Changelog

- 49.5 — VU meter: hold instead of fade when paused, and reflect scrubbing:
  - `widgets.py` (`VUMeter`): now tracks play state (`set_playing`). While actually playing, behaves as
    before (instant attack, gradual decay via the tick timer). While paused, the tick timer stops
    decaying it entirely and `set_level` writes the reported level straight through with no smoothing -
    so pausing holds exactly that frame's level instead of fading to zero, and a level reported while
    scrubbing the playhead (paused) updates the meter immediately, up or down, rather than only ever
    climbing to a max. Whether a paused seek actually produces a fresh audio buffer to report is up to
    the Qt multimedia backend - the same one already responsible for updating the paused video frame
    during scrubbing.
  - `main_window.py`: wired `Engine.playStateChanged -> VUMeter.set_playing`.
- 49.4 — 1 feature, 1 regression fix:
  - `main_window.py`: the VU meter next to the Timeline is 30% wider (26px -> 34px).
  - `media_model.py`: restored the 49.1 auto-group-naming fix ("Group1", "Group2", ...), which got
    silently reverted in 49.3 - that turn started `media_model.py` fresh from the very first upload (to
    add the new audio fields) instead of from the last edited copy, losing the unrelated naming change
    in the process. Nothing else from before 49.3 was affected - it was the only edit `media_model.py`
    had prior to that turn.
- 49.3 — Clip Options reorganized into Video/Audio sections + real multi-track audio support (touches every
  file in the "add a per-clip field" checklist, plus probing/export/preview/UI):
  - `media_model.py`: `Media` gains `audio_streams` (list of `{"lang", "name"}` per audio stream, filled by
    `probe_media`). `Seg` gains 3 new per-clip fields - `atracks` (tuple[bool], one per audio stream, default
    all-on), `vol_db` (float, +0 = unity), `track_type` ("stereo"/"mono") - threaded through `__slots__`,
    `__init__`, `opts`, `Sequence.snapshot()` and `Sequence.split_at()` per the field checklist at the top of
    the file. `opts` only turns non-None for audio reasons when a track is off, volume != 0, or mono AND the
    source actually has audio - untouched clips are completely unaffected.
  - `probing.py`: `_probe_audio_streams` now reads every `Stream #0:N: Audio:` line from `ffmpeg -i` (plus a
    `title` metadata tag when present) instead of just the first one, so multi-track sources are enumerated.
  - `dialogs.py`: `ClipOptionsDialog` reorganized into bordered "VIDEO" (speed/mirror/reverse) and "AUDIO"
    (mute, track list, volume, track type) sections. New: a scrollable checkbox list of the clip's audio
    tracks (named from the source's title tag, else "Audio Track N"); a volume slider (`DblSlider`, -20..+20
    dB, double-click resets to +0, live "+N dB" label); a Stereo/Mono "Track Type" combo. `values()` now
    returns 7 items (was 4) - `edit_clip_options`/`copy_selected`/`paste_clip` in `main_window.py` updated to
    match.
  - `export_worker.py` (`_fx_args`): selecting a subset of tracks maps only those; selecting more than one
    but not all mixes them into one output stream via `-filter_complex ... amix` (every downstream step -
    concat, `_parts_match`, `ReverseProxy` - assumes one audio stream per clip); Volume becomes a `volume=`
    filter; Track Type "Mono" adds `-ac 1`. The untouched/full-track-selection default is byte-identical to
    the old unconditional `-map 0:a?` (still `-c:a copy`-able) even when a source has multiple audio streams -
    only a genuine partial multi-select forces the mixdown/re-encode.
  - `playback.py` (`Engine`): new `audioLevel(l, r)` signal - taps decoded audio via `QAudioBufferOutput`
    (Qt >= 6.8; wrapped defensively, degrades to "meter stays at zero" on older Qt) and computes per-channel
    RMS from the raw buffer (stdlib `array`, no numpy). `_apply_fx` now also applies `vol_db` to
    `QAudioOutput.setVolume` for live preview.
  - [KNOWN ISSUE] `atracks` / mono downmix are EXPORT-only - the live preview always plays the source's
    default audio (documented in `ClipOptionsDialog`'s docstring); `vol_db` previews via `QAudioOutput` but
    clamps at +0 dB (unity) since `QAudioOutput` cannot exceed 1.0 - export is not capped.
  - `widgets.py`: new `VUMeter` - a simple two-channel bar with instant attack / smooth decay + peak-hold,
    driven purely by `Engine.audioLevel`.
  - `main_window.py`: `build_timeline_panel` now places a `VUMeter` to the left of the Timeline (in its own
    row) and wires `engine.audioLevel -> vu.set_level`. Added `QFrame#optSection` to the QSS for the dialog's
    section boxes.
  - `timeline.py` (`_paint_badges`): added a "+NdB"/"-NdB" pill (mirrors the existing rotation-degree pill)
    and a small "M" pill when Track Type is Mono.
- 49.2 — 1 feature, 2 bugfixes:
  - `timeline.py`: right-click menu on a grouped clip gains "Change Group Color..." (next to Rename
    Group / Ungroup), opening a color picker seeded with the group's current color
    (`Timeline.change_group_color`) - re-tints the bottom bar, name pill, and 49.1's 20% body tint.
  - `timeline.py`: dragging a clip body no longer "lifts" it the instant the mouse is pressed - it now
    waits until the mouse actually moves past `MOVE_ARM_PX` (4px) before lifting/switching to the
    drag cursor. Fixes a visible jerk when simply single- or double-clicking a clip without dragging.
  - `dialogs.py`: the Clip Options speed slider (`DblSlider`) now jumps straight to wherever it's
    clicked or dragged instead of Qt's default page-step-toward-click behavior, making it precise to
    click on; double-click-to-reset-to-1x is unaffected (handled separately, still wins).
- 49.1 — 2 features:
  - `timeline.py`: a grouped clip is now tinted with 20% opacity of its own group color over the whole
    clip body (`Timeline._draw_clip`), on top of the existing bottom color bar + name pill.
  - `preview_stack.py`: the Crop/Resize "Reset" button now requires two clicks - the first arms it
    (label flips to "Confirm"), the second actually fires the reset (`VideoStage._on_reset_clicked`).
    Anything that closes/reopens the tool session (switching tools, OK, Cancel) disarms it back to
    "Reset" (`VideoStage._disarm_reset`) so "Confirm" never lingers into a later session.
- 49.0 — 4 features + 3 bugfixes, all in `timeline.py` / `media_model.py` / `preview_stack.py`.
  (Note: `timeline_-_Copy.py`, uploaded alongside these in the same batch, is a stray OLDER accidental
  upload - not used as a base for anything below; `timeline.py` remained the correct/current file
  throughout this patch. An earlier draft of this changelog entry said otherwise - disregard that.)
  - `timeline.py`: playhead now snaps onto a clip start/end within 8px while scrubbing the ruler
    (`Timeline._snap_seek_t`, used by both the initial ruler click and the drag in `mouseMoveEvent`).
  - `timeline.py`: grouping two different existing groups (or a selection that only partially overlaps
    one) is refused (`Timeline._group_selectable`, checked in both `group_selected` and the right-click
    menu's enable condition) so a group can no longer end up split with a second group interleaved into
    the gap.
  - `timeline.py`: a clip can no longer be trimmed or dragged/reordered with the mouse while Crop or
    Resize is the active tool — editing the very clip mid crop/resize would change the frame out from
    under the tool. Selection (click / ctrl-click / right-click menu) still works; only starting a
    trim/move drag is blocked (`Timeline.mousePressEvent`).
  - `media_model.py`: new groups are auto-named "Group1", "Group2", ... (`Sequence.new_group`) instead
    of blank — still renameable via F2 / right-click "Rename Group...".
  - `preview_stack.py`: Crop/Resize pan (drag empty space, or now also middle-click-drag anywhere,
    `CropOverlay.mousePressEvent`) is unbounded at any zoom level, including fit — the old clamp/wall
    (`mx`/`my` in `VideoStage.relayout`) is gone.
  - `preview_stack.py`: added a "Reset" button in the action bar between Cancel and OK
    (`VideoStage.b_reset`) that puts position and size back to the clip's original starting point (no
    crop/resize at all) — wired to the same `resetRequested` signal the right-click menu's "Reset crop
    && resize for this clip" already used.
  - `preview_stack.py`: the background-color swatch (`b_color`) is now hidden while the Resize tool is
    active (`VideoStage.relayout`) — still shown for Crop.
- 48.1 — initial split of SQVCEversion47.py into 10 modules + main.py
- 48.2 — fixed missing `IMAGE_CLIP_DUR`/`NOWIN`/etc. constants block in `utils.py`; switched every module to cumulative `from X import *` for all modules before it in load order (was missing some deps, e.g. `preview_stack.py` needed `dialogs.py` for `icon()`)
- 48.3 — fixed `by_path` KeyError in `main_window.py` (`remove_medias` and `rename_media` assumed the dict key equals `m.path`, which is false for baked image clips); added this working-conventions section
- 48.4 — fixed `find_tool()` in `utils.py`: it only searched next to `utils.py` itself, but the split moved it one folder deeper (`quickcut_split/`) than the old single-file script, so `ffmpeg.exe`/`ffprobe.exe` left next to the project folder were no longer found — this force-disabled the Snap/Precise checkboxes (`setEnabled(bool(FFPROBE))`). Now also checks the parent and grandparent folders before falling back to PATH
- 48.5 — 6 bugs + 1 feature:
  - `main_window.py`/`timeline.py`: playhead can no longer be scrubbed (ruler click/drag) and playback can no longer be (un)paused/stepped while Crop or Resize is active (`MainWindow._tool_locked`, `Timeline.mousePressEvent`/`mouseMoveEvent` ruler guard)
  - `timeline.py`: clips magnetically snap to the playhead within ~8px while trimming (`_trim`) or reordering (`_move`); reorder-snap only takes a boundary already adjacent to the mouse's own choice, so it can't jump over an obstructing clip
  - `media_model.py`: `Sequence.insert_at` no longer splits the clip under an inserted/dropped clip — it now always pushes to that clip's nearer edge instead (this is what fixed the "insert cuts the clip at the playhead" bug, since insertion time was usually mid-clip when done from the Project panel while the playhead sat there)
  - `main_window.py`: right-click "Remove from Project" now removes the whole current selection (matching the Delete key) instead of only the row that was clicked
  - `timeline.py`: grouping a selection that only partially overlaps an existing group is now refused (`Timeline._group_selectable`) — previously this created a new group interleaved with the remaining part of the old one
  - `widgets.py` (`ProjectList`): a right-button drag over a row no longer shows a (nonfunctional) drag pixmap — dragging is now disabled for the duration of a right-button press
  - `widgets.py` (`ProjectList`): holding left click on a Project row now reorders it within the list (works in both list and grid view) instead of only being usable to drag a file onto the Timeline
- 48.6 — reverted the 48.5 "reorder Project row by drag" feature (`ProjectList.dropEvent`/`_row_index`): `ProjectRow`'s drag is started with a blocking `QDrag(...).exec()` still on the call stack inside its own `mouseMoveEvent`; the reorder code called `takeItem()`/`setItemWidget()` on that same row from inside `dropEvent`, i.e. while it was still the active drag source — corrupting/crashing it. Everything else from 48.5 (right-click-drag disable included) is unaffected/kept.
- 48.7 — 3 features:
  - `main_window.py`: Space is now handled in `MainWindow.keyPressEvent`/`keyReleaseEvent` instead of a `QShortcut`, so a tap can be told apart from a hold. Tap while paused/playing still starts/pauses playback as before; **holding** Space down while already playing instead ramps to 2x speed for as long as it's held (`SPACE_HOLD_MS` = 280 ms grace period), reverting to normal speed - still playing - on release. `playback.py`: added `Engine.set_speed_boost(mult)` / `Engine._rate_boost`, applied in `_apply_fx` (multiplies the clip's own `speed`, clamped same as before) and accounted for in `_monitor`'s expected-rate check so the boost doesn't falsely trip the "can't keep up" warning. Purely a live-preview effect - `Seg.speed` and export are untouched.
  - `timeline.py`: the playhead now magnet-snaps onto a clip edge when the ruler is clicked/dragged (scrubbed) within 8 px of one (`Timeline._snap_seek_t`), mirroring the existing trim/reorder-to-playhead magnet in `_trim`/`_move` but in the opposite direction.
  - `main_window.py`: Crop/Resize now has keyboard shortcuts - Enter = OK, Esc = Cancel (both handled in the same `keyPressEvent`, only while `self.stage.tool is not None`, by clicking `VideoStage.b_ok`/`b_cancel` so they go through the exact same `committed`/`cancelClicked` -> `on_stage_commit`/`exit_tool` path as the mouse). Also: pressing "R" while Resize is already the active tool now rotates the clip 90° (`MainWindow._r_shortcut`) instead of re-entering Resize, matching the tool's own rotate button.
- 48.8 — 2 features, `preview_stack.py` only:
  - Crop tool gets a "Mode:" toggle button (`VideoStage.b_mode`/`crop_mode`/`set_crop_mode`), placed at the opposite end of the action bar from Cancel/OK with its own isolating gap so it doesn't read as one of them. Default **Video** mode is the existing (unchanged) un-crop-bounded-to-the-recoverable-picture behaviour; **Canvas** mode lets dragging an edge/corner go past the actual video into the background color, extending the output frame beyond the clip's own frame (`CropOverlay.mouseMoveEvent`'s bound `b` becomes the whole overlay, same freedom Resize already had, instead of `picture_rect().united(canvas_rect())`). Toggling the mode mid-edit discards any uncommitted drag (`self.pend = None`) so the new mode always starts from the last committed crop, never a half-finished selection from the other mode. Resets to Video every time the tool is (re)selected (`set_tool`).
  - Crop/Resize: holding left-click on empty space (not a handle, not inside the selection) now pans the view instead of doing nothing (`CropOverlay.mousePressEvent`'s new `"pan"` drag kind + `VideoStage.pan_by`). Reuses the same `_pan` the mouse-wheel zoom already drives, so it's naturally a no-op at fit zoom (`relayout()` re-centers pan whenever the view isn't actually zoomed in) - exactly the same clamping, just another way to drive it.
- 48.9 — decoupled canvas geometry from video-crop geometry (they were the same rect before this, which is why a Canvas-mode pad/crop could restore or re-hide a Video-mode crop):
  - `utils.py`: `Seg.xf` gains a persistent `clip_rect` (the portion of the picture actually visible), stored as 4 trailing elements — `xf` is now a 6-tuple (legacy), 7-tuple (+color) or 11-tuple (+clip_rect); `xf_clip(xf)` reads it, defaulting to `clip_rect == canvas_rect` for anything stored before this patch (verified byte-for-byte identical `xf_filter` output for every pre-existing clip shape). `norm_xf` now always emits the 11-tuple. `xf_filter` cuts the scaled picture down to `picture ∩ clip_rect` *before* the existing pad-to-canvas/crop-to-canvas math, so export visibility follows `clip_rect`, not `canvas_rect`/`picture_rect`.
  - `main_window.py` (`on_stage_commit`): Video-mode crop commits set `clip_rect` AND mirror it into `canvas_rect` (unchanged visible behaviour, un-crop v4 included). Canvas-mode commits change ONLY `canvas_rect`; `clip_rect`'s own size is untouched — it re-bases onto the new selection's origin exactly like `picture` already does, since every commit re-anchors the coordinate frame to the new canvas's top-left.
  - `preview_stack.py`: new `VideoStage.clip_rect()` / `visible_rect()` (picture ∩ clip_rect ∩ canvas) accessors. `relayout()` masks the native video widget to `visible_rect()` instead of always the full canvas (a Canvas-mode resize can no longer reveal or re-hide cropped video) and gives the `canvas` backdrop widget its own dynamic bg-color (was hardcoded black) so it shows through around a smaller `clip_rect`. `CropOverlay`'s drag bound/snap-target and the ghost "recoverable extent" indicator now key off `clip_rect` (Video mode) or `canvas_rect` (Canvas mode) instead of always `canvas_rect`.
  - No changes to `media_model.py` (`Seg.xf` stays one opaque tuple field — the per-clip-field checklist doesn't apply) or `export_worker.py` (`xf_filter`'s call signature is unchanged).

## Load order (each file only depends on files above it — no circular imports)

| # | File | What's in it |
|---|------|---------------|
| 1 | `utils.py` | Constants (`NOWIN`, `MIN_DUR`, `IMAGE_*`, `VIDEO_FILTER`), `FFMPEG`/`FFPROBE` lookup, `find_tool`, `run_tool`, `system_memory`, `quiet_ffmpeg_log`, `_replace_with_retry`, `fmt_tc`, `xf_color`, `norm_xf`, `xf_filter` |
| 2 | `media_model.py` | `Media`, `Seg`, `Sequence` (the magnetic timeline + undo/redo data model) |
| 3 | `probing.py` | `probe_media`, `gen_thumb_file`, `image_to_clip`, `MediaWorker` (background thumbnail/keyframe scan) |
| 4 | `export_worker.py` | `ExportWorker` (cut / fx / concat / GIF / transcode) |
| 5 | `dialogs.py` | `icon()`, `DblSlider`, `ClipOptionsDialog`, GIF presets (`gif_title`, `gif_builtin`, `GifPresetDialog`), HandBrake-lite presets (`hb_title`, `hb_builtin`, `HbPresetDialog`), `DragFileButton`, `ExportDoneDialog` |
| 6 | `widgets.py` | `TitleBar`, `Panel`, `ProjectRow`, `ProjectList` |
| 7 | `preview_stack.py` | `std_ratio`, `nearest_ratio`, `CropOverlay`, `VideoView`, `VideoStage`, `WarningBar` |
| 8 | `timeline.py` | `Timeline` (painting + all mouse editing — the biggest single class) |
| 9 | `playback.py` | `ReverseProxy`, `Engine` (the ONLY code that touches `QMediaPlayer`) |
| 10 | `main_window.py` | `QSS` stylesheet, `dark_palette()`, `MainWindow` (wires everything together) |
| 11 | `main.py` | Entry point — `main()` and the `if __name__ == "__main__"` block. **Run this file.** |

## How each file is wired

Every module file re-declares the same stdlib + PySide6 import block at the top,
then does `from <module> import *` for **every module before it** in the load-order
table above (not just the ones it happens to use). This matches the flat, single
namespace the original file had — any name defined earlier in the original 6,316
lines is guaranteed available to code further down, exactly as before. Because the
load order is fixed and linear (no module ever imports one that comes after it),
this can't create a circular import.

## The 8 rules that keep this program from breaking (unchanged from v47)

1. Do not edit `Engine` casually: each of its lines fixes a reproduced bug.
2. Every timeline change goes through `Sequence.edit(fn)` — undo/redo and preview refresh depend on it.
3. `Seg` has a 9-field tuple contract copied in ~8 places — check every `Seg`-shaped tuple literal if you change its shape.
4. Before touching a media file on disk (rename / Save-Over / remove / clear) call BOTH `worker.cancel(...)` and `proxy.cancel_media(...)` — Windows keeps files locked otherwise.
5. ffmpeg is always launched with an argv LIST and `creationflags=NOWIN`, via `ExportWorker._ffmpeg` (export) or `run_tool` (probe/thumbnails). Never build shell strings.
6. No per-frame Python hooks on the video sink, no browser storage, no blocking work on the GUI thread.
7. Preview must equal export: a new visual/audio effect needs a live-preview path AND an ffmpeg path, and must make `Seg.opts` non-None so the clip is re-encoded instead of stream-copied.
8. After every edit run: `python -m py_compile *.py` and `python test_quickcut_headless.py` (if you still have the regression harness from the original project).

## Typical task → which file(s) to open

- **Crop/Resize tool bug** → `preview_stack.py` (CropOverlay/VideoStage) + `media_model.py` (Seg.opts contract)
- **Export/transcode bug** → `export_worker.py` + `dialogs.py` (GIF/HandBrake presets)
- **Timeline drag/snap/trim bug** → `timeline.py` + `media_model.py` (Sequence.edit)
- **Playback/scrubbing bug** → `playback.py`
- **UI layout / panel wiring** → `main_window.py` + `widgets.py`
- **New project-wide feature touching everything** → `main_window.py` first (it's the wiring hub), then whichever leaf module owns the actual logic

## Regenerating a single monolith file (if you ever need one)

If a tool of yours needs one flat file again, concatenate in load order (1→11
above), then de-duplicate the repeated import blocks at the top by hand — the
files were deliberately kept import-redundant for safety, not stripped.
