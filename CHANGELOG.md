# SQVCE changelog (ARCHIVE)

Full per-version history, newest first. **Do NOT send this to the AI by default** - it is only for looking up
WHY something was built a certain way (grep the version/feature). ARCHITECTURE.md holds the distilled rules and the last few versions.
Working convention: after each change add the new entry at the TOP here (full detail) and a one-line summary to ARCHITECTURE.md "Recent changes" (drop the oldest there).

- 52.22 - cross-layer groups (grp on text/audio, select_group/expand_groups); box select from any lane; Alt+arrows never split a group; audio slider click/double-click; text overlay export x264 veryfast.

- 52.21 - video snaps to lane edges; blank playhead interpolated; images bake off-thread; blank inserts at right-click; box select across layers; Alt+Left/Right group moves; Up/Down blocked for mixed layers.

- 52.20 - (1) FIX export: `export_worker` now imports `_replace_with_retry` explicitly (`from x import *` skips underscore names; the 52.16 overlay-hook replace raised NameError). (2) Lane snapping: `Timeline.snap_t/snap_span` (8 px) - text and audio clips snap to video clip edges (every cut + end) when moving (start or end) and trimming (`TextLane.move`, `AudioTrack.move`). (3) Audio options dialog: dB slider synced with the spin box. (4) Ctrl+D (`KEYBIND_DEFS` id `duplicate`, `MainWindow.duplicate_selected`): lanes may define `duplicate_key() -> bool` (text: `TextLane.duplicate_key`, audio: `AudioTrack.duplicate_key` = next free space on the track, shortened to fit, never stacks, status hint when no room); otherwise the selected video clip(s) are copied right after the last selected one (ripple). Timeline right-click gets `Duplicate Clip` (`Timeline.duplicateRequested`).

- 52.19 - (0) Fix: `AudioTrack` is created right after `Engine` in `MainWindow.__init__` (the timeline is built before the engine). (1) Blank clips draw as ONE flat colour (no filmstrip/thumbnail area; label colour adapts) - `Timeline._draw_clip`. (2) AUDIO TRACK: NEW `audio_track.py` (load order 10b, after plugins; `main_window` imports it, `main.py` unchanged). Audio files (`is_audio_media`: no video stream or .mp3/.m4a/.wav/.aac/.flac/.ogg/.opus/.wma) dropped on the timeline or inserted from the Project panel (`MainWindow.insert_media`) become `AClip`s on ONE thin strip ABOVE the video row (`Timeline.atrack`; `Timeline.V_Y` is now an instance value moved by `top_h()` in `refresh_lanes`; `_all_lanes()` = lanes + atrack). Clips never overlap; drag = move, edges = trim, double-click / right-click = audio-only options (mute, volume dB), Delete, Up/Down (`nav_edit`) work; undo via `seq.ext['audio_track']`; `MainWindow.clear_project` calls `atrack.clear()`. Preview: its own `QMediaPlayer` follows `Engine.playStateChanged/playheadChanged` (gain capped at 0 dB). Export: `EXPORT_HOOKS` entry mixes clips with atrim/adelay/volume/amix (`normalize=0` needs ffmpeg >= 4.4). `utils.VIDEO_FILTER` lists more audio types. [KNOWN] not in recovery.json; no VU meter for the strip; single track only.

- 52.18 - Blank clip hang fix: the old 120 s all-intra 30 fps bake froze the GUI. `probing.blank_clip(..., fps=1)` is now a near-instant 1-fps stand-in used ONLY for preview (`MainWindow._blank_media` sets `m.fps` to the project fps); the real render happens at export: `ExportWorker._bake_blanks` (worker thread) re-bakes each blank at project fps/size and exact length and swaps it into a copy of `parts`. [KNOWN] the first blank of a colour still runs one short synchronous ffmpeg call (~0.2 s).

- 52.17 - (1) Up/Down edit navigation: lanes may define `nav_edit(d)` -> time or None; `MainWindow.goto_edit` asks every lane first, so with a text clip selected Up/Down steps to the previous/next text clip on the SAME layer (stops at the ends) instead of jumping to video clips (`text_tool.TextLane.nav_edit`). (2) Blank clip: timeline right-click > "Add Blank Clip" (`Timeline.blankRequested` -> `MainWindow.add_blank_clip`, 4 s at the playhead). `probing.blank_clip(color,w,h)` bakes a 120 s all-intra solid-colour mp4 (cached in temp); its `Media` has `blank=True`, `color`, dense `keyframes`, is NOT in medias/by_path (no Project row) and is cached per colour in `MainWindow._blank_cache`. Double-click a blank clip = colour picker only (`recolor_blank` swaps `seg.media` inside `seq.edit`); `Timeline._draw_clip` paints a flat colour and requests no thumbnails. [KNOWN] Blank clips are not restored from recovery.json as blank (they reload as the cached mp4); project size = first clip's size, else 1280x720.

- 52.16 - (1) Text mod `duplicate`: the copy goes to the next free space on the same layer (starts where the original ends, skips occupied spans, shortened to fit the gap); with no room left it opens a new layer at the same time. (2) `Timeline._snap_seek_t` also snaps the playhead to lane items' start/end (`lane.snap_points()`). (3) `export_worker.py`: the overlay hook's final replace now raises if it fails (was silently ignored). [KNOWN/OPEN] Save-Over "applies the naming scheme": `save_over()` contains NO naming-scheme code (it exports to `.<name>.quickcut_tmp<ext>` next to the primary and swaps it in; only export/export_gif use `name_scheme`). Not reproduced - needs the exact symptom from the user.

