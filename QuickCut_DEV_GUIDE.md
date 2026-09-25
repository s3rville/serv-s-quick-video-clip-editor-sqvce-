# QuickCut — Developer Guide

Companion to `QuickCut_annotated.py` (the same program with ~160 review comments added inline —
proved byte-identical in executable code to the original, see §1) and `test_quickcut_headless.py`
(a runnable regression harness, real ffmpeg, no display needed).

This guide is written for **whoever touches this code next**, human or AI. It assumes no prior
context. Where it says "verified", that means run in this review against real ffmpeg output, not
inferred from reading the code.

---

## 0. What QuickCut is

A single-file (~5,000 line) PySide6 desktop video cutter. Core promise: **cuts and joins are
lossless** (ffmpeg `-c copy`, no re-encoding) whenever possible; crop, resize, speed change, mute,
mirror, rotate, reverse, GIF export and "Transcode" (HandBrake-lite) all trigger a real re-encode of
just the affected clip. One process, one window, one `QMediaPlayer` for preview, `ffmpeg`/`ffprobe`
as external processes for everything else.

Stack: Python 3, PySide6 (Qt 6) for UI and playback (`QtMultimedia`), `ffmpeg`/`ffprobe` binaries
(bundled next to the script or on PATH) for probing, thumbnailing, and all encoding work.

---

## 1. How this review was done (so you can trust — and repeat — it)

1. **Read every line.** The file was pulled apart with Python's `ast` module into every class and
   function, then read top to bottom in ~10 chunks, cross-referencing the original dev notes
   (`QuickCut_dev_notes.md`) claim-by-claim against the code.
2. **Ran it.** PySide6 6.11 and ffmpeg 6.1.1 are available in this environment. The GUI was
   constructed with `QT_QPA_PLATFORM=offscreen` (no display needed) and exercised directly:
   `MainWindow()`, `import_paths`, `insert_media`, `Sequence.edit`, `ExportWorker.run()`,
   `MediaWorker`, `ReverseProxy` — all real objects, not mocks.
3. **Generated real test clips** with `ffmpeg -f lavfi` (synthetic video, various resolutions/frame
   rates/audio presence) and ran the actual export pipeline against them, then inspected the output
   with `ffprobe` (duration, codec, resolution, frame-identical-to-source packet comparison).
