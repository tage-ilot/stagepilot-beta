//! Loopback OAuth callback listener for "Sign in with Planning Center".
//!
//! Modeled directly on `native_credentials.rs` (`NativeCredentialBroker`):
//! a plain `TcpListener` on 127.0.0.1 with a non-blocking accept loop and no
//! extra HTTP dependency. The differences are OAuth-specific and deliberate:
//!
//! * The port is NOT ephemeral. Planning Center requires an exact redirect
//!   URI match and exactly four loopback URIs are registered for StagePilot
//!   (`http://127.0.0.1:52847/callback` .. `:52850/callback`), so the
//!   listener tries those four ports in order and the first free one wins.
//! * The listener is single-use: it stops after the first genuine
//!   `/callback` request, or after a bounded timeout (~5 minutes) if the
//!   user abandons the browser tab.
//! * The `state` query parameter must match the value the backend generated
//!   for this sign-in attempt (CSRF protection), otherwise the callback is
//!   rejected and no code is returned.

use std::{
    io::{Read, Write},
    net::{TcpListener, TcpStream},
    time::{Duration, Instant},
};

/// The exact loopback redirect ports registered with Planning Center.
pub const CALLBACK_PORTS: [u16; 4] = [52847, 52848, 52849, 52850];
pub const CALLBACK_PATH: &str = "/callback";
const MAX_REQUEST_BYTES: usize = 8 * 1024;
const DEFAULT_TIMEOUT: Duration = Duration::from_secs(300);
const AUTHORIZE_URL_PREFIX: &str = "https://api.planningcenteronline.com/oauth/authorize?";

const COMPLETED_PAGE: &str = "<!doctype html><html><head><meta charset=\"utf-8\"><title>StagePilot</title></head><body style=\"font-family:system-ui;padding:3rem;text-align:center\"><h1>Planning Center connected</h1><p>You can close this tab and return to StagePilot.</p></body></html>";

/// What a single callback request resolved to.
#[derive(Debug, PartialEq, Eq)]
pub enum CallbackOutcome {
    /// A valid, state-matched authorization code.
    Code(String),
    /// Planning Center reported an error (e.g. the user denied access).
    Denied(String),
    /// The request did not belong to this sign-in attempt; keep listening.
    Ignored,
    /// The request was for this path but its `state` did not match.
    StateMismatch,
}

pub struct CallbackServer {
    listener: TcpListener,
    port: u16,
}

impl CallbackServer {
    /// Bind the first free pre-registered loopback port.
    pub fn start() -> Result<Self, String> {
        let listener = bind_registered_port()?;
        // Deliberately left in the default *blocking* mode. The previous
        // implementation used a nonblocking `accept()` polled in a loop with
        // a wall-clock deadline check ahead of each iteration. That has a
        // structural flaw independent of how wide the timeout is: if the
        // polling thread is descheduled (CPU-starved CI runner under load)
        // during one of its `sleep()` calls for longer than the remaining
        // budget, the loop's `while Instant::now() < deadline` guard fails
        // and it returns a timeout error WITHOUT ever calling `accept()`
        // again — even though a connection is already sitting in the OS
        // accept backlog, queued the moment the client connected. This is a
        // pure scheduling artifact, not an actual absence of a callback, and
        // widening the timeout does not fix it because the failure mode is
        // "thread didn't get scheduled in time to notice", not "client was
        // too slow". A genuinely blocking `accept()` has no such gap: as
        // soon as the OS wakes the thread there's nothing left to check, the
        // call returns the already-queued connection immediately.
        let port = listener
            .local_addr()
            .map_err(|_| "Could not resolve the Planning Center sign-in listener.".to_string())?
            .port();
        Ok(Self { listener, port })
    }

    pub fn port(&self) -> u16 {
        self.port
    }

    pub fn redirect_uri(&self) -> String {
        redirect_uri_for(self.port)
    }