- 52.15 - Fixes + 2 shortcuts. (1) `probing.image_to_clip`: the baked still-image video (600 s) is encoded to `<name>.part.mp4` and renamed only when complete (a timed-out/killed ffmpeg used to leave a broken cached mp4 that later hung/crashed the import), with a far cheaper encode (`ultrafast`, `stillimage`, `-g 300`, 180 s timeout); zero-byte cache files are ignored. (2) `main_window.eventFilter`: Space is routed to `keyPressEvent`/`keyReleaseEvent` even when a button/list/checkbox has focus (not while typing in line/spin/text edits). (3) Shift+click the window X = close without the warning; Shift+click Clear Project = no confirmation (modifiers read via `QApplication.keyboardModifiers()`). (4) Up/Down edit navigation: `Timeline.select_only` also clears lane (text) selection; text overlay mask/repaint is now deferred (`TextOverlay.request_sync`, 0 ms timer) instead of running inside the engine's playheadChanged emit. [KNOWN] The exact cause of the Up/Down crash with a selected text was NOT reproduced (no Qt in the sandbox) - the two changes above are the likely culprits; if it still crashes send crash_logs/.

- 52.14 - (1) Text mod: a plain click on the preview with the Text tool creates text ONLY if layer 0 is free at the playhead (length capped by the next clip); otherwise it does nothing (status hint). Shift+click is the only way to add another layer. (2) ONE selection across all layers: selecting text (click, Ctrl+click, marquee, preview) clears the video selection (`State._drop_video_sel`) and clicking/marquee on video clears the text selection (Ctrl no longer keeps it); Ctrl+A is video-only again (`lane.select_all` hook no longer called). Text clips can still be multi-selected among themselves.

- 52.13 - Text layers + selection. (1) `mods/text_tool.py`: a LAYER (row) is now like the video row - clips on one layer never overlap. New text goes on layer 0 at the playhead, or right after / into the gap beside whatever occupies that spot; Shift+click = new layer; move/trim in the lane clamp against neighbours (`State.limits`), a vertical drag only changes layer if the target is free at that time. (2) Text clip selection box in the lane = same white 2 px rounded box as video clips; Ctrl+A also selects text (`lane.select_all`); Delete removes selected text AND the selected video clip together (`handle_delete_key`). (3) `timeline.py`: lanes may define `cursor(pos, tl)` (hover cursor; text clip edges show the resize cursor) and `select_all()`.
  [KNOWN] The Text options dialog's Start/Duration fields can still make clips overlap on a layer.

- 52.12 - (1) `main_window.py` `insert_media` / `paste_clip`: the playhead stays where it was after inserting/pasting a clip (was: jumped to the end of the inserted clip). (2) `mods/text_tool.py` ghosting: `TextOverlay.sync_mask` now makes the parent repaint the region that left the mask, and `TextOverlay.update` also repaints the parent under the overlay (stale text/handle pixels were left on the video).

- 52.11 - Mod history + clear + text placement. (1) `media_model.py`: `Snap(list)` (snapshot with `.ext`), `Sequence.ext` = {name: provider}; a provider has `ext_snapshot()` (comparable plain data) and `ext_restore(data)`; `snapshot()` stores every provider's state, `_restore()` hands it back, so Ctrl+Z/Ctrl+Shift+Z cover mods. A mod records a change with `before = seq.snapshot()` ... `seq.commit(before); seq.edited.emit()` (only when its state really changed). (2) `plugins.py` `PluginHost.broadcast(fn)`; `MainWindow.clear_project` broadcasts `on_clear(api)` to loaded mods (after the undo stacks are cleared). (3) Text mod: registers in `seq.ext`, records add/delete/duplicate/dialog/inline edit/lane drags/preview drags; `on_clear` empties its items; a new text now always starts exactly AT the playhead (was clamped back to `total - 4 s` near the end; duration is cut at the end of the video instead).
  [KNOWN] Text items still not saved in recovery.json.

- 52.10 - Text mod rework (`mods/text_tool.py`, plus tiny hooks). (1) New text starts selected, NOT in edit mode (double-click / Enter edits). (2) Items have an explicit layer (`TItem.row`): all new text goes on layer 0 (stacked/overlapping); Shift+click on the preview = new layer; dragging a text clip vertically in the lane moves it between layers (Shift = open a new one); layers compact automatically; higher layer = drawn on top (preview + export). (3) Delete: `MainWindow.handle_delete_key` first asks every lane `delete_key()`; preview + timeline right-click menus on text: Edit / Duplicate / Delete. (4) Lane rubber-band selection (+Ctrl add, multi-select, multi-move) and `Timeline._marquee_select` forwards to `lane.marquee(rect, tl, add)`; clicking outside lanes calls `lane.clear_sel(tl)`. (5) Select tool (V) can now pick/move/resize text in the preview: the overlay is masked to the visible text boxes (clicks elsewhere pass through) - `TextOverlay.sync_mask`. Border: dashed blue with the Text tool, solid yellow with Select. Optional lane methods: `delete_key`, `clear_sel`, `marquee`.