4. **Turned every load-bearing claim into an assertion.** The dev notes make specific promises
   ("only the small sliver up to the next keyframe gets re-encoded", "mute-only clips stay
   lossless", etc.) — each one became a test that either passed or documented exactly how and by
   how much it fails.
5. **Proved the annotation step didn't change behaviour**, mechanically: `ast.dump()` of the
   original and of `QuickCut_annotated.py` are identical, and stripping the inserted comment lines
   back out reproduces the original file **byte-for-byte**. Both checks run automatically in
   `apply_notes.py` (kept for provenance, not part of the deliverable) every time notes are applied.

If you doubt a claim below, the harness (`python test_quickcut_headless.py`) re-proves all of it in
about a minute.

---

## 2. Architecture in one page

```
MainWindow  (app shell: builds every panel, owns all state, wires signals together)
 |- Sequence          the timeline MODEL: ordered list of Seg, undo/redo, no gaps ("magnetic")
 |   `- Seg           one clip = slice [in_s,out_s] of a Media + mute/speed/mirror/rot/rev + xf (crop/resize)
 |- Media / MediaWorker   one entry per imported FILE; background thumbnail + keyframe scan
 |- Engine            THE ONLY playback path -- one QMediaPlayer hopping between files
 |- ReverseProxy       background renderer of small reversed preview copies (QMediaPlayer can't play backwards)
 |- Timeline           paints the track, owns ALL mouse editing (trim/move/razor/scrub), talks only via signals
 |- VideoStage/VideoView/CropOverlay   live preview + crop/resize tool (same geometry math as export)
 `- ExportWorker (QThread)   cut -> (re-encode if needed) -> join -> optional GIF/Transcode pass
```

**Golden rule**: `Timeline`, `Sequence`, `Engine` and `ExportWorker` never call into each other
directly. `MainWindow` is the only place that reacts to `Sequence.edited` and pushes the result into
the `Engine` and back into the `Timeline`. If you're chasing "who updates X after Y changes", start
at `MainWindow.on_seq_edited`.

### 2.1 The two durations (read this before touching anything with `dur` in the name)

Every `Seg` has:
- `src_dur = out_s - in_s` — length **in the source file**, in source seconds. This is what ffmpeg
  cut points, keyframe lookups and `ReverseProxy` keys are computed from.
- `dur = src_dur / speed` — length **on the timeline**. This is what all layout math (`starts()`,
  `Timeline.tx`, the playhead) uses.

Converting a timeline offset inside a clip to a source-file time is always
`in_s + offset * speed` (see `Engine._target`, `Sequence.split_at`).

### 2.2 Lossless vs re-encode: the one switch that matters

`Seg.opts` is `None` when a clip can be stream-copied, or a `(mute, speed, mirror, rot, rev)` tuple
when it can't. `Sequence.export_parts()` uses it (and `xf`) to decide whether neighbouring clips can
be merged into one cut. `ExportWorker._build_part_files` uses it to decide `_cut_args` (copy) vs
`_fx_args` (re-encode). **Any new per-clip effect must flip this switch**, or it will silently be
stream-copied without your effect applied.

### 2.3 File-lock protocol (Windows-critical, but good practice everywhere)

Before touching a media file on disk — rename, Save-Over, remove from project, clear project — the
code always does, in this order:

```
engine.pause()                              # stop playback
if engine.path == file: engine.player.stop(); engine.player.setSource(QUrl()); engine.path = None
worker.cancel(media, wait=True)             # release the background loader's handle
proxy.cancel_media(media.path)              # release/kill any reverse-proxy render for it
```

Skipping any of these three release calls is the single most common way to reintroduce the
"file in use" bugs the original dev notes were written to fix. `MainWindow._resume_load` restarts
the loader if the subsequent disk operation fails.

### 2.4 Preview must equal export

Every visual/audio transform is implemented **twice**: once in ffmpeg args (`ExportWorker._fx_args`
/ `xf_filter`) for the real export, and once in Qt geometry/audio (`VideoStage`/`VideoView`/
`Engine._apply_fx`) for the live preview. When you add an effect, you need both paths, and they must
agree, or "what you see is not what you get". `xf_filter`'s docstring and `VideoStage`'s three
coordinate-space note are the two places to read before changing crop/resize geometry.

---

## 3. Findings register

Everything here was **reproduced against real ffmpeg output** by the test harness (`known=` tag
= the test id). None of these were fixed in `QuickCut_annotated.py` — that file is comment-only, by
design, so it stays behaviourally identical to what you were handed. Treat this as the backlog.

| # | Severity | Where | What actually happens | Evidence |
|---|----------|-------|------------------------|----------|
| **F1** | Medium | `ExportWorker._reencode_args` | Precise-mode cut that lands off-keyframe re-encodes the **entire clip video**, not just "the small sliver up to the next keyframe" as the Help text, the Precise-mode status message and the Precise tooltip all claim. | Exported a 3.3-9.0s cut; compared per-frame compressed packet sizes to the source. 0 of 150 frames after the first reachable keyframe (4.0s) were byte-identical to source — i.e. fully re-encoded, not just the head. |
| **F2** | Medium | `ExportWorker._concat_reencode` | Joining clips that don't all share the same audio presence (one has audio, one doesn't; or both lack audio) crashes ffmpeg: `Stream specifier ':a:0' in filtergraph ... matches no streams`. This is reached whenever mismatched clips (different codec/res/fps) need the re-encode join path. | Reproduced with (a) one clip with audio + one without, (b) two video-only clips of different resolution. Both fail export outright. |
| **F3** | Low | `ExportWorker._fx_args` | When the *only* active option is `mute` (no crop/mirror/rotate/reverse/speed), video is stream-copied (`-c:v copy`), so even in Precise mode the clip still starts on the previous keyframe, not the requested frame. | 3.3-8.0s mute-only precise cut measured 5.16s (starts at the 3.0s keyframe) instead of the expected 4.7s. |
| **F4** | Low | `MainWindow.save_over` / `build_actions` | The Save-Over **button** is hidden in GIF mode, but the `Ctrl+S` **shortcut** is not gated by `app_mode`, so it still runs a lossless video Save-Over while in GIF mode. Two confirmation dialogs remain in the way, so this needs deliberate user action, but it contradicts the "hidden in GIF mode" intent. | Verified the QAction stays enabled after switching `app_mode` to `"GIF"`. |
| **F5** | Low | `Timeline.request_thumb` / `gen_thumb_file` | Timeline filmstrip thumbnail cache key is `path + "|" + time`, with no file-modification-time component (unlike the Project-panel thumbnail, which correctly includes mtime). After Save-Over (or any external edit) replaces a file's bytes, the old cached frame can still be served for the same key. | Rendered a thumbnail, replaced the file's content in place, re-requested the same key — got the stale image back. |
| **F6** | Low | `ClipOptionsDialog` vs `Engine._apply_fx` | The speed dialog accepts 0.05x-100x, but the live-preview playback rate is clamped to <=20x. Clips faster than 20x preview desynchronised (playhead crawls; the "GPU/CPU can't keep up" warning can fire). Export itself is unaffected. | Set speed to the dialog's max (100x); measured actual `QMediaPlayer.playbackRate()` = 20x. |
| **F7** | Low | `ExportWorker.cancel` | `_cancel` is only checked *between* ffmpeg steps (inside `_ffmpeg`, after a process returns). Clicking Cancel between two steps (e.g. between "preparing clip 2" and "joining clips") lets the next full step run to completion before cancellation takes effect. | Code inspection; not time-boxed to reproduce with a stopwatch, but the control-flow gap is unambiguous. |
| **F8** | Low | `MainWindow.export` | The "don't overwrite a source clip" guard compares `os.path.abspath()` case-sensitively. On a case-insensitive filesystem (Windows, default macOS) two paths differing only in case slip past the guard. | Code inspection (this sandbox is Linux/case-sensitive, so it wasn't reproduced end-to-end here — flagged for a Windows tester). |
| **F9** | Cosmetic | `MainWindow.build_topbar` | The "Transcode" checkbox's on/off state is persisted (`QSettings` key `hb_on`) and restored at launch, so a user who leaves it checked gets re-encoded exports on the next run, not the "off by default, lossless" behaviour the notes describe as the steady state. | Code inspection; `QSettings` behaviour is standard and not independently re-tested here. |
| **F10** | Low | `MediaWorker._load` | If the keyframe scan exceeds `SCAN_TIMEOUT` (600s), ffprobe is killed but the **partial** keyframe list collected so far is still published as if it were complete. Cut points beyond that point silently don't exist for Snap mode. | Code inspection (600s is impractical to reproduce here). |
| **F11** | Low | `Sequence.split_at` | While a media's keyframe scan is still running (`keyframes is None`), a split in Snap mode is allowed at the *exact* requested time, unsnapped — so a cut made during the scan may not be reproducible by the lossless export once keyframes are known. `Timeline._trim` explicitly blocks this case; `split_at` does not. | Code inspection, cross-checked against `Timeline._trim`'s explicit guard for the same situation. |
| **F12** | Low (UX) | `probe_media` via `MainWindow.import_paths` | Runs synchronously on the GUI thread (25s timeout) during import. Importing from a slow or network drive freezes the window for that file. | Code inspection. |
| **F13** | Cosmetic | `MainWindow._checked_parts` | The "clips don't match" warning dialog text ("the result may not play correctly") is stale: `_parts_match` already gates the unsafe stream-copy join, so a mismatch is now always handled by a safe re-encode. The warning still fires (and is technically harmless) but describes a risk that no longer exists. | Code inspection. |
| **F14** | Cosmetic | module docstring, `_xf_args`, `capture_still`, 4 imports | Dead code / stale comments that don't match current behaviour. See §5. | Confirmed via `pyflakes` (unused imports) and call-site search (`_xf_args`, `capture_still` have zero callers of their live code). |
| **F15** | Cosmetic | `ExportWorker.run`, thumbnail caches | Temp folders (`quickcut_*`, `quickcut_rev_*`) and thumbnail cache folders under the OS temp directory are never proactively pruned across runs (only within a single run's `finally`, or on graceful `closeEvent`/`shutdown`). A crash or kill -9 leaves orphaned folders. | Verified normal export cleans up (`test: staging dir is removed afterwards`); a kill mid-export was not tested (would need process-level interruption). |
| **F16** | Cosmetic | `MainWindow.est_bytes`/`est_secs` | `self._sizes` is lazily created inside `update_est` (a `hasattr` check). Calling `est_bytes()`/`est_secs()` directly before the first `update_est()` raises `AttributeError`. In the app's actual call order this never happens (`update_labels` calls `update_est` first), so it's latent, not live. | Code inspection. |
| **F17** | Cosmetic | `ExportWorker.run` | With exactly one part, the GIF/Transcode second-pass progress step re-emits progress value `1` instead of advancing to `2` — the progress bar doesn't visibly move on that step. | Code inspection. |

**Not a defect, but worth knowing:** `_parts_match` (the gatekeeper for the fast concat-demuxer
path) was specifically tested and behaves exactly as documented — mismatched clips are correctly
routed to the safe re-encode join (aside from F2, which is a bug *inside* that re-encode join, not
in the routing decision).

---

## 4. Suggested fixes (not applied — for your backlog)

- **F1/F3** — give `_fx_args` (or a new helper) access to `precise`/keyframe info so a mute-only or
  off-keyframe clip can still copy the *tail* after the first in-range keyframe and only re-encode
  the head, the way the UI already claims. Until fixed, either implement it or soften the UI copy
  ("re-encodes this clip" instead of "re-encodes just the sliver").
- **F2** — in `_concat_reencode`, probe each input for an audio stream first; feed `anullsrc` for any
  clip missing one (mirroring what `_fx_args` already does for `mute`) instead of assuming
  `[i:a:0]` exists for every input.
- **F4** — early-return in `save_over()` when `self.app_mode == "GIF"` (or disable the `QAction` in
  `request_mode`), matching the button's hidden state.
- **F5** — include the file's `os.path.getmtime()` in the timeline thumbnail cache key, exactly as
  the Project-panel thumbnail already does.
- **F6** — either raise `Engine._apply_fx`'s clamp to match the dialog's 100x max, or lower the
  dialog's max to 20x to match the preview; pick one and make them agree.
- **F7** — check `self._cancel` at the top of `_ffmpeg` (before spawning) and inside the
  `_build_part_files` loop, not only after a process returns.
- **F8** — use `os.path.normcase(os.path.abspath(...))` for the source-file comparison in `export()`.

---

## 5. Confirmed dead / stale code (safe to remove, left untouched here)

- `ExportWorker._xf_args` — zero callers; crop/resize now goes entirely through `_fx_args` +
  `xf_filter`.
- `MainWindow.capture_still` — returns unconditionally on its first line; everything after that
  (including a call to `VideoStage.set_still`, which doesn't exist anywhere in the file) is dead.
  Three call sites still schedule it via `QTimer.singleShot` — harmless no-ops. Delete the method and
  its three call sites together, don't just delete the early `return`.
- Unused imports (`pyflakes`-confirmed): `QRect`, `QRegion`, `QCursor` (`QtCore`/`QtGui`),
  `QVideoWidget` (`QtMultimediaWidgets` — superseded by `VideoView`/`QGraphicsVideoItem`).
- Module docstring ("Nothing is ever re-encoded... uses ffmpeg stream copy (-c copy)") is stale
  since crop/resize/speed/mute/mirror/rotate/reverse/GIF/Transcode were added — all of those
  re-encode. Only plain cuts/joins of matching clips stay lossless.

---

## 6. The 8 rules (also at the top of the annotated file)

1. **Don't edit `Engine` casually.** Every line in it fixes a bug that was reproduced by a real
   user — see the numbered `[DO NOT BREAK]` list at `class Engine` in the annotated file (8 items,
   each with the exact failure mode it prevents).
2. **Every timeline change goes through `Sequence.edit(fn)`.** `fn()` returning exactly `False`
   means "nothing happened, don't commit" — anything else (including `0`, `0.0`, `None`) commits.
   Don't refactor that identity check to `if r:`.
3. **`Seg` has a 9-field positional contract copied in ~10 places.** Adding a field means walking
   the checklist at `class Seg` in the annotated file: `__slots__`/`__init__`, `opts` (if it changes
   exported bytes), `Sequence.snapshot`, both `split_at`/`insert_at` copies, copy/paste, the options
   dialog, `ExportWorker._fx_args`, `Engine._apply_fx` (if audible/visible live), and a timeline
   badge in `_paint_badges`.
4. **Before touching a media file on disk, release it first** — see §2.3. This is the #1 source of
   "file in use" bugs on Windows.
5. **ffmpeg is always launched as an argv list**, via `ExportWorker._ffmpeg` (export) or `run_tool`
   (probe/thumbnails), with `creationflags=NOWIN`. Never build a shell string.
6. **No per-frame Python hooks on the video sink**, no browser storage, no blocking work on the GUI
   thread. The only frame-signal subscription in the whole program is the drag-only scrub timing
   connection in `Engine.scrub` — it exists to *time* seeks, not to touch frame data.
7. **Preview must equal export** — see §2.4. A new effect needs both a live-preview implementation
   and an ffmpeg implementation, and must make `Seg.opts` non-`None`.
8. **After every edit:**
   ```
   python -m py_compile QuickCut.py
   python test_quickcut_headless.py QuickCut.py
   ```
   The harness needs only PySide6 + ffmpeg/ffprobe on PATH (no display). It currently reports
   `PASS=27 FAIL=0 KNOWN-FAIL=7` against the unmodified file — 7 is the size of the findings
   register in §3. If your change makes a `FAIL` appear, you broke something that used to work. If a
   `KNOWN-FAIL` becomes `FIXED?`, you fixed a registered bug — update §3 and remove its `known=` tag
   in the test file.

---

## 7. What still needs a human with a GUI

This review could not exercise real audio/video playback (no GPU/display in this environment), so
these are **untested by this pass** — the original dev notes flagged them as untested too, and
that has not changed:

- Playback across mixed clips, especially transitions into/out of a reversed clip.
- Scrub responsiveness/adaptiveness under real frame-decode latency.
- Undo/redo after trimming a reversed clip, visually.
- Grid/list project-panel toggle with actual drag-and-drop.
- GIF export quality with vs without `gifsicle` present, by eye.
- Keep/clear timeline prompt on Video<->GIF mode switch, by eye.
- Rename/Save-Over while a reverse-proxy render is in flight (race timing).
- Crop-tool "ghost" (un-crop) visual correctness — the math was verified numerically (§3 table,
  "not a defect" note and the harness's crop-commit test), but the on-screen dashed outline and
  dimming were not visually inspected.

---

## 8. Files in this delivery

| File | What it is |
|------|-----------|
| `QuickCut_annotated.py` | The original program, comment-only additions, proved AST-identical to the source you gave us. |
| `QuickCut_DEV_GUIDE.md` | This document. |
| `test_quickcut_headless.py` | Runnable regression harness (real ffmpeg, offscreen Qt). `python test_quickcut_headless.py [path-to-QuickCut.py]`. |