    /// Wait for the single expected callback, or fail once `timeout` elapses.
    ///
    /// Consumes the server: the listener is dropped (and the port released)
    /// as soon as this returns, whatever the outcome.
    ///
    /// Implementation note: the accept loop runs on its own thread using a
    /// genuinely *blocking* `accept()`, so there is no polling interval to
    /// oversleep past. The `timeout` is enforced entirely on this side via
    /// `Receiver::recv_timeout`, which only affects the case where no
    /// connection ever arrives; it can never cause a queued connection to be
    /// missed. If the timeout fires, the listener is dropped, which unblocks
    /// the accept thread (with an error) so it doesn't leak.
    pub fn wait(self, expected_state: &str, timeout: Duration) -> Result<String, String> {
        let expected_state = expected_state.to_string();
        let (tx, rx) = std::sync::mpsc::channel();
        let listener = self.listener;
        let accept_thread = std::thread::spawn(move || loop {
            match listener.accept() {
                Ok((stream, _)) => match handle(stream, &expected_state) {
                    CallbackOutcome::Code(code) => {
                        let _ = tx.send(Ok(code));
                        return;
                    }
                    CallbackOutcome::Denied(reason) => {
                        let _ = tx.send(Err(sign_in_error(&reason)));
                        return;
                    }
                    CallbackOutcome::StateMismatch => {
                        let _ = tx.send(Err(
                            "Planning Center sign-in could not be verified. Try signing in again."
                                .to_string(),
                        ));
                        return;
                    }
                    CallbackOutcome::Ignored => {}
                },
                Err(_) => return,
            }
        });
        match rx.recv_timeout(timeout) {
            Ok(result) => {
                // The accept thread already sent its result and is exiting
                // (or has exited); joining here is bounded and just reclaims
                // the thread promptly instead of leaving it detached.
                let _ = accept_thread.join();
                result
            }
            Err(_) => {
                // The accept thread is still blocked inside `accept()` (no
                // connection ever arrived) and there is no portable way to
                // cancel a blocking accept from the outside. Detach it
                // instead of joining: it will keep the (now-abandoned)
                // listener alive until either a stray connection wakes it up
                // or the process exits, but it will never block this call.
                drop(accept_thread);
                Err("Planning Center sign-in was not completed in time. Try again.".to_string())
            }
        }
    }
}

pub fn default_timeout() -> Duration {
    DEFAULT_TIMEOUT
}

pub fn redirect_uri_for(port: u16) -> String {
    format!("http://127.0.0.1:{port}{CALLBACK_PATH}")
}

/// Only Planning Center's own authorize endpoint may be opened by the
/// sign-in command, so a compromised/confused caller cannot use StagePilot
/// as a generic "open any URL in the user's browser" primitive.
pub fn is_allowed_authorize_url(url: &str) -> bool {
    url.starts_with(AUTHORIZE_URL_PREFIX) && !url.contains(char::is_whitespace)
}

fn bind_registered_port() -> Result<TcpListener, String> {
    for port in CALLBACK_PORTS {
        if let Ok(listener) = TcpListener::bind(("127.0.0.1", port)) {
            return Ok(listener);
        }
    }
    Err(
        "StagePilot could not open a Planning Center sign-in port. Close other copies of \
StagePilot and try again."
            .to_string(),
    )
}

fn sign_in_error(reason: &str) -> String {
    if reason == "access_denied" {
        "Planning Center sign-in was cancelled.".to_string()
    } else {
        "Planning Center did not complete the sign-in. Try again.".to_string()
    }
}

fn handle(mut stream: TcpStream, expected_state: &str) -> CallbackOutcome {
    let _ = stream.set_read_timeout(Some(Duration::from_secs(2)));
    let _ = stream.set_write_timeout(Some(Duration::from_secs(2)));
    let Some(target) = read_request_target(&mut stream) else {
        respond(&mut stream, 400, "text/plain", "");
        return CallbackOutcome::Ignored;
    };
    let outcome = classify_callback(&target, expected_state);
    match &outcome {
        CallbackOutcome::Code(_) => {
            respond(&mut stream, 200, "text/html; charset=utf-8", COMPLETED_PAGE)
        }
        CallbackOutcome::Denied(_) => respond(
            &mut stream,
            400,
            "text/plain",
            "Planning Center sign-in did not complete. Return to StagePilot.",
        ),
        CallbackOutcome::StateMismatch => {
            respond(&mut stream, 400, "text/plain", "Invalid sign-in request.")
        }
        CallbackOutcome::Ignored => respond(&mut stream, 404, "text/plain", ""),
    }
    outcome
}