- 52.9 - Text tool mod + 3 core hooks. NEW `mods/text_tool.py` (tool mod "Text"). (1) `timeline.py`: **lanes** - `Timeline.lanes` list of objects with `height/paint/press/move/release/dbl` (strip under the clip row; `lane_h/lane_y/refresh_lanes`, `_lane_grab`); clip row height accounts for `lane_h()`. (2) `export_worker.py`: `EXPORT_HOOKS` (`active(worker)`, `run(worker, src, tmpdir, ext)`) = extra pass after step C, before GIF/Transcode (output is staged then replaced/copied to `out`). (3) `plugins.py`: any mod may define `on_load(api)`; `ModAPI.tl`, `ModAPI.stage`.
  Text mod: items (`TItem`, NOT `Seg`s, not undoable, not saved in recovery) on their own timeline lane (default 4 s, min 0.3 s, overlapping items stack in rows; drag body = move, edges = trim, double-click / right-click = options dialog: font, size (px at 1080p), bold/italic/underline, align, colour, background+alpha, outline, opacity, start/duration, box). Preview: `TextOverlay` child of `VideoStage` (stage.relayout is wrapped to keep it on top); with the tool active click the preview = new "Type Here" (Enter saves, Shift+Enter newline, double-click re-edits, drag body/corners/edges, Del). Boxes are stored as fractions of the displayed frame (rot/crop-agnostic). Export: each item painted by the SAME `paint_item` into a PNG, burned in with ffmpeg `overlay ... enable=between(t,..)` (re-encodes x264 crf17 / vp9 for .webm).
  [KNOWN] UNTESTED with Qt (no PySide6 in the dev sandbox). Items do not ripple when clips are edited, are lost on exit (not in recovery.json), and the preview position uses the displayed (post-crop/rotate) frame.

- 52.8 - Crash reports. NEW `crashlog.py` (load order 12, after recovery; needs only `utils`, imports `recovery` lazily). `install_crash_logging()` is the FIRST line of `main()`; `crash_set_window(win)` runs right after
  `MainWindow(...)`. Logs go to `<program folder>/crash_logs/` (fallback: `<user data>/QuickCut/crash_logs`, then temp if the program folder is not writable); newest 20 `crash_*.log` are kept.
  (1) `sys.excepthook` / `threading.excepthook` / `sys.unraisablehook` -> `crash_YYYYmmdd_HHMMSS.log` with versions, ffmpeg paths, traceback, project state (clips, mode, playhead, engine file, media list, RAM,
  recovery file), last 60 Qt messages (`qInstallMessageHandler`) and every thread's stack; the same error (type + last frame) writes at most 3 files per run; status-bar hint on the GUI thread.
  (2) Native crashes (segfault/abort): `faulthandler` writes into `crash_logs/native_crash.log`; the next start renames a non-empty one to `crash_<time>_native.log` (+ status-bar hint), empty ones are deleted,
  clean exit removes it. `write_crash_report(kind, *sys.exc_info())` can be called by hand (e.g. from a `try/except` that swallows an error but should leave a trace).
  [INVARIANT] Nothing in `crashlog.py` may raise; keep every state read in `_state_lines` wrapped in `g(...)`. The recovery file is untouched by crashes (it is what the restore prompt uses).

- 52.7 - Crash recovery ("save aborted project"; NOT a project save). NEW `recovery.py` (load order 11, after plugins, before main_window): `Recovery(QObject)`, `recovery_path()`
  (`GenericDataLocation/QuickCut/recovery.json`), format v1 JSON. (1) Autosave: `Sequence.edited` ONLY (never `live`) -> 2 s trailing debounce (max wait 10 s) -> payload dict built on the
  GUI thread -> one daemon writer thread (latest wins): json + tmp file + fsync + `os.replace` (`utils._replace_with_retry`). Empty timeline deletes the file. `MainWindow.autosave_lbl`
  ("Auto-Saving N%", next to "?", hidden except while writing). Saved: clips (all 13 Seg fields, media by PROJECT KEY = `by_path` key, i.e. the original picture for baked images), groups + `_next_grp`,
  Project media + primary, playhead, Keyframes/Precise. Not saved: thumbs, keyframes, proxies, undo, selection, zoom. (2) `MainWindow.closeEvent` -> `recovery.shutdown_clean()` (last line, after
  `super().closeEvent`). (3) Startup: `MainWindow.__init__` -> `singleShot(0, recovery.startup(files))`: prompt Restore/Discard first, THEN command-line files; corrupt / unknown-version / clip-less
  file = deleted silently. Restore = `import_paths` -> match clips to Media by key -> skip missing/changed files (one summary box) -> assign segs/groups, clear undo+redo, `_invalidate_geometry`,
  `edited.emit()`, mode, primary, zoom-fit, `engine.seek(playhead)`. `main.py` / `main_window.py`: `from recovery import *`.
  [INVARIANT] New per-clip field = also add it to `Recovery._build` and `_parse_clip` (they mirror `Sequence.snapshot()`), and bump `RECOVERY_VERSION` if the layout is not backward compatible.
  [KNOWN] Group edits / mode / primary / imports don't emit `edited`, so they are saved with the next committed edit. Two running instances share one recovery file. UNTESTED with Qt in the dev sandbox.

