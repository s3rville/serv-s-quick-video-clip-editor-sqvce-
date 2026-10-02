# SQVCE n4.0 - architecture (lean map)

**Version: 52.20**  |  PySide6 video editor, split into modules (see load order). Full history: `CHANGELOG.md` (archive - do NOT send it).

## Working conventions (read every session)
- **Bump the version +0.1 on every change** and: (1) add the full entry at the TOP of `CHANGELOG.md`, (2) add ONE line to "Recent changes" below and drop the oldest line there.
- **Only output the module files changed/added in a turn** - never resend the project or a zip unless asked.
- **Only the user's session needs: this file + the files for the task** (table below). Do not ask for / read other files "to be safe". Inline `[MAP]` / `[INVARIANT]` / `[PITFALL]` comments in each file explain local details.
- After edits run `python -m py_compile *.py`. Qt is usually NOT available in the AI sandbox: say what is untested.
- Underscore names (`_replace_with_retry`) are NOT exported by `from x import *` - import them explicitly.

## Which files to send (token budget)
Sizes are approximate. Send ARCHITECTURE.md always, then ONLY the row that matches the task (add a second row if the task spans both).

| Task | Send | ~KB |
|---|---|---|
| Timeline UI: drag/trim/snap/select/menus/painting/thumbnails | `timeline.py` + `media_model.py` | 90 |
| Data model, undo/redo, split/insert, Seg fields | `media_model.py` (+ `utils.py` if `xf`/crop maths) | 25-46 |
| Playback, scrubbing, audio preview, speed/volume | `playback.py` (+ `media_model.py`) | 63-88 |
| Export / transcode / GIF / concat | `export_worker.py` (+ `dialogs.py` only for presets) | 32-86 |
| Crop / Resize tools, preview stage | `preview_stack.py` (+ `utils.py` for `xf_*`) | 51-72 |
| Text tool (layers, overlay, dialog) | `text_tool.py` (+ `timeline.py` only if lane hooks change) | 37-102 |
| Audio track (strip above video, mixing) | `audio_track.py` (+ `timeline.py` for geometry/mouse) | 15-80 |
| Blank clips / still images / probing / thumbnails | `probing.py` + `main_window.py` (only the touched methods if you can paste them) | 19+ |
| Clip Options dialog, GIF / HandBrake presets | `dialogs.py` (+ `media_model.py` for Seg fields) | 54-79 |
| Mods loader / plugin API | `plugins.py` (+ the mod) | 13+ |
| Project panel / title bar / docks widgets | `widgets.py` (+ `main_window.py`) | 22-148 |
| Preferences, keybinds, naming scheme | `utils.py` + `main_window.py` | 147 |
| Global wiring, menus, shortcuts, import/export launch, anything cross-cutting | `main_window.py` (126 KB - largest; paste only the relevant methods when possible) | 126 |
| Crash recovery / crash logs | `recovery.py` / `crashlog.py` (+ `media_model.py` for the snapshot layout) | small |
| New per-clip field | `media_model.py` + `main_window.py` + `export_worker.py` + `recovery.py` (+ `playback.py`/`timeline.py`/`dialogs.py` if preview/badge/UI) - follow the checklist atop `media_model.py` | all |

Rarely needed: `widgets.py`, `dialogs.py`, `plugins.py`, `recovery.py`, `crashlog.py`, `main.py` (14 lines). `main_window.py` imports every module, so ask for it only when wiring changes.

## Load order (each file only imports files above it - no circular imports; every file does `from <module> import *` for all earlier ones)
| # | File | Contents |
|---|------|----------|
| 1 | `utils.py` | constants (`NOWIN`, `MIN_DUR`, `IMAGE_*`, `VIDEO_FILTER`), `FFMPEG/FFPROBE`, `run_tool`, `_replace_with_retry`, `fmt_tc`, prefs (`pref_json`), `KEYBIND_DEFS`/`keybind_map`, naming scheme, `xf_color/norm_xf/xf_filter` |
| 2 | `media_model.py` | `Media` (file; `blank`/`color` for blank clips), `Seg` (timeline clip), `Snap`, `Sequence` (magnetic timeline, undo/redo, `ext` providers) |
| 3 | `probing.py` | `probe_media`, `gen_thumb_file`, `image_to_clip`, `blank_clip`, `MediaWorker` |
| 4 | `export_worker.py` | `ExportWorker` (cut/fx/concat/GIF/transcode), `EXPORT_HOOKS`, `_bake_blanks` |
| 5 | `dialogs.py` | `icon()`, `DblSlider`, `ClipOptionsDialog`, GIF/HandBrake presets, `ExportDoneDialog`, `DragFileButton` |
| 6 | `widgets.py` | `TitleBar`, `Panel`, `ProjectRow`, `ProjectList` |
| 7 | `preview_stack.py` | `CropOverlay`, `VideoView`, `VideoStage`, `WarningBar` |
| 8 | `timeline.py` | `Timeline` (painting + all mouse editing; lanes + audio strip geometry; `snap_t/snap_span`) |
| 9 | `playback.py` | `ReverseProxy`, `ScrubProxy`, `Engine` (the ONLY code touching the main `QMediaPlayer`) |
| 10 | `plugins.py` | `ModInfo`, `PluginHost`, `ModAPI` (opt-in mods in `mods/`: tool / mode / tab) |
| 10b | `audio_track.py` | `AudioTrack` (strip above video: `AClip`, lane API, own preview player, export mix hook), `AudioDialog`, `is_audio_media` |
| 11 | `recovery.py` | `Recovery` autosave -> `recovery.json`, restore prompt |
| 12 | `crashlog.py` | crash reports in `crash_logs/`, faulthandler native crashes |
| 13 | `main_window.py` | `QSS`, `dark_palette`, `MainWindow` (wires everything) |
| 14 | `main.py` | entry point - **run this** |
`mods/text_tool.py` (shipped as `text_tool.py`) is a tool mod loaded by `PluginHost`, not part of the import chain.

