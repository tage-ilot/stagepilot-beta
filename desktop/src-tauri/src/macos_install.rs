//! macOS install-location hygiene: Gatekeeper App Translocation detection and
//! quarantine clearing.
//!
//! # Why this exists
//!
//! StagePilot is ad-hoc signed (no paid Apple Developer identity), so its DMG
//! cannot be notarized. macOS therefore attaches `com.apple.quarantine` to the
//! downloaded DMG and to the `StagePilot.app` a user drags out of it. When a
//! quarantined, non-notarized app bundle is launched, Gatekeeper runs it from a
//! randomized, **read-only** App Translocation image:
//!
//! ```text
//! /private/var/folders/<…>/T/AppTranslocation/<uuid>/d/StagePilot.app
//! ```
//!
//! Finder still shows the app as if it lived in `/Applications`, but every
//! write into the running bundle path fails. That is fatal for the in-app
//! updater: the download succeeds, the managed backend is stopped for the
//! install, the in-place bundle replacement silently fails, and the user is
//! left on the old version with a confusing "UPDATE INTERRUPTED" error.
//!
//! Apple's documented behaviour (WWDC 2016 "What's New in Security", and the
//! `NSURLQuarantinePropertiesKey` / Gatekeeper path-randomization notes) is
//! that translocation stops happening once the quarantine xattr is removed from
//! the bundle — which any user can do for their own `/Applications` copy
//! without elevated privileges, because translocation is triggered purely by
//! the quarantine flag plus the "not moved by Finder" heuristic.
//!
//! So this module does two things:
//!
//! 1. Detect translocation (path-based, see [`is_translocated_path`]) and let
//!    the updater refuse to even start an install that is guaranteed to fail,
//!    showing a specific, actionable message instead.
//! 2. On startup, when NOT translocated, proactively strip the quarantine flag
//!    from our own bundle so the *next* launch can never be translocated.

use std::path::{Path, PathBuf};

use serde::Serialize;

/// Path marker macOS uses for Gatekeeper's randomized read-only app images.
const TRANSLOCATION_MARKER: &str = "/AppTranslocation/";

/// The user-facing instruction shown when the app is translocated. Kept in Rust
/// so the same wording is available to the updater UI and to any log output.
pub const TRANSLOCATION_GUIDANCE: &str = concat!(
    "StagePilot is running from a temporary, quarantined copy created by macOS ",
    "(App Translocation), so it cannot install updates into itself. ",
    "Quit StagePilot, open Finder \u{2192} Applications, right-click ",
    "StagePilot.app and choose Open once (or run `xattr -cr ",
    "/Applications/StagePilot.app` in Terminal), then relaunch StagePilot to ",
    "enable in-app updates."
);

/// Snapshot of where StagePilot is actually running from, and whether in-app
/// updates can work from there.
#[derive(Clone, Debug, Serialize, PartialEq, Eq)]
#[serde(rename_all = "camelCase")]
pub struct InstallEnvironment {
    /// Canonicalized path of the running executable, when it could be resolved.
    pub executable_path: Option<String>,
    /// True when macOS is running us from a read-only App Translocation image.
    pub translocated: bool,
    /// True when in-app updates must not be attempted from this location.
    pub updates_blocked: bool,
    /// Actionable guidance to show the user, present only when blocked.
    pub guidance: Option<String>,
}

impl InstallEnvironment {
    fn allowed(executable_path: Option<String>) -> Self {
        Self {
            executable_path,
            translocated: false,
            updates_blocked: false,
            guidance: None,
        }
    }

    fn translocated(executable_path: Option<String>) -> Self {
        Self {
            executable_path,
            translocated: true,
            updates_blocked: true,
            guidance: Some(TRANSLOCATION_GUIDANCE.to_string()),
        }
    }
}

/// Returns true when `path` is inside a Gatekeeper App Translocation image.
///
/// Translocation is deliberately transparent to naive checks: the app's own
/// `Contents/MacOS` layout and bundle identifier are unchanged, and Finder
/// reports the original `/Applications` location. The one reliable, documented
/// signal available without private APIs is the randomized mount path itself,
/// which always contains an `AppTranslocation/<uuid>/d/` component under the
/// per-user temporary directory. Callers must pass an already-canonicalized
/// path (`/private/var/…`, not the `/var` symlink) — see
/// [`resolved_executable_path`].
pub fn is_translocated_path(path: &Path) -> bool {
    path.to_string_lossy().contains(TRANSLOCATION_MARKER)
}

/// Resolves the running executable through symlinks, falling back to the
/// unresolved path when canonicalization fails (e.g. a read-only image that
/// refuses `realpath`, which itself does not hide the marker component).
pub fn resolved_executable_path() -> Option<PathBuf> {
    let executable = std::env::current_exe().ok()?;
    Some(std::fs::canonicalize(&executable).unwrap_or(executable))
}