- 52.6 - Timeline selection. (1) `main_window.goto_edit` also selects the clip starting at the edit it lands on (`Timeline.select_only`, single clip, group ignored; last clip
  when landing on the end). (2) New keybind `select_all` (Ctrl+A, `KEYBIND_DEFS`) -> `MainWindow._select_all` -> `Timeline.select_all` (a focused list widget, e.g. Project, keeps
  its own select-all unless the mouse is over the timeline). (3) `timeline.py`: left-drag on empty timeline space = marquee selection (`mode == "marquee"`, `_marq`,
  `_marquee_select`; Ctrl adds to the current selection; grouped clips pull in their whole group; disabled for razor/crop/resize tools; rectangle drawn in `paintEvent`).

- 52.5 - (1) `utils.name_scheme()`: the old built-in defaults (`%pn_edit`, `{name}_edit`) saved by 52.0/52.1 count as "not customised" -> new default `%pn_%ra{4}`
  (that stale saved value was the "_edit" suffix); used by export_default/export_beside/Preferences. (2) `main_window.request_mode`: no confirmation dialogs when
  switching Video/GIF (timeline always kept). (3) New keybind `tool_plugin` (default Z, in `KEYBIND_DEFS`): `PluginToolStack.cycle()` activates the plugin tool, further
  presses cycle through the plugin tools. (4) Top bar order (right side): warnings | estimate | GIF combo+cog | Transcode presets+cog | Transcode checkbox | Save-Over | Export
  - presets open to the LEFT of the checkbox so it stays put; the estimate label is inserted at `est_idx` (left of the presets).

- 52.4 - `main_window.py`: Shift+click Export (video + GIF) now uses the naming scheme (`export_beside`: saved next to the source file, ignores the default
  export location, `_2`, `_3`... if taken) instead of `editN`.

- 52.3 - (1) `main_window.py` `export_auto`: with "Use a default export location" ticked (folder set) Export / GIF export save straight there without the Save
  dialog (scheme name, `_2`, `_3`... if taken); no/invalid folder -> dialog as before; Shift+click unchanged (`editN` next to the source). (2) `plugins.py`
  `PluginToolStack.paintEvent`: with >1 plugin tool the button gets an accent box + small down-arrow. (3) `utils.DEFAULT_SCHEME` = `%pn_%ra{4}` (an already
  saved scheme is kept until Reset).

- 52.2 - Mods rework. (1) Type names: old "tab" (title-bar tab beside Video/GIF) is now **"mode"** (`build_mode`/`on_mode_shown`); **"tab"** is now a
  WINDOW (dock panel like Project) via `build_tab(api)`: `MainWindow.add_mod_dock/remove_mod_dock/_place_mod_dock`, `mod_docks` dict (key `mod:file.py`, right of
  Preview, Panel header, follows lock/unlock, listed in the eye menu; position NOT persisted). (2) `plugins.py`: opt-in loading - `ModInfo.enabled` = file in
  QSettings `pref_mods_on`; no mod code is executed unless ticked; `PluginHost.sync()` (replaces `reload`) loads newly ticked mods live and unloads unticked ones,
  returning their names; `MainWindow.apply_prefs` returns that list and `PreferencesDialog._run_apply` warns that a restart is needed to fully unload
  (code stays imported). Startup calls `sync()` (only previously ticked mods load). (3) Interface tab: mini reset button (U+21BA) beside each colour picker.
  `mods/`: blank_mode.py (was blank_tab), new blank_tab.py (window), blank_tool.py. Legacy `pref_mods_off` is ignored (removed on Reset).
  [INVARIANT] `_default_dock_layout` re-places mod docks; `_validate_layout` ignores them.

- 52.1 - File naming scheme now follows "naming guide for ai.txt": tokens `%pn %mo %d %yy %yyyy %h %mi %s %ra{N} %rn{N}` (`utils.NAME_TOKENS`,
  `format_out_name`; default `%pn_edit`; `%pn` = source file name since the app has no project object; N capped 1..64; unknown text stays literal).
  `{n}`-style tokens and the "first free number" logic from 52.0 are removed. `dialogs.py`: General-tab token buttons/help text updated.

- 52.0 - Preferences + mods foundation. `utils.py`: `prefs()`, `pref_json`, `KEYBIND_DEFS` (ALL keybinds; ids -> handlers in `MainWindow.build_actions`),
  `keybind_map`, `THEME_DEFAULTS/theme_map`, `format_out_name` + `NAME_TOKENS` ({name} {n} {date} {time} {mode} {dir}), `MODS_DIR`. All stored in QSettings
  `pref_*` keys. `dialogs.py`: `PreferencesDialog` rewritten (General: default export folder + naming scheme; Keybinds table; Interface colour pickers;
  Plug-ins list; Credits links; footer RESET (confirm) | OK / CANCEL / APPLY -> `MainWindow.apply_prefs`); `TitleBar.add_tab/remove_tab/restyle` (mod tabs, accent
  from theme). NEW `plugins.py` (load order 10, before main_window): `ModInfo`/`scan_mods` (metadata read via `ast`, no execution), `PluginToolStack` (one
  stacked tool button; click again = scrollable icon list), `PluginHost` (load/unload/reload, tool/tab hooks), `ModAPI`. `main_window.py`: shortcuts are now
  QShortcuts from `keybind_map()` (Space/Enter/Esc via `_key_is` in keyPressEvent; `build_shortcuts` removed), `build_qss()`/`dark_palette()` use the theme,
  `export_default()` feeds the Export/GIF Save dialogs (Shift+click Export still ignores it), `page_stack` (page 0 = editor, mod tabs after; `request_mode` returns to 0).
  `main.py`: imports plugins, uses `build_qss()`. New folder `mods/` (blank_tool.py, blank_tab.py templates).
  [KNOWN] Timeline/preview painting colours and some dialog-local stylesheets keep fixed colours (only QSS/palette/title bar/prefs are themed).
  [INVARIANT] New shortcut = add to `KEYBIND_DEFS` AND `_bind_fns`; the Help text (`show_help`) still lists default keys.

