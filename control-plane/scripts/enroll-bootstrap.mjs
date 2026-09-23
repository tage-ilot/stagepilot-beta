#!/usr/bin/env node

import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

export const BOOTSTRAP_SCHEMA = "org.stagepilot.private-beta-bootstrap";
export const BOOTSTRAP_VERSION = 1;
const ID = /^[a-f0-9]{8}$|^[a-f0-9]{16}$|^[a-f0-9]{32}$/;
const HOSTNAME = /^[a-z0-9](?:[a-z0-9.-]{1,251}[a-z0-9])$/;
const IDEMPOTENCY_KEY = /^[A-Za-z0-9._:-]{8,128}$/;
const CREDENTIAL = /^spi_([a-f0-9]{8}|[a-f0-9]{16}|[a-f0-9]{32})\.([A-Za-z0-9_-]{32,})$/;
const EXACT_KEYS = [
  "schema",
  "version",
  "bundleId",
  "controlPlaneOrigin",
  "installationId",
  "hostname",
  "remotePort",
  "installationCredential",
  "issuedAt",
].sort();

function exactHttpsOrigin(value) {
  if (typeof value !== "string") throw new Error("Control-plane origin is required");
  const parsed = new URL(value);
  if (parsed.protocol !== "https:" || parsed.origin !== value || parsed.username || parsed.password) {
    throw new Error("Control-plane origin must be an exact HTTPS origin without a path or credentials");
  }
  return parsed.origin;
}

function validPort(value) {
  return Number.isInteger(value) && value >= 1024 && value <= 65535 && value !== 8765;
}

export function validateBootstrapBundle(bundle) {
  if (!bundle || typeof bundle !== "object" || Array.isArray(bundle)) throw new Error("Invalid bootstrap bundle");
  if (JSON.stringify(Object.keys(bundle).sort()) !== JSON.stringify(EXACT_KEYS)) {
    throw new Error("Bootstrap bundle has unknown or missing fields");
  }
  if (bundle.schema !== BOOTSTRAP_SCHEMA || bundle.version !== BOOTSTRAP_VERSION) {
    throw new Error("Unsupported bootstrap bundle schema");
  }
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(bundle.bundleId)) {
    throw new Error("Invalid bootstrap bundle ID");
  }
  exactHttpsOrigin(bundle.controlPlaneOrigin);
  if (!ID.test(bundle.installationId)) throw new Error("Invalid installation ID");
  if (!HOSTNAME.test(bundle.hostname) || bundle.hostname.includes("..")) throw new Error("Invalid installation hostname");
  if (bundle.hostname !== `sp-${bundle.installationId}.${bundle.hostname.split(".").slice(1).join(".")}`) {
    throw new Error("Bootstrap hostname is not bound to its installation ID");
  }
  const credential = CREDENTIAL.exec(bundle.installationCredential);
  if (!credential || credential[1] !== bundle.installationId) {
    throw new Error("Bootstrap credential is not bound to its installation ID");
  }
  if (!validPort(bundle.remotePort)) throw new Error("Invalid dedicated Remote port");
  if (typeof bundle.issuedAt !== "string" || Number.isNaN(Date.parse(bundle.issuedAt))) {
    throw new Error("Invalid bootstrap issue time");
  }
  return bundle;
}

function validateEnrollment(response) {
  if (!response || typeof response !== "object" || Array.isArray(response)) throw new Error("Invalid enrollment response");
  if (!ID.test(response.installationId) || typeof response.hostname !== "string" || typeof response.installationCredential !== "string") {
    throw new Error("Enrollment response is missing required fields");
  }
  return response;
}

function sameEnrollment(bundle, enrollment, origin, remotePort) {
  return bundle.controlPlaneOrigin === origin
    && bundle.remotePort === remotePort
    && bundle.installationId === enrollment.installationId
    && bundle.hostname === enrollment.hostname
    && bundle.installationCredential === enrollment.installationCredential;
}

function readPrivateToken(file) {
  const stat = fs.lstatSync(file);
  if (!stat.isFile() || stat.isSymbolicLink()) throw new Error("Administrator token path must be a regular file");
  if ((stat.mode & 0o077) !== 0) throw new Error("Administrator token file must not be accessible by group or others");
  const value = fs.readFileSync(file, "utf8").trim();
  if (value.length < 32 || /\s/.test(value)) throw new Error("Administrator token file is invalid");
  return value;
}

function writePrivateJson(file, value) {
  const descriptor = fs.openSync(file, "wx", 0o600);
  try {
    fs.writeFileSync(descriptor, `${JSON.stringify(value, null, 2)}\n`, "utf8");
    fs.fsyncSync(descriptor);
  } finally {
    fs.closeSync(descriptor);
  }
  if ((fs.statSync(file).mode & 0o777) !== 0o600) throw new Error("Bootstrap bundle permissions are not 0600");
}