/// Walks up from `…/StagePilot.app/Contents/MacOS/<exe>` to the `.app` bundle.
#[cfg_attr(not(target_os = "macos"), allow(dead_code))]
pub fn bundle_path_for(executable: &Path) -> Option<PathBuf> {
    let mut candidate = executable.parent()?.to_path_buf(); // …/Contents/MacOS
    for _ in 0..2 {
        candidate = candidate.parent()?.to_path_buf();
    }
    if candidate
        .extension()
        .and_then(|extension| extension.to_str())
        == Some("app")
    {
        Some(candidate)
    } else {
        None
    }
}

/// Describes the current install location. Translocation is a macOS-only
/// mechanism, but the path check is harmless (and always false) elsewhere, so it
/// runs unconditionally rather than behind `cfg`.
pub fn describe() -> InstallEnvironment {
    let executable = resolved_executable_path();
    let display = executable
        .as_ref()
        .map(|path| path.to_string_lossy().to_string());
    if executable.as_deref().is_some_and(is_translocated_path) {
        return InstallEnvironment::translocated(display);
    }
    InstallEnvironment::allowed(display)
}

/// Best-effort startup hygiene: when we are NOT translocated but our own bundle
/// still carries `com.apple.quarantine`, strip it so macOS cannot translocate
/// the next launch. Requires no elevated privileges for a bundle the current
/// user can write (the normal case for `/Applications` on a personal Mac); any
/// failure (read-only volume, other owner) is logged and ignored.
#[cfg(target_os = "macos")]
pub fn clear_own_quarantine_if_possible() {
    use std::process::Command;

    let Some(executable) = resolved_executable_path() else {
        return;
    };
    if is_translocated_path(&executable) {
        // The translocated image is read-only and disposable; clearing xattrs
        // there would accomplish nothing. The real bundle is the user's to fix.
        eprintln!("StagePilot is running translocated. {TRANSLOCATION_GUIDANCE}");
        return;
    }
    let Some(bundle) = bundle_path_for(&executable) else {
        return;
    };
    let quarantined = Command::new("/usr/bin/xattr")
        .args(["-p", "com.apple.quarantine"])
        .arg(&bundle)
        .output()
        .map(|output| output.status.success())
        .unwrap_or(false);
    if !quarantined {
        return;
    }
    match Command::new("/usr/bin/xattr").arg("-cr").arg(&bundle).status() {
        Ok(status) if status.success() => {
            eprintln!(
                "StagePilot cleared the macOS quarantine flag on {} so future launches are not translocated.",
                bundle.display()
            );
        }
        Ok(status) => eprintln!(
            "StagePilot could not clear the macOS quarantine flag on {} (xattr exited with {status})."
            , bundle.display()
        ),
        Err(error) => eprintln!(
            "StagePilot could not run xattr to clear the macOS quarantine flag on {}: {error}",
            bundle.display()
        ),
    }
}

#[cfg(not(target_os = "macos"))]
pub fn clear_own_quarantine_if_possible() {}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn detects_a_real_translocated_launch_path() {
        let path = Path::new(
            "/private/var/folders/gh/x_484m0j0jv4sg0204x0gkq80000gn/T/AppTranslocation/\
             F600EA8D-857F-4C72-BB2D-F7FC242EE642/d/StagePilot.app/Contents/MacOS/StagePilot",
        );
        assert!(is_translocated_path(path));
    }

    #[test]
    fn does_not_flag_a_genuine_applications_install() {
        for path in [
            "/Applications/StagePilot.app/Contents/MacOS/StagePilot",
            "/Users/someone/Applications/StagePilot.app/Contents/MacOS/StagePilot",
            "/Volumes/StagePilot/StagePilot.app/Contents/MacOS/StagePilot",
            "/Users/someone/Projects/AppTranslocationNotes/StagePilot.app/Contents/MacOS/StagePilot",
        ] {
            assert!(
                !is_translocated_path(Path::new(path)),
                "{path} must not be treated as translocated"
            );
        }
    }

    #[test]
    fn resolves_the_bundle_from_the_executable_path() {
        assert_eq!(
            bundle_path_for(Path::new(
                "/Applications/StagePilot.app/Contents/MacOS/StagePilot"
            )),
            Some(PathBuf::from("/Applications/StagePilot.app"))
        );
        assert_eq!(
            bundle_path_for(Path::new("/usr/local/bin/stagepilot")),
            None
        );
    }

    #[test]
    fn blocked_environments_always_carry_actionable_guidance() {
        let blocked = InstallEnvironment::translocated(Some("/tmp/x".to_string()));
        assert!(blocked.updates_blocked);
        let guidance = blocked.guidance.expect("guidance");
        assert!(guidance.contains("xattr -cr /Applications/StagePilot.app"));
        assert!(guidance.contains("Quit StagePilot"));

        let allowed = InstallEnvironment::allowed(None);
        assert!(!allowed.updates_blocked);
        assert!(allowed.guidance.is_none());
    }
}