- 51.13 — `main_window.py`: eye and "?" buttons take their height from the Preferences button (`sizeHint().height()`, padding 0) so the
  row is uniform. Panels hidden via the eye button remember their size: `set_panel_visible` stores (w, h) in `_dock_sizes` (QSettings
  `dock_sizes`, JSON) before hiding; on show `_restore_panel_size` (run at 0 and 150 ms) resizes Timeline by height / Project+Preview
  by width and updates `_tl_keeper.h`. `reset_layout` clears the stored sizes.

- 51.12 — (1) `timeline.py` `_draw_clip`: with thumbnails hidden the whole clip is filled with the solid band colour (badges/label unchanged).
  (2) `main_window.py` `TimelineHeightKeeper`: a window resize no longer changes the Timeline dock's height - the height/width delta goes
  to the Preview/Project row. A dock Resize arriving together with a new window size = window resize -> `resizeDocks` back to the last
  height seen at constant window size (i.e. the user's separator position). Active 1.5 s after start.
  (3) Eye button next to Preferences (`eye_icon`, `show_panels_menu`, `set_panel_visible`): checkable list Project/Preview/Timeline;
  unchecking hides that dock. Hidden set persisted in QSettings `hidden_docks`; `_validate_layout` ignores user-hidden docks;
  `reset_layout` clears the set (all panels shown again).

- 51.11 — `timeline.py`: fix ZeroDivisionError in `_draw_clip` when thumbnails are hidden (`n` was 0 and `tile_w = r.width() / n`). `n` is
  always >= 1 now; the tile loop is simply skipped when `show_thumbs` is False.

- 51.10 — `timeline.py`: the Timeline can be dragged down to 40% of its old minimum height (60% smaller). `V_H` is now an INSTANCE
  attribute (class value 92 = `V_H_FULL`) recomputed in `resizeEvent` from the widget height (min `BAND_H+1`); clip hit-testing/painting
  already read `self.V_H`. Below `THUMB_MIN_H` (58 px) `_draw_clip` draws no filmstrip and requests no thumbnails. Thumbnails are
  always cached at full-row height and scaled at draw time so resizing never re-decodes them.
  [INVARIANT] Use `self.V_H` (not `Timeline.V_H`) for anything vertical. Dock `_validate_layout` treats height < 40 as squashed; the
  smallest timeline dock (50 px widget + nav bar) stays above that.

- 51.9 — `main_window.py`: `mode_btn` is a plain (non-highlighted) button whose text shows the CURRENT mode ("Precise" default /
  "Keyframes"); clicking calls `set_trim_mode` which flips `seq.snap`/`seq.precise` and the text. `MODE_BTN_QSS` removed.

- 51.8 — `main_window.py`: Keyframes/Precise is now ONE checkable button `mode_btn` (on = Keyframes, off = Precise, default off;
  `on_mode_toggled` sets `seq.snap` and `seq.precise`); `snap_cb`/`precise_cb`/`mode_grp` are gone. The Help button is now a
  fixed-width "?" placed right after Preferences in the top bar.

- 51.7 — `main_window.py`: the Snap/Precise checkboxes are now checkable `QPushButton`s (`MODE_BTN_QSS`, blue when on), "Snap" renamed
  **Keyframes**. Same names (`snap_cb`/`precise_cb`) and exclusive `mode_grp`; Precise is on at start when ffprobe exists
  (already the default), otherwise Keyframes is forced on. Help/hint strings updated.

- 51.6 — Audible boosts without quieting other clips. Removes 51.4/51.5's project-wide headroom and `PREVIEW_MAX_BOOST_DB`.
  `dialogs.py`: volume slider back to +-20 dB (typed box still -200..+200, no warning note needed). `playback.py`: for a clip
  with gain > 0 dB `_apply_fx` sets `_boost_on`, silences the player's `QAudioOutput`, and `_on_audio_buffer` -> `_boost_write`
  amplifies the tapped decoded buffers (hard clip, like export) into an Engine-owned `QAudioSink` (default device, ~60 ms
  buffer). Flushed on pause/seek/device change (`_boost_flush`). Any failure sets `_boost_ok=False` -> unity playback.
  Requires the QAudioBufferOutput tap (Qt >= 6.8). Cuts (<= 0 dB) still use `QAudioOutput.setVolume`.
  [KNOWN] ~60 ms audio latency on boosted clips; 8-bit sources can't be boosted in preview; UNTESTED with Qt.
  [INVARIANT] `_boost_*` attrs are set in __init__ before `_apply_fx` can run.

- 51.5 — Honest preview limits. `utils.py`: `PREVIEW_MAX_BOOST_DB = 50`. `playback.py`: `MAX_HEADROOM_DB` uses it (was 24), so the
  whole slider range (+-50 dB) is previewed accurately. `dialogs.py`: `vol_note` warning under the volume row when a typed
  boost exceeds the preview limit ("Preview can only play up to +50 dB. Export applies the full X dB."). Export unchanged.
  [KNOWN] At a +50 dB boost the other clips play ~50 dB quieter in the preview (relative levels stay true; lower the constant
  if 16-bit output noise at that level bothers you).

- 51.4 — Fixes. `playback.py`: positive Volume works in the preview again - `_apply_fx` reserves headroom equal to the largest
  positive `vol_db` in the project (`_headroom_db`, cap `MAX_HEADROOM_DB` 24 dB; QAudioOutput tops out at 1.0), so clips play
  that much quieter and boosted clips regain it (relative levels right; other clips get quieter only when a boost exists).
  `_on_audio_buffer` multiplies the (pre-volume) tapped level by the current clip's gain so the meter follows Volume.
  `widgets.py`: `VUMeter.FLOOR_DB` -54 -> -45 (less sensitive).
  [INVARIANT] Anything that changes a clip's `vol_db` must re-run `_apply_fx` (edits already `seek()`) since headroom is project-wide.

- 51.3 — Silent scrub blips, VU meter rework, wider volume range. `playback.py`: scrub audio blips run at volume 0 (the
  audio buffer tap still feeds the meter); `_blip_stop` restores volume via `_apply_fx`. `widgets.py` (`VUMeter`): bars use a
  dB scale (`FLOOR_DB` -54 dB = empty, 0 dB = full; `_frac`) and a fixed green > yellow > red gradient by absolute level;
  decay 0.90/tick. `dialogs.py` (`ClipOptionsDialog`): volume slider range -50..+50 dB (was +-20) plus a `vol_spin`
  QDoubleSpinBox (-200..+200 dB, 0.1 steps) that is the source of truth for `values()`; slider pins at its ends for
  out-of-range typed values. Preview still caps at unity (QAudioOutput); export applies the full gain.

- 51.2 — Fix `AUDCLNT_E_DEVICE_INVALIDATED` when an audio device is unplugged/swapped (`playback.py`). `Engine._devs`
  (`QMediaDevices.audioOutputsChanged`, 200 ms debounce) -> `_rebind_audio` sets both `QAudioOutput`s (`audio`, `_sa`) to
  `defaultAudioOutput()` and re-seeks to restore position/play intent. Audio-device errors in `_on_error` are swallowed
  the same way (rebind, no error emitted). Outputs are also bound to the default device at startup.
  [INVARIANT] `_rebind_audio` iterates `self.audio`/`self._sa` at call time (they swap roles) - don't cache them.

- 51.1 — Audible scrubbing + middle-mouse pan. `playback.py` (`Engine._blip_kick/_blip_stop`, `BLIP_MS`): every scrub seek
  plays the player ~110 ms so audio is decoded (speakers + VU meter update while dragging the playhead); blip state
  reports are ignored by `_on_playback_state` (`_blip`/`_blip_guard`) so `playing`/play button never flip, and the
  position snaps back to the target on stop. Skipped when muted, playing, or a source load is pending.
  `timeline.py`: middle-button drag pans (`mode == "pan"`, `_pan_x0/_pan_s0`; press/`_process_move`/release).

- 51.0 — Renamed to **SQVCE n4.0** (`utils.APP_NAME`; change ONLY when the owner asks). Small grey name label centred in the
  custom title bar (`TitleBar.name_label`, `dialogs.py`). Topbar "QuickCut" logo replaced by a **Preferences** button (below the
  Video/GIF tabs, `main_window.build_topbar`) opening `PreferencesDialog` (`dialogs.py`): left tab list General / Keybinds /
  Interface / Plug-ins / Credits (placeholder pages). QSettings org/app stays "QuickCut" so saved settings keep working.

- 50.3 — Dock fixes (`main_window.py`). Duplicate titles: Panel header title hidden while unlocked. Drop lag/crash: AnimatedDocks
  off; empty title widgets are created once per dock (not re-created mid-drag); re-lock waits until the mouse is released.
  Lost panel: `_validate_layout` (hidden/floating/squashed dock -> default layout) runs at start and before saving; reset
  un-floats every dock and re-applies sizes after the event loop turns. [UNTESTED with Qt in the dev sandbox]

- 50.2 — Modular dock layout (`main_window.py` only). main/top splitters replaced by `build_docks()`: Project, Preview,
  Timeline are QDockWidgets in an inner QMainWindow (`dock_host`, hidden central widget). Locked by default (no title bars);
  right-click a panel header / dock title / separator / control bar -> Unlock/Lock layout, Reset layout. Unlock re-locks after
  one move. Layout persisted via `saveState` in QSettings `dock_layout` (on lock + closeEvent). No floating docks (frameless window).
  - [INVARIANT] Builders `build_project/build_video_column/build_timeline_panel` are now called from `build_docks` (still before Engine).

- 50.1 — Final perf pass. `Timeline.set_playhead` repaints only the thin strip around the old/new playhead (cache supplies
  the rest; full repaint when the cache is dirty). `import_paths` probes non-image files 4-at-a-time in a
  ThreadPoolExecutor (probe_media touches no Qt objects) and consumes results in order; cancel still stops the batch.

- 50.0 — Gapless clip switching (black screen when playback moves to a different file). The 49.8 held-frame overlay only
  masks the gap; this removes it. `Engine` now owns a STANDBY channel (`_sp` QMediaPlayer + `_sa` QAudioOutput rendering
  into the new hidden `VideoView.item2`). While playing, `_tick` -> `_maybe_preload` loads the NEXT clip's target file
  (only when it needs a different file/position, same test as `_advance`) and parks it on its first frame ~2.5 source-s
  before the boundary. `_advance` -> `_swap_standby` then swaps roles (players, audio outputs, `VideoView.swap_items()`,
  `_sink`), applies fx and `play()`s - no stop()/setSource()/load. Any mismatch/not-ready/scrubbing falls back to the old
  `_go()` path (with the overlay), so it can only help.
  - [INVARIANT] `self.player`/`self.audio`/`self._sink` are the ACTIVE channel and CHANGE at runtime - never cache them
    across a playback clip switch. Signals of both players go through `_wire` and check `pl is self.player`.
    `VideoView.item` is the active surface (`item2` the standby); `_relayout` lays out both identically.
  - [INVARIANT] The standby holds a file open: `_drop_standby()` (called from `seek()` and `pause()`) releases it, and every
    file-lock site (rename/remove/clear/Save-Over/closeEvent) already calls `engine.pause()` first - keep that.
  - `_on_state_from` ignores Paused/Stopped reports from the new active player for 0.4 s after a swap while the user still
    wants playback (queued pre-swap pause() reports). The overlay also uses a picture pre-captured 0.5 s before the end of
    a clip when the sink has already been cleared.
  - [KNOWN] Not preloaded: loop wrap-around, clips after a speed-boost change mid-preload (falls back), reverse-proxy
    clips whose proxy finishes after preload started (target mismatch -> fallback). Untested with Qt in the dev sandbox.

- 49.9 — ffmpeg CPU spikes + memory/disk holes. Files: `utils.py`, `probing.py`, `timeline.py`, `playback.py`,
  `preview_stack.py`, `main.py`.
  - Cause of the ffmpeg.exe spikes: `Timeline.request_thumb` started a thread + an ffmpeg per filmstrip tile (up to 48
    at once, hundreds during a zoom animation). Now a 2-thread worker pool with a newest-first queue capped at 96
    (`_thumb_jobs`/`_thumb_loop`), requests are skipped while the zoom animation runs (it re-invalidates on `finished`),
    and each thumbnail ffmpeg is `-threads 1 -an -sn` at below-normal priority (`run_tool(low=True)`, `LOW_PRIO`).
  - ffprobe keyframe scan, ReverseProxy and ScrubProxy encodes now run at below-normal priority; reverse encode uses
    `-threads 2`; all get `-nostdin`.
  - RAM: `reverse` holds every frame of the span in memory (~700 MB for a 30 s 540p clip). ReverseProxy now lowers the
    proxy height so the buffer stays <= ~384 MB (30 s 16:9 -> 400 px; <= ~15 s stays 540; floor 240).
  - RAM: `Timeline.thumb_pix` was unbounded (a new key per 0.1 s per file) -> OrderedDict capped at THUMB_CACHE_MAX=1200
    (evicted tiles reload from the on-disk jpg, no ffmpeg). Content pixmap is reused when the size is unchanged; the
    held-frame overlay releases its pixmap on thaw.
  - Disk: ScrubProxy limited to MAX_FILES=3 / MAX_SEC=1200 (all-intra proxies are big); thumbnail cache dirs are purged of
    files older than 14 days at startup (`purge_old_thumb_cache`, daemon thread).
  - [KNOWN] Undo snapshots (<= 100) keep Media objects (and their keyframe lists) alive after a media is removed from the
    Project; `remove_media` clears the stacks only when clips used it. Export ffmpeg is intentionally full-speed.

- 49.8 — Fix: black flash while scrubbing. Cause: `Engine._go` does `player.stop()` + `setSource()` whenever the file
  changes (clip boundary, or the new scrub-proxy <-> source swap) and Qt pushes an empty frame to the sink. Now
  `Engine._hold_frame()` copies the last valid sink frame into `VideoView`'s overlay (`freeze`/`thaw`/`_place_freeze`,
  a QGraphicsPixmapItem above the video item that mirrors its size/transform/aspect mode) just before the switch;
  `_thaw()` removes it on the first VALID frame after the pending seek was applied (`_on_thaw_frame`), on error, on
  empty timeline, or after a 2.5 s failsafe. Also removes the flash at clip boundaries during normal playback.
  The sink's frame signal is connected only while an overlay is up. If already holding a frame (rapid switches)
  the older frame is kept. Held frame is downscaled to <= 960 px wide (one `toImage()` per switch, not per frame).

