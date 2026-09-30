# Screenshots

| File | Description |
|---|---|
| `01-startup-overlay.png` | **PLACEHOLDER -- needs a new screenshot.** The blocking startup overlay shown when the server is checking dependencies and fetching models (see CLAUDE.md's "Startup: Dependency Checks & Model Prefetch"). Best captured with the model caches cleared (`rm -rf ~/.cache/audio-separator-models/* ~/.cache/huggingface/hub/models--adefossez--HTDemucs*`) and mid-download, so the shot shows a realistic mix of states at once: some checks/models already ✅ done, at least one ⬇️ actively downloading with its live percentage bar visible, and ideally one still showing "in queue". Replaces the old `01-intro-screen.png` (removed -- that plain upload screen is what appears *after* this overlay closes, no longer the first thing a user sees). |
| `02-initial-processing-song1.png` | Song 1 during initial upload/processing (alignment-mode choice) |
| `03-initial-processing-song2.png` | Song 2 during initial upload/processing (including "Snap beat grid to Song 1") |
| `04-realtime-workspace-volumes.png` | The real-time mixing workspace with per-stem volume sliders and crossfader |
| `05-realtime-audio-display.png` | The multi-stem waveform display during live playback |
| `06-processing-and-download.png` | The processing progress bar and download buttons |