## The 8 rules that keep this program from breaking
1. Do not edit `Engine` casually: each line fixes a reproduced bug. `self.player/audio/_sink` are the ACTIVE channel and swap at runtime - never cache them.
2. Every timeline change goes through `Sequence.edit(fn)` (undo/redo + preview refresh). Drags mutate in place and call `commit(before)` + `edited.emit()` on release. Code that mutates `segs`/in_s/out_s/speed in place must emit `live`/`edited`.
3. `Seg` has a 9+-field tuple contract copied in ~8 places - follow the CHECKLIST above `class Seg` in `media_model.py` (also `Recovery._build` + `_parse_clip`).
4. Before touching a media file on disk (rename / Save-Over / remove / clear) call BOTH `worker.cancel(...)` and `proxy.cancel_media(...)`.
5. ffmpeg: argv LIST + `creationflags=NOWIN`, via `ExportWorker._ffmpeg` (export) or `run_tool` (probe/thumbs). Never shell strings.
6. No per-frame Python hooks on the video sink, no browser storage, no blocking work on the GUI thread (blank/still bakes must stay tiny - a 120 s 30 fps bake froze the UI).
7. Preview must equal export: a new visual/audio effect needs a live-preview path AND an ffmpeg path, and must make `Seg.opts` non-None so the clip re-encodes instead of stream-copying.
8. New shortcut = add to `KEYBIND_DEFS` (utils) AND `_bind_fns` (main_window.build_actions); update the Help text.

## Extension points & cross-cutting invariants
- **Lanes (under the video row)**: `Timeline.lanes` objects with `height(tl)`, `paint(p,tl,y,W)`, `press/move/release/dbl`; optional `cursor`, `clear_sel`, `delete_key`, `duplicate_key`, `nav_edit(d)->time|None`, `marquee`, `snap_points`, `select_all`. `Timeline.atrack` = the audio strip ABOVE the row (same API); `Timeline._all_lanes()`; `V_Y` is an instance value moved by `top_h()` in `refresh_lanes` - use `self.V_Y/V_H`, never the class values. One selection across video/text/audio (select_only/`clear_sel`).
- **Lane snapping**: `Timeline.snap_t(t)` / `snap_span(t0,dur)` snap to video cut points (8 px) - use in every lane move/trim.
- **History for mods/lanes**: provider in `seq.ext[name]` with `ext_snapshot()` (comparable plain data) / `ext_restore(data)`; record with `before = seq.snapshot()` ... `seq.commit(before); seq.edited.emit()` only if state really changed. `MainWindow.clear_project` clears `atrack` and broadcasts `on_clear` to mods.
- **Export hooks**: `export_worker.EXPORT_HOOKS` objects `active(worker)` / `run(worker, src, tmpdir, ext) -> new path`; run after the lossless step, before GIF/transcode (text burn-in, audio mix). Blank clips are 1-fps preview stand-ins; `ExportWorker._bake_blanks` re-renders them at project fps/size.
- **Blank clips**: `Media.blank=True`, per-colour cache `MainWindow._blank_cache`, not in Project panel; double-click = colour only. **Audio files** (`is_audio_media`) go to the audio strip, never the video row (`MainWindow.insert_media`).
- **Mods**: contract in `plugins.py` header; `on_load/on_unload/on_clear/on_tool_selected/...`; loaded only when ticked in Preferences.
- **Startup order** in `MainWindow.__init__`: docks/timeline are built BEFORE `Engine` exists - anything needing `engine` (e.g. `AudioTrack`) is created after `Engine(...)`.
- **Recovery**: new per-clip field => `Recovery._build` + `_parse_clip` (+ bump `RECOVERY_VERSION` if incompatible). `crashlog.py` must never raise.
- **Engine/audio**: anything changing a clip's `vol_db` must re-run `_apply_fx`; `_rebind_audio` reads `self.audio/_sa` at call time; standby player is released by `_drop_standby()` (seek/pause).
- **Thumbnails**: cache key `f"{path}|{t:.1f}"` (keep the `path|` prefix - `clear_thumbs_for`); cached at full row height, scaled at draw.

## Known open issues
- Text and audio clips are NOT saved in `recovery.json`; they do not ripple when video clips are edited; audio strip is single-track, preview gain capped at 0 dB, no VU meter; text dialog Start/Duration can overlap clips on a layer.
- `atracks` / mono downmix are export-only (preview plays source layout). Boost >0 dB has ~60 ms latency in preview.
- Thumbnails stale after Save-Over (key has no mtime). Group edits / mode / primary / imports don't emit `edited` (saved with next edit). Two app instances share one recovery file.
- Save-Over "naming scheme" report (52.16) not reproduced. Most 52.x work is UNTESTED with Qt (sandbox has none).

## Recent changes (older: CHANGELOG.md)
- 52.20 - export_worker imports `_replace_with_retry`; text/audio snap to video edges (`snap_t/snap_span`); audio dialog slider; Ctrl+D duplicate (video / text / audio, `duplicate_key`).
- 52.19 - audio strip above video row (`audio_track.py`), blank clips draw flat colour, AudioTrack created after Engine.
- 52.18 - blank clip = 1-fps preview stand-in, real render at export (`_bake_blanks`).
- 52.17 - Up/Down navigates within the selected lane (`nav_edit`); Add Blank Clip (right-click, 4 s, colour-only options).
- 52.16 - text duplicate fits next free space; playhead snaps to lane items; overlay-hook replace raises on failure.