- 49.7 — Perf round 2 (scrub input path, model lookups, adaptive resolution). Files: `timeline.py`, `media_model.py`,
  `playback.py`, `main_window.py`.
  - **Mouse-move coalescing** (`Timeline.mouseMoveEvent` -> `_pending_pos` + 0 ms `_move_timer` -> `_flush_move` ->
    `_process_move`): the handler only stores the newest position; scrub seek requests / trim / move / hover run once
    per event-loop turn on the latest position. `mousePressEvent` drops a stale pending position; `mouseReleaseEvent`
    flushes it first so the final position is applied before a drag is committed.
  - **`Sequence` geometry cache**: `starts()`/`total()` are memoized (`_starts_cache`/`_total_cache`) and `locate()` is
    an O(log n) bisect (results verified identical to the old linear scan on 20k randomized timelines, incl. float
    edges). Invalidated by `_invalidate_geometry`, connected to BOTH `edited` and `live`, and called directly by
    `_restore`, `split_at`, `delete`, `delete_many`, `insert_at` (they are queried inside `edit(fn)` before the emit).
    [INVARIANT] Any new code that mutates `segs` or in_s/out_s/speed in place must emit `live`/`edited` (or call
    `seq._invalidate_geometry()`) in the same call, or `starts()` goes stale. Do not mutate the list `starts()` returns.
  - `Timeline._snap_seek_t` uses bisect (was O(n) `min` with a lambda on every scrub move).
  - VU meter: `_on_audio_buffer` RMS now samples ~256 frames per buffer (strided C slicing) instead of every sample.
  - **Adaptive-resolution scrubbing** (`ScrubProxy`, `Engine._scrub_note/_target/_scrub_settle/_restore_fullres`): QMediaPlayer
    can't decode at a lower resolution, so after 3 consecutive slow scrub seeks the Engine requests a low-res
    (<=360 px, all-intra, time-aligned) proxy of the media under the playhead, rendered lazily in the background
    (below-normal priority, 2 threads, only media >= 480 px tall and <= 40 min). While a proxy is ready and the
    session is "degraded" (30 s window, renewed by further slow seeks) SCRUB seeks use it (`_scrub_seek` flag is set
    only inside `_scrub_send`, so play/seek/export never do). Full resolution is reloaded when the mouse rests 220 ms
    and when the scrub session ends. Nothing is rendered if scrubbing never stutters. `ScrubProxy` is registered in
    `ReverseProxy.companions`, so the existing `proxy.cancel_media(path)` / `shutdown()` call sites cover it.
    [KNOWN LIMITS] The first slow scrub of a media still stutters until its proxy finishes (no proxy = old behavior);
    the proxy is not used for normal playback; the file swap on rest/resume costs one reload.