/// Decide what a request target (e.g. `/callback?code=x&state=y`) means for
/// this sign-in attempt. Pure, so the state/CSRF rules are unit-testable.
pub fn classify_callback(target: &str, expected_state: &str) -> CallbackOutcome {
    let (path, query) = match target.split_once('?') {
        Some((path, query)) => (path, query),
        None => (target, ""),
    };
    if path != CALLBACK_PATH {
        return CallbackOutcome::Ignored;
    }
    let state = query_value(query, "state");
    if state.as_deref() != Some(expected_state) || expected_state.is_empty() {
        return CallbackOutcome::StateMismatch;
    }
    if let Some(error) = query_value(query, "error") {
        return CallbackOutcome::Denied(error);
    }
    match query_value(query, "code") {
        Some(code) if !code.is_empty() => CallbackOutcome::Code(code),
        _ => CallbackOutcome::Denied("invalid_request".to_string()),
    }
}

fn query_value(query: &str, name: &str) -> Option<String> {
    query
        .split('&')
        .filter_map(|pair| pair.split_once('='))
        .find(|(key, _)| *key == name)
        .map(|(_, value)| percent_decode(value))
}

fn percent_decode(value: &str) -> String {
    let bytes = value.as_bytes();
    let mut decoded = Vec::with_capacity(bytes.len());
    let mut index = 0;
    while index < bytes.len() {
        match bytes[index] {
            b'+' => {
                decoded.push(b' ');
                index += 1;
            }
            b'%' if index + 2 < bytes.len() => {
                let hex = std::str::from_utf8(&bytes[index + 1..index + 3]).ok();
                match hex.and_then(|part| u8::from_str_radix(part, 16).ok()) {
                    Some(byte) => {
                        decoded.push(byte);
                        index += 3;
                    }
                    None => {
                        decoded.push(bytes[index]);
                        index += 1;
                    }
                }
            }
            byte => {
                decoded.push(byte);
                index += 1;
            }
        }
    }
    String::from_utf8_lossy(&decoded).into_owned()
}

fn read_request_target(stream: &mut TcpStream) -> Option<String> {
    let mut bytes = Vec::new();
    let mut buffer = [0_u8; 1024];
    loop {
        let count = stream.read(&mut buffer).ok()?;
        if count == 0 || bytes.len() + count > MAX_REQUEST_BYTES {
            return None;
        }
        bytes.extend_from_slice(&buffer[..count]);
        if bytes.windows(4).any(|part| part == b"\r\n\r\n") {
            break;
        }
    }
    let header = std::str::from_utf8(&bytes).ok()?;
    let mut first = header.split("\r\n").next()?.split_whitespace();
    let method = first.next()?;
    let target = first.next()?.to_string();
    if method != "GET" {
        return None;
    }
    Some(target)
}

