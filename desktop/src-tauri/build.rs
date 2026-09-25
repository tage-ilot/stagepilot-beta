fn main() {
    // Bake the Planning Center OAuth client_id into the built binary.
    //
    // This is deliberately NOT a secret (only PLANNING_CENTER_CLIENT_SECRET
    // is, and that never leaves the control-plane Worker) — it's a
    // product-wide OAuth registration value that the desktop app needs at
    // runtime to build its authorize URL. Release workflows set
    // STAGEPILOT_PCO_CLIENT_ID from the same repo secret
    // (PLANNING_CENTER_CLIENT_ID) used by deploy-control-plane.yml. Local/
    // dev builds simply get an empty string, which keeps sign-in reporting
    // itself as unavailable rather than building a broken authorize URL —
    // same behavior as today's unset-env-var case.
    println!("cargo:rerun-if-env-changed=STAGEPILOT_PCO_CLIENT_ID");
    println!("cargo:rerun-if-env-changed=STAGEPILOT_RELEASE_BUILD");
    let client_id = std::env::var("STAGEPILOT_PCO_CLIENT_ID").unwrap_or_default();
    let release_build = std::env::var("STAGEPILOT_RELEASE_BUILD").as_deref() == Ok("1");
    if release_build && client_id.trim().is_empty() {
        panic!(
            "release builds MUST have a real OAuth client_id; refusing to produce a build that silently ships broken sign-in"
        );
    }
    println!("cargo:rustc-env=STAGEPILOT_PCO_CLIENT_ID={client_id}");

    tauri_build::build()
}
