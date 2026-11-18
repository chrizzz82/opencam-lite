# Changelog

All notable changes to OpenCam Lite will be documented in this file.

## [v0.2.0] - 2026-11-04

### Added
- **Daily Recording Folders:** Recordings are now strictly structured under `YYYY-MM-DD` subfolders with automated pre-creation to ensure zero midnight failures.
- **Watchdog & Auto-Reconnect:** Added smart stream activity monitoring that detects camera reboots and automatically recovers the stream after 25 seconds.
- **Advanced Archive & Time Range Filter:** Added a dedicated Archive tab in the UI with a time-range filter (`From / To`) and single-day ZIP exports.
- **Signed Tapo PTZ Support:** Implemented ONVIF WS-Security Digest Authentication for reliable PTZ control on Tapo cameras.
- **Full i18n Support:** Added complete translations for German, English, French, and Spanish across all modals and settings.
- **Hybrid ONVIF Scanner:** Combined multicast WS-Discovery with a fast parallel TCP subnet scan to reliably discover cameras behind Docker bridge networks.

### Fixed
- Fixed HLS stream freezes when enabling privacy or motion zone masks by enforcing stable `libx264` transcoding with `yuv420p` pixel formatting.
- Relocated PTZ controls from an intrusive video overlay to the bottom card footer.



# Changelog

## [v0.2.1] - 2026-11-05
### Added
* **Automatic Update Checker:** Background check comparing the local version against the GitHub repository, displaying a direct notification in the header when updates are available.
* **Integrated Update Modal:** Clicking the update badge opens a popup containing the exact ZimaOS terminal commands.
* **Port Migration to 7777:** Standardized port configuration for clean homelab integration.

### Changed & Optimized
* **Persistent Configuration:** Hardcoded Docker volume mount points for `/config` and `/recordings` ensure all settings and recordings survive container updates.
* **UI Refinement:** Cleaned up header badge styling and version display alongside the project title.

* 
## v0.3.0

### Added
- **Live archive:** Recordings can now be searched, played back and exported while they are still being written. Play, MP4 and GIF work for the current block, no need to wait until it is finished.
  - The recorder now writes fragmented MP4 through ffmpeg (same MPEG-4 codec as before), so the running file is readable at any time. If ffmpeg fails, the cause is logged and recording falls back to the previous OpenCV writer.
  - Running recordings are played back as on-demand H.264 clips (5 min per segment, continues automatically) and are cached for 60 s.
- **Per-camera folders:** Recordings are stored in `recordings/<date>/<CameraName>_<id>/`. The folder stays the same if a camera is renamed. Existing recordings in the flat day folder are moved into camera folders automatically on startup. ZIP export keeps the folder structure.
- **Live mode with motion zones:** A camera set to "Live" that has motion zones configured now records on motion in those zones (like "Motion" mode). Without zones, "Live" stays live-only.
- **Diagnostics:** When no recording is found, the timeline now shows which files the server returned. `/api/status` reports `live_archive: true`, and the startup log prints `Live-Archiv aktiv`.

### Changed
- Event "Play" from the gallery or event list always jumps into the recording, even for events within the last 20 seconds (previously it fell back to the live view).
- Odd video resolutions are padded to even dimensions so the ffmpeg recorder never fails on them.

### Fixed
- **"No recording" for every event:** Recordings were not listed in the archive when the camera name contained characters like spaces or parentheses (e.g. `TP-Link Tapo Camera (192.168.1.5)`), because the file name was parsed from the wrong position. Date, time and mode are now read from the end of the file name.
- GIF/MP4 export failed with "Keine Aufnahme" for events in the current block.

### Upgrade notes
- ffmpeg must be available in the container (already required for the post-recording conversion).
- Restarting ends the current block. The unfinished file from before the update is not readable and is discarded on startup; a new block starts in the camera folder.
- Docker: rebuild the image or copy `server.py` and `index.html` into the container, otherwise the old code keeps running.