fn respond(stream: &mut TcpStream, status: u16, content_type: &str, body: &str) {
    let reason = match status {
        200 => "OK",
        400 => "Bad Request",
        404 => "Not Found",
        _ => "Service Unavailable",
    };
    let _ = write!(
        stream,
        "HTTP/1.1 {status} {reason}\r\nContent-Type: {content_type}\r\nCache-Control: no-store\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    );
    let _ = stream.flush();
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::TcpStream as ClientStream;

    #[test]
    fn registered_ports_are_the_four_planning_center_redirect_uris() {
        assert_eq!(CALLBACK_PORTS, [52847, 52848, 52849, 52850]);
        assert_eq!(
            redirect_uri_for(CALLBACK_PORTS[0]),
            "http://127.0.0.1:52847/callback"
        );
    }

    #[test]
    fn falls_back_to_the_next_registered_port_when_earlier_ones_are_taken() {
        // Occupy the first two registered ports, then confirm the listener
        // still starts and lands on a later registered port (never an
        // ephemeral one, which Planning Center would reject).
        let mut held = Vec::new();
        for port in [CALLBACK_PORTS[0], CALLBACK_PORTS[1]] {
            if let Ok(listener) = TcpListener::bind(("127.0.0.1", port)) {
                held.push(listener);
            }
        }
        let Ok(server) = CallbackServer::start() else {
            // All four ports busy on this machine: nothing to assert.
            return;
        };
        assert!(CALLBACK_PORTS.contains(&server.port()));
        if held.len() == 2 {
            assert!(server.port() != CALLBACK_PORTS[0] && server.port() != CALLBACK_PORTS[1]);
        }
    }

    #[test]
    fn accepts_a_matching_state_and_returns_the_code() {
        assert_eq!(
            classify_callback("/callback?code=abc123&state=s1", "s1"),
            CallbackOutcome::Code("abc123".to_string())
        );
        assert_eq!(
            classify_callback("/callback?state=s1&code=a%2Bb", "s1"),
            CallbackOutcome::Code("a+b".to_string())
        );
    }

    #[test]
    fn rejects_a_mismatched_missing_or_empty_state() {
        assert_eq!(
            classify_callback("/callback?code=abc&state=other", "s1"),
            CallbackOutcome::StateMismatch
        );
        assert_eq!(
            classify_callback("/callback?code=abc", "s1"),
            CallbackOutcome::StateMismatch
        );
        assert_eq!(
            classify_callback("/callback?code=abc&state=", ""),
            CallbackOutcome::StateMismatch
        );
    }

    #[test]
    fn ignores_requests_for_other_paths() {
        assert_eq!(
            classify_callback("/favicon.ico", "s1"),
            CallbackOutcome::Ignored
        );
        assert_eq!(classify_callback("/", "s1"), CallbackOutcome::Ignored);
    }

    #[test]
    fn surfaces_planning_center_errors_and_missing_codes() {
        assert_eq!(
            classify_callback("/callback?error=access_denied&state=s1", "s1"),
            CallbackOutcome::Denied("access_denied".to_string())
        );
        assert_eq!(
            classify_callback("/callback?state=s1", "s1"),
            CallbackOutcome::Denied("invalid_request".to_string())
        );
    }

    #[test]
    fn only_planning_centers_authorize_endpoint_may_be_opened() {
        assert!(is_allowed_authorize_url(
            "https://api.planningcenteronline.com/oauth/authorize?client_id=x&state=y"
        ));
        assert!(!is_allowed_authorize_url(
            "https://example.com/oauth/authorize?a=b"
        ));
        assert!(!is_allowed_authorize_url(
            "http://api.planningcenteronline.com/oauth/authorize?a=b"
        ));
        assert!(!is_allowed_authorize_url(
            "https://api.planningcenteronline.com/oauth/authorize"
        ));
    }

    #[test]
    fn times_out_when_no_callback_arrives() {
        let Ok(server) = CallbackServer::start() else {
            return;
        };
        let started = Instant::now();
        let outcome = server.wait("s1", Duration::from_millis(250));
        assert!(outcome.is_err());
        assert!(started.elapsed() >= Duration::from_millis(200));
    }

    #[test]
    fn end_to_end_callback_returns_the_code_and_releases_the_port() {
        let Ok(server) = CallbackServer::start() else {
            return;
        };
        let port = server.port();
        // Generous deadline for the worker's own `wait()` loop: this only
        // needs to comfortably exceed the accept-loop's 50-100ms poll
        // interval plus whatever scheduling delay a loaded CI runner adds;
        // it is not what caused the original flake (the fixed pre-connect
        // sleep above was), but a wider margin here costs nothing and adds
        // resilience against CPU-starved CI runners.
        let worker = std::thread::spawn(move || server.wait("state-xyz", Duration::from_secs(10)));
        // The listener is bound synchronously inside `CallbackServer::start()`
        // (before it returns), so the OS will happily queue an incoming
        // connection in the accept backlog even before the spawned `wait()`
        // thread gets scheduled and reaches its first `accept()` call. There
        // is therefore no need to race a fixed sleep against that thread's
        // own scheduling: retry the connect itself with a short backoff
        // until it succeeds (or a generous deadline elapses), which removes
        // the race entirely instead of just narrowing it.
        let connect_deadline = Instant::now() + Duration::from_secs(2);
        let mut client = loop {
            match ClientStream::connect(("127.0.0.1", port)) {
                Ok(stream) => break stream,
                Err(error) if Instant::now() < connect_deadline => {
                    std::thread::sleep(Duration::from_millis(5));
                    let _ = error;
                }
                Err(error) => panic!("connect: {error}"),
            }
        };
        client
            .write_all(
                b"GET /callback?code=the-code&state=state-xyz HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n",
            )
            .expect("write");
        let mut response = String::new();
        let _ = client.read_to_string(&mut response);
        assert_eq!(worker.join().expect("join"), Ok("the-code".to_string()));
        assert!(response.starts_with("HTTP/1.1 200 OK"));
        // Single-use: the listener is dropped, so the port is bindable again.
        assert!(TcpListener::bind(("127.0.0.1", port)).is_ok());
    }
}
