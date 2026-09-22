StagePilot Uninstaller
=======================

This DMG includes "Uninstall StagePilot.command" alongside StagePilot.app.
Double-click it in Finder (after copying it out of the DMG, or run it
directly from the mounted DMG) to fully remove StagePilot from this Mac,
including data that a simple "drag app to Trash" would leave behind:

  - /Applications/StagePilot.app
  - App data and config: ~/Library/Application Support/org.stagepilot.desktop
  - App logs: ~/Library/Logs/org.stagepilot.desktop
  - The Planning Center access token stored in the macOS Keychain
  - Any StagePilot DMG volumes still mounted under /Volumes from a
    previous install or update

It asks for confirmation before deleting anything and does not require
an administrator password.

Gatekeeper warning ("cannot be opened because it is from an unidentified
developer" or similar):
This script is not notarized. The first time you run it, right-click (or
Control-click) "Uninstall StagePilot.command" in Finder, choose "Open",
then click "Open" again in the dialog that appears. macOS will remember
this choice for future runs. This is the standard one-time approval Apple
requires for unsigned scripts and does not bypass any security check.