- 49.6 — Perf: Timeline no longer repaints every clip's filmstrip/badges on every mouse-move or playback tick,
  `timeline.py` only (this was the main cause of scrub/seek stutter and general "busy project" hiccups):
  - Root cause: `Timeline.paintEvent` redrew the ruler + every visible clip (filmstrip tiles, badges, labels,
    selection/group borders) from scratch on every single `self.update()`. That call fired on every mouse-move
    while scrubbing the ruler AND on every one of the Engine's ~66/sec `playheadChanged` ticks during ordinary
    playback (`MainWindow.on_playhead` -> `Timeline.set_playhead` -> `update()`), so the cost of a repaint scaled
    with clip count and ran at mouse/frame rate - worse on bigger projects, most visible while scrubbing since
    that's the tightest feedback loop.
  - Fix: split painting into a cached `QPixmap` (`_render_content`/`_content_pix` - ruler + all clips, the
    expensive and rarely-changing part) and a cheap per-frame overlay (`paintEvent` - hover line, drop marker,
    playhead line/marker, the part that actually needs to move every frame). `paintEvent` now just blits the
    cache and draws the overlay unless the cache is dirty.
  - New `Timeline._invalidate_content()` marks the cache dirty (+ schedules a repaint) and is called from every
    place that actually changes clip appearance: `_on_seq` (sequence edits/undo/redo), selection changes
    (press/release/double-click), scroll (`on_scroll`/`wheelEvent`), zoom (`_apply_zoom`), grouping
    (`group_selected`/`ungroup_selected`/`rename_group`/`change_group_color`), a thumbnail arriving
    (`_on_thumb_ready`), and trim/move drags in `mouseMoveEvent` (drawn every move regardless of whether the
    drag has actually reordered anything yet, since the lifted-clip ghost tracks the mouse continuously).
  - Left as a plain (cache-preserving) `self.update()`: ruler-drag scrubbing, ordinary playhead advance during
    playback (`set_playhead` now only sets `_content_dirty` on the rare frame where auto-scroll actually shifts
    the view), hover-only mouse moves, and the file-drop marker - these are now an O(1) pixmap blit instead of
    an O(clip count) repaint.
  - Cache is keyed on `(width, height, devicePixelRatioF())` so a resize or a DPI/monitor change re-renders it;
    the pixmap itself is sized/`setDevicePixelRatio`'d for HiDPI so this isn't a visual regression on retina/
    scaled displays.
  - No behavior change: every pixel `_paint_ruler`/`_paint_clips`/`_draw_clip` draws is identical to before,
    only *when* that drawing runs changed. `MainWindow`'s several direct `self.tl.update()` calls after a
    `Sequence.edit(...)`/`edited.emit()` are all still correct as-is (the invalidate from `_on_seq` already
    happened synchronously earlier in the same call, before any actual repaint occurs).
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