export async function enrollAndWriteBundle({
  origin,
  idempotencyKey,
  label = "",
  remotePort,
  output,
  adminToken,
  fetchImpl = fetch,
  now = () => new Date().toISOString(),
  randomUUID = () => crypto.randomUUID(),
  log = console.log,
}) {
  const controlPlaneOrigin = exactHttpsOrigin(origin);
  if (!IDEMPOTENCY_KEY.test(idempotencyKey)) throw new Error("Idempotency key must contain 8-128 safe characters");
  if (typeof label !== "string" || label.length > 100) throw new Error("Label must contain at most 100 characters");
  if (!validPort(remotePort)) throw new Error("Invalid dedicated Remote port");
  if (typeof output !== "string" || output.length === 0 || !path.isAbsolute(output)) {
    throw new Error("Bootstrap output must be an explicitly selected absolute path");
  }
  if (typeof adminToken !== "string" || adminToken.length < 32 || /\s/.test(adminToken)) {
    throw new Error("Administrator token is invalid");
  }

  const response = await fetchImpl(`${controlPlaneOrigin}/v1/admin/installations`, {
    method: "POST",
    headers: {
      authorization: `Bearer ${adminToken}`,
      "content-type": "application/json",
    },
    body: JSON.stringify({ idempotencyKey, label }),
  });
  if (response.status !== 201) throw new Error(`Enrollment failed with HTTP ${response.status}`);
  const enrollment = validateEnrollment(await response.json());

  if (fs.existsSync(output)) {
    const outputStat = fs.lstatSync(output);
    if (!outputStat.isFile() || outputStat.isSymbolicLink()) {
      throw new Error("Existing bootstrap output must be a regular file, not a link");
    }
    const existing = validateBootstrapBundle(JSON.parse(fs.readFileSync(output, "utf8")));
    if ((outputStat.mode & 0o777) !== 0o600) throw new Error("Existing bootstrap bundle permissions are not 0600");
    if (!sameEnrollment(existing, enrollment, controlPlaneOrigin, remotePort)) {
      throw new Error("Existing bootstrap bundle conflicts with the idempotent enrollment result");
    }
    log(`Bootstrap bundle unchanged at ${output}; installation identity redacted; secret values omitted.`);
    return { status: "unchanged", bundle: existing };
  }

  const bundle = validateBootstrapBundle({
    schema: BOOTSTRAP_SCHEMA,
    version: BOOTSTRAP_VERSION,
    bundleId: randomUUID(),
    controlPlaneOrigin,
    installationId: enrollment.installationId,
    hostname: enrollment.hostname,
    remotePort,
    installationCredential: enrollment.installationCredential,
    issuedAt: now(),
  });
  writePrivateJson(output, bundle);
  log(`Bootstrap bundle created at ${output} with mode 0600; installation identity redacted; secret values omitted.`);
  log("Deliver it through a private out-of-band channel and delete the source file after successful import; secure erasure is not claimed.");
  return { status: "created", bundle };
}

function parseArguments(argv) {
  const options = {};
  for (let index = 0; index < argv.length; index += 2) {
    const name = argv[index];
    const value = argv[index + 1];
    if (!name?.startsWith("--") || value === undefined) throw new Error("Every option requires a value");
    if (Object.hasOwn(options, name)) throw new Error(`Duplicate option: ${name}`);
    options[name] = value;
  }
  const allowed = new Set(["--origin", "--idempotency-key", "--label", "--remote-port", "--output", "--admin-token-file"]);
  for (const name of Object.keys(options)) if (!allowed.has(name)) throw new Error(`Unknown option: ${name}`);
  for (const name of ["--origin", "--idempotency-key", "--remote-port", "--output", "--admin-token-file"]) {
    if (!options[name]) throw new Error(`Missing required option: ${name}`);
  }
  return options;
}

async function main() {
  const options = parseArguments(process.argv.slice(2));
  await enrollAndWriteBundle({
    origin: options["--origin"],
    idempotencyKey: options["--idempotency-key"],
    label: options["--label"] ?? "",
    remotePort: Number(options["--remote-port"]),
    output: options["--output"],
    adminToken: readPrivateToken(path.resolve(options["--admin-token-file"])),
  });
}

if (import.meta.url === pathToFileURL(process.argv[1]).href) {
  try {
    await main();
  } catch (error) {
    console.error(error instanceof Error ? error.message : "Bootstrap export failed");
    process.exitCode = 1;
  }
}
