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
