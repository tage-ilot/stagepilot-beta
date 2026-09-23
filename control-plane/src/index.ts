interface Env {
  REGISTRY: DurableObjectNamespace;
  CLOUDFLARE_API_TOKEN: string;
  CLOUDFLARE_ACCOUNT_ID: string;
  CLOUDFLARE_ZONE_ID: string;
  REMOTE_HOST_SUFFIX: string;
  ADMIN_API_TOKEN: string;
  INSTALLATION_SIGNING_KEY: string;
  // Optional: the release-broker/in-app updater is deferred for this beta
  // (see docs/native-completion-runbook.md). No STAGEPILOT_RELEASE_TOKEN is
  // issued, so this binding is absent in production; release-asset routes
  // then respond 503 instead of ever making an unauthenticated GitHub call.
  GITHUB_RELEASE_TOKEN?: string;
  REMOTE_PORT?: string;
  ENROLLMENT_ENABLED?: string;
  BETA_INSTALLATION_LIMIT?: string;
  BETA_RELEASE_VERSIONS?: string;
  BETA_LATEST_RELEASE_VERSION?: string;
  ENROLLMENT_EXEMPT_SOURCES?: string;
  // Configurable enrollment-quota window in seconds. Defaults to 24h
  // (86400) when unset. TEMPORARY OPERATOR-TESTING OVERRIDE: this is
  // currently deployed as 3600 (1 hour) for the active beta test period so
  // the operator does not have to wait ~24h between exhausted-quota test
  // cycles. This MUST be reverted to 86400 (or longer) before this beta is
  // promoted to stable or opened to real friend-beta users -- do not ship
  // the 1-hour window as the permanent default.
  ENROLLMENT_WINDOW_SECONDS?: string;
}

type Phase = 'disabled' | 'enabling' | 'provisioned' | 'revoking';

interface Installation {
  id: string;
  hostname: string;
  label: string;
  phase: Phase;
  desiredEnabled: boolean;
  generation?: string;
  lastGeneration?: string;
  tunnelId?: string;
  revoked: boolean;
  createdAt: string;
  updatedAt: string;
  statusRate?: RateWindow;
  mutationRate?: RateWindow;
  providerConfirmedAt?: number;
  // Bumped whenever a revoked installation is reactivated so its
  // installation credential rotates to a fresh value even though the
  // durable id/hostname stay the same.
  credentialGeneration?: number;
}

interface RateWindow {
  startedAt: number;
  count: number;
}

interface RegistryStats {
  activeInstallations: number;
  enrollments: number;
  enrollmentDenied: number;
  statusDenied: number;
  mutationDenied: number;
  providerDenied: number;
}

interface SourceQuota extends RateWindow {
  lastSeenAt: number;
}

interface CloudflareEnvelope<T> {
  success: boolean;
  result: T;
}

interface GitHubReleaseAsset {
  name?: unknown;
  url?: unknown;
  size?: unknown;
  state?: unknown;
  content_type?: unknown;
}

interface GitHubRelease {
  tag_name?: unknown;
  draft?: unknown;
  assets?: GitHubReleaseAsset[];
}

const JSON_HEADERS = {
  'content-type': 'application/json; charset=utf-8',
  'cache-control': 'no-store',
};
const ID = /^[a-f0-9]{32}$/;
const GENERATION = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const IDEMPOTENCY_KEY = /^[A-Za-z0-9._:-]{8,128}$/;
const ENROLLMENTS_PER_SOURCE = 3;
const DEFAULT_ENROLLMENT_WINDOW_SECONDS = 86_400;
const MAX_SOURCE_QUOTAS = 2_000;
const STATUS_REQUESTS_PER_MINUTE = 120;
const MUTATION_REQUESTS_PER_MINUTE = 20;
const RECONCILE_CACHE_SECONDS = 30;
const PROVIDER_WINDOW_SECONDS = 300;
const PROVIDER_TOTAL_BUDGET = 600;
const PROVIDER_NORMAL_BUDGET = 480;
const BETA_RELEASE_REPOSITORY = 'tage-ilot/stagepilot-beta';
const RELEASE_VERSION = /^\d+\.\d+\.\d+-beta\.\d+$/;
const RELEASE_METADATA_PER_MINUTE = 30;
const RELEASE_DOWNLOADS_PER_MINUTE = 6;
const MAX_RELEASE_SOURCES = 2_000;
const MAX_MANIFEST_BYTES = 64 * 1024;
const MAX_UPDATER_BYTES = 512 * 1024 * 1024;
const BETA_RELEASE_ORIGIN = 'https://stagepilot-beta-control-plane.stagepilot-illuminary-beta.workers.dev';
const MAX_RELEASE_REDIRECTS = 5;

function exactLengthStream(
  body: ReadableStream<Uint8Array>,
  expected: number,
  maximum: number,
): ReadableStream<Uint8Array> {
  let received = 0;
  return body.pipeThrough(new TransformStream<Uint8Array, Uint8Array>({
    transform(chunk, controller) {
      received += chunk.byteLength;
      if (received > expected || received > maximum) {
        throw new Error('release asset exceeded declared size');
      }
      controller.enqueue(chunk);
    },
    flush() {
      if (received !== expected) throw new Error('release asset size mismatch');
    },
  }));
}

function parseUrl(value: unknown): URL | undefined {
  if (typeof value !== 'string') return undefined;
  try {
    return new URL(value);
  } catch {
    return undefined;
  }
}

class Limited extends Error {
  constructor(
    readonly status: 429 | 503,
    readonly retryAfter: number,
    message: string,
  ) {
    super(message);
  }
}

function reply(body: unknown, status = 200, retryAfter?: number): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...JSON_HEADERS, ...(retryAfter ? { 'retry-after': String(retryAfter) } : {}) },
  });
}

function randomHex(bytes: number): string {
  const value = crypto.getRandomValues(new Uint8Array(bytes));
  return Array.from(value, (part) => part.toString(16).padStart(2, '0')).join('');
}

const NEW_ID_BYTES = 4; // 8 hex chars (32 bits) for new-enrollment / reenroll ids.
const MAX_ID_COLLISION_ATTEMPTS = 5;

// id lineage: installations minted furthest in the past used a 32-hex-char
// id (16 random bytes); a later generation shortened that to 16 hex chars
// (8 random bytes); this generation shortens it again to 8 hex chars (4
// random bytes, ~4.3 billion possibilities) for a shorter Remote URL.
// Every existing installation keeps whatever id/hostname it was minted
// with -- only brand-new enrollments and reenroll() calls made after this
// change get the shorter 8-char id. Route matching therefore has to accept
// all three lengths indefinitely (see the {8}|{16}|{32} regexes above).
async function generateUniqueInstallationId(
  storage: DurableObjectStorage,
): Promise<string> {
  for (let attempt = 0; attempt < MAX_ID_COLLISION_ATTEMPTS; attempt++) {
    const candidate = randomHex(NEW_ID_BYTES);
    const existing = await storage.get(`installation:${candidate}`);
    if (existing === undefined) return candidate;
  }
  throw new Limited(503, 5, 'installation id allocation failed');
}

function encodeBase64Url(value: Uint8Array): string {
  let binary = '';
  for (const byte of value) binary += String.fromCharCode(byte);
  return btoa(binary).replaceAll('+', '-').replaceAll('/', '_').replace(/=+$/, '');
}

async function digest(value: string): Promise<Uint8Array> {
  return new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(value)));
}

async function equalSecret(actual: string, expected: string): Promise<boolean> {
  const [left, right] = await Promise.all([digest(actual), digest(expected)]);
  let difference = left.length ^ right.length;
  for (let index = 0; index < Math.max(left.length, right.length); index += 1) {
    difference |= (left[index] ?? 0) ^ (right[index] ?? 0);
  }
  return difference === 0;
}

async function keyedHash(keyMaterial: string, value: string): Promise<string> {
  const key = await crypto.subtle.importKey(
    'raw', new TextEncoder().encode(keyMaterial), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign'],
  );
  return encodeBase64Url(new Uint8Array(
    await crypto.subtle.sign('HMAC', key, new TextEncoder().encode(value)),
  ));
}

function normalizeSourceAddress(value: string): string | undefined {
  const ipv4 = value.split('.');
  if (ipv4.length === 4 && ipv4.every((part) => /^\d{1,3}$/.test(part) && Number(part) <= 255)) {
    return ipv4.map(Number).join('.');
  }
  const lowered = value.toLowerCase().split('%', 1)[0];
  if (!lowered.includes(':') || !/^[0-9a-f:]+$/.test(lowered) || (lowered.match(/::/g)?.length ?? 0) > 1) {
    return undefined;
  }
  const sides = lowered.split('::');
  const left = sides[0] ? sides[0].split(':') : [];
  const right = sides[1] ? sides[1].split(':') : [];
  if ([...left, ...right].some((part) => !/^[0-9a-f]{1,4}$/.test(part))) return undefined;
  const missing = 8 - left.length - right.length;
  if ((sides.length === 1 && missing !== 0) || (sides.length === 2 && missing < 1)) return undefined;
  const words = [...left, ...Array(Math.max(0, missing)).fill('0'), ...right]
    .map((part) => Number.parseInt(part, 16));
  if (words.length !== 8) return undefined;
  return `${words.slice(0, 4).map((part) => part.toString(16)).join(':')}::/64`;
}

function exemptSources(value: string | undefined): Set<string> {
  const set = new Set<string>();
  for (const raw of (value ?? '').split(',')) {
    const trimmed = raw.trim();
    if (!trimmed) continue;
    const normalized = trimmed.endsWith('::/64')
      ? normalizeSourceAddress(`${trimmed.slice(0, -'::/64'.length)}::`)
      : normalizeSourceAddress(trimmed);
    // Only accept config entries that are already in exact canonical form;
    // anything that round-trips differently (a malformed or spoofed variant)
    // is silently dropped rather than treated as an exemption.
    if (normalized === trimmed) set.add(normalized);
  }
  return set;
}

function bearer(request: Request): string {
  const value = request.headers.get('authorization') ?? '';
  return value.startsWith('Bearer ') ? value.slice(7) : '';
}

async function body(request: Request): Promise<Record<string, unknown>> {
  const length = Number(request.headers.get('content-length') ?? '0');
  if (length > 4096) throw new Error('invalid request');
  const text = await request.text();
  if (text.length > 4096) throw new Error('invalid request');
  const value: unknown = JSON.parse(text);
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('invalid request');
  return value as Record<string, unknown>;
}

function publicInstallation(installation: Installation): Record<string, unknown> {
  return {
    installationId: installation.id,
    hostname: installation.hostname,
    phase: installation.phase,
    generation: installation.generation ?? null,
    revoked: installation.revoked,
    createdAt: installation.createdAt,
    updatedAt: installation.updatedAt,
  };
}

export class Registry {
  private serial: Promise<void> = Promise.resolve();
  private readonly tokenCache = new Map<string, { token: string; expiresAt: number }>();
  private readonly releaseCache = new Map<string, { release: GitHubRelease; expiresAt: number }>();
  private providerLane: 'normal' | 'recovery' = 'normal';

  constructor(
    private readonly state: DurableObjectState,
    private readonly env: Env,
  ) {}

  async fetch(request: Request): Promise<Response> {
    let release = (): void => {};
    const previous = this.serial;
    this.serial = new Promise((resolve) => { release = resolve; });
    await previous;
    try {
      return await this.handle(request);
    } finally {
      release();
    }
  }

  private async handle(request: Request): Promise<Response> {
    try {
      this.validateConfiguration();
      const url = new URL(request.url);
      if (request.method === 'POST' && url.pathname === '/v1/installations/enroll') {
        return await this.enroll(request);
      }
      if (request.method === 'GET' && url.pathname === '/v1/releases/latest.json') {
        return await this.releaseAsset(request, this.latestReleaseVersion(), 'latest.json');
      }
      const releaseAsset = url.pathname.match(/^\/v1\/releases\/v([^/]+)\/([^/]+)$/);
      if (request.method === 'GET' && releaseAsset) {
        return await this.releaseAsset(request, releaseAsset[1], releaseAsset[2]);
      }
      if (request.method === 'GET' && url.pathname === '/v1/admin/metrics') {
        if (!(await this.isAdmin(request))) return reply({ error: 'unauthorized' }, 401);
        return reply(await this.stats());
      }
      const adminRevoke = url.pathname.match(/^\/v1\/admin\/installations\/([a-f0-9]{8}|[a-f0-9]{16}|[a-f0-9]{32})\/revoke$/);
      if (request.method === 'POST' && adminRevoke) {
        if (!(await this.isAdmin(request))) return reply({ error: 'unauthorized' }, 401);
        return await this.withProviderLane('recovery', () => this.adminRevoke(adminRevoke[1]));
      }
      const reenrollRoute = url.pathname.match(/^\/v1\/installations\/([a-f0-9]{8}|[a-f0-9]{16}|[a-f0-9]{32})\/reenroll$/);
      if (request.method === 'POST' && reenrollRoute) {
        const installation = await this.state.storage.get<Installation>(`installation:${reenrollRoute[1]}`);
        if (!installation || installation.revoked || !(await this.isInstallation(request, installation))) {
          return reply({ error: 'unauthorized' }, 401);
        }
        await this.takeInstallationRate(installation, 'mutation');
        return await this.withProviderLane('recovery', () => this.reenroll(installation));
      }
      // Authenticated Enable on a known-but-disabled-or-revoked installation:
      // unlike the other routes below, this deliberately allows a revoked
      // installation through (that is the whole point) as long as the
      // caller can still present its installation credential.
      const reactivateRoute = url.pathname.match(/^\/v1\/installations\/([a-f0-9]{8}|[a-f0-9]{16}|[a-f0-9]{32})\/reactivate$/);
      if (request.method === 'POST' && reactivateRoute) {
        const installation = await this.state.storage.get<Installation>(`installation:${reactivateRoute[1]}`);
        if (!installation || !(await this.isInstallation(request, installation))) {
          return reply({ error: 'unauthorized' }, 401);
        }
        await this.takeInstallationRate(installation, 'mutation');
        return await this.withProviderLane('recovery', () => this.reactivate(installation));
      }
      const route = url.pathname.match(/^\/v1\/installations\/([a-f0-9]{8}|[a-f0-9]{16}|[a-f0-9]{32})\/(status|provision|disable|revoke|reconcile)$/);
      if (!route) return reply({ error: 'not found' }, 404);
      const installation = await this.state.storage.get<Installation>(`installation:${route[1]}`);
      if (!installation || installation.revoked || !(await this.isInstallation(request, installation))) {
        return reply({ error: 'unauthorized' }, 401);
      }
      const action = route[2];
      if (action === 'status' && request.method === 'GET') {
        await this.takeInstallationRate(installation, 'status');
        return reply(publicInstallation(installation));
      }
      if (request.method !== 'POST') return reply({ error: 'method not allowed' }, 405);
      await this.takeInstallationRate(installation, 'mutation');
      if (action === 'provision') return await this.provision(request, installation);
      if (action === 'disable') return await this.withProviderLane('recovery', () => this.disable(installation, false));
      if (action === 'revoke') return await this.withProviderLane('recovery', () => this.disable(installation, true));
      if (action === 'reconcile') {
        const lane = installation.desiredEnabled ? 'normal' : 'recovery';
        return await this.withProviderLane(lane, () => this.reconcile(installation));
      }
      return reply({ error: 'method not allowed' }, 405);
    } catch (error) {
      if (error instanceof Limited) {
        return reply({ error: error.message }, error.status, error.retryAfter);
      }
      // Internal messages are deliberately generic and never include provider
      // response bodies, request headers, or credential values.
      const message = error instanceof Error ? error.message : 'unknown';
      console.error('control-plane request failed', message);
      const response: Record<string, string> = { error: 'operation incomplete; retry reconciliation' };
      const safeDiagnostics = new Set([
        'missing generation',
        'hostname ownership conflict',
        'tunnel creation not confirmed',
        'hostname route not confirmed',
        'installation credential unavailable',
        'hostname removal not confirmed',
        'tunnel revocation not confirmed',
        'ambiguous tunnel ownership',
        'invalid tunnel ownership',
        'ambiguous hostname ownership',
        'tunnel configuration not confirmed',
      ]);
      if (message.startsWith('provider ') || safeDiagnostics.has(message)) {
        response.diagnostic = message;
      }
      return reply(response, 503);
    }
  }

  private validateConfiguration(): void {
    if (!ID.test(this.env.CLOUDFLARE_ACCOUNT_ID) || !ID.test(this.env.CLOUDFLARE_ZONE_ID)) {
      throw new Error('invalid provider configuration');
    }
    if (typeof this.env.CLOUDFLARE_API_TOKEN !== 'string' || this.env.CLOUDFLARE_API_TOKEN.length < 20
      || typeof this.env.ADMIN_API_TOKEN !== 'string' || this.env.ADMIN_API_TOKEN.length < 32
      || typeof this.env.INSTALLATION_SIGNING_KEY !== 'string' || this.env.INSTALLATION_SIGNING_KEY.length < 32
      || (this.env.GITHUB_RELEASE_TOKEN !== undefined && this.env.GITHUB_RELEASE_TOKEN.length < 20)
      || this.env.ADMIN_API_TOKEN === this.env.INSTALLATION_SIGNING_KEY) {
      throw new Error('invalid authentication configuration');
    }
    if (!['true', 'false'].includes(this.env.ENROLLMENT_ENABLED ?? 'true')
      || !Number.isInteger(this.installationLimit()) || this.installationLimit() < 1) {
      throw new Error('invalid enrollment configuration');
    }
    const suffix = this.env.REMOTE_HOST_SUFFIX.toLowerCase();
    if (!/^[a-z0-9](?:[a-z0-9.-]{1,251}[a-z0-9])$/.test(suffix) || suffix.includes('..')) {
      throw new Error('invalid hostname suffix');
    }
    this.remotePort();
    this.releaseVersions();
  }

  private releaseVersions(): string[] {
    const values = (this.env.BETA_RELEASE_VERSIONS ?? '').split(',').map((value) => value.trim()).filter(Boolean);
    if (values.length < 1 || values.length > 20 || new Set(values).size !== values.length
      || values.some((value) => !RELEASE_VERSION.test(value))) {
      throw new Error('invalid beta release allowlist');
    }
    if (!values.includes(this.latestReleaseVersion())) throw new Error('invalid beta latest release');
    return values;
  }

  private latestReleaseVersion(): string {
    const value = this.env.BETA_LATEST_RELEASE_VERSION ?? '';
    if (!RELEASE_VERSION.test(value)) throw new Error('invalid beta latest release');
    return value;
  }

  private allowedReleaseAsset(version: string, filename: string): boolean {
    if (!this.releaseVersions().includes(version)) return false;
    return new Set([
      `StagePilot_${version}_aarch64.dmg`,
      `StagePilot_${version}_x64.dmg`,
      `StagePilot_${version}_aarch64.app.tar.gz`,
      `StagePilot_${version}_x64.app.tar.gz`,
      `StagePilot_${version}_x64-setup.exe`,
    ]).has(filename) || (version === this.latestReleaseVersion() && filename === 'latest.json');
  }

  private async takeReleaseRate(request: Request, kind: 'metadata' | 'download'): Promise<void> {
    const source = normalizeSourceAddress(request.headers.get('cf-connecting-ip') ?? '');
    if (!source) throw new Limited(429, 60, 'release request rate limited');
    const now = Math.floor(Date.now() / 1000);
    const hash = await keyedHash(this.env.INSTALLATION_SIGNING_KEY, `release-source:${source}`);
    const subject = `${kind}:${hash}`;
    const indexKey = 'release-source-index';
    const index = await this.state.storage.get<string[]>(indexKey) ?? [];
    const retained: string[] = [];
    for (const candidate of index) {
      const stored = await this.state.storage.get<RateWindow>(`release-source:${candidate}`);
      if (stored && now - stored.startedAt < 60) retained.push(candidate);
      else await this.state.storage.delete(`release-source:${candidate}`);
    }
    if (!retained.includes(subject) && retained.length >= MAX_RELEASE_SOURCES) {
      throw new Limited(503, 60, 'release service unavailable');
    }
    const key = `release-source:${subject}`;
    const current = await this.state.storage.get<RateWindow>(key);
    const window = !current || now - current.startedAt >= 60 ? { startedAt: now, count: 0 } : current;
    const limit = kind === 'metadata' ? RELEASE_METADATA_PER_MINUTE : RELEASE_DOWNLOADS_PER_MINUTE;
    if (window.count >= limit) throw new Limited(429, Math.max(1, 60 - (now - window.startedAt)), 'release request rate limited');
    await this.state.storage.put({
      [key]: { ...window, count: window.count + 1 },
      [indexKey]: retained.includes(subject) ? retained : [...retained, subject],
    });
  }

  private async releaseAsset(request: Request, version: string, filename: string): Promise<Response> {
    if (!this.allowedReleaseAsset(version, filename)) return reply({ error: 'not found' }, 404);
    if (typeof this.env.GITHUB_RELEASE_TOKEN !== 'string' || this.env.GITHUB_RELEASE_TOKEN.length < 20) {
      // The release broker is deferred for this beta: no GITHUB_RELEASE_TOKEN
      // is configured, so respond 503 without ever calling GitHub.
      return reply({ error: 'release unavailable' }, 503, 60);
    }
    await this.takeReleaseRate(request, filename === 'latest.json' ? 'metadata' : 'download');
    const release = await this.githubRelease(version);
    if (!release) return reply({ error: 'release unavailable' }, 503, 60);
    if (release.tag_name !== `v${version}` || release.draft !== false || !Array.isArray(release.assets)) {
      return reply({ error: 'release unavailable' }, 503, 60);
    }
    const matches = release.assets.filter((asset) => asset.name === filename && asset.state === 'uploaded');
    const asset = matches.length === 1 ? matches[0] : undefined;
    const maximum = filename === 'latest.json' ? MAX_MANIFEST_BYTES : MAX_UPDATER_BYTES;
    const assetUrl = parseUrl(asset?.url);
    const expectedAssetPath = new RegExp(
      `^/repos/${BETA_RELEASE_REPOSITORY.replace('/', '\\/')}/releases/assets/[1-9][0-9]*$`,
    );
    if (!asset || !assetUrl || typeof asset.size !== 'number'
      || asset.size < 1 || asset.size > maximum || assetUrl.protocol !== 'https:'
      || assetUrl.hostname !== 'api.github.com' || assetUrl.search !== ''
      || !expectedAssetPath.test(assetUrl.pathname)) {
      return reply({ error: 'release unavailable' }, 503, 60);
    }
    const response = await this.downloadGitHubAsset(assetUrl.toString());
    if (!response.ok || !response.body) return reply({ error: 'release unavailable' }, 503, 60);
    const length = Number(response.headers.get('content-length') ?? asset.size);
    if (!Number.isFinite(length) || length !== asset.size || length > maximum) {
      return reply({ error: 'release unavailable' }, 503, 60);
    }
    const boundedBody = exactLengthStream(response.body, asset.size, maximum);
    if (filename === 'latest.json') {
      let manifest: string;
      try {
        manifest = await new Response(boundedBody).text();
      } catch {
        return reply({ error: 'release unavailable' }, 503, 60);
      }
      if (!this.validManifest(manifest, version)) {
        return reply({ error: 'release unavailable' }, 503, 60);
      }
      return new Response(manifest, {
        headers: {
          'content-type': 'application/json; charset=utf-8',
          'content-length': String(asset.size),
          'cache-control': 'public, max-age=60, s-maxage=300',
          'x-content-type-options': 'nosniff',
        },
      });
    }
    return new Response(boundedBody, {
      status: 200,
      headers: {
        'content-type': typeof asset.content_type === 'string' ? asset.content_type : 'application/octet-stream',
        'content-length': String(length),
        'cache-control': 'public, max-age=31536000, immutable',
        'x-content-type-options': 'nosniff',
      },
    });
  }

  private validManifest(content: string, version: string): boolean {
    try {
      const manifest = JSON.parse(content) as {
        version?: unknown;
        pub_date?: unknown;
        platforms?: Record<string, { url?: unknown; signature?: unknown }>;
      };
      if (manifest.version !== version || typeof manifest.pub_date !== 'string'
        || !Number.isFinite(Date.parse(manifest.pub_date)) || !manifest.platforms) return false;
      const filenames: Record<string, string> = {
        'darwin-aarch64': `StagePilot_${version}_aarch64.app.tar.gz`,
        'darwin-x86_64': `StagePilot_${version}_x64.app.tar.gz`,
        'windows-x86_64': `StagePilot_${version}_x64-setup.exe`,
      };
      if (Object.keys(manifest.platforms).sort().join(',') !== Object.keys(filenames).sort().join(',')) return false;
      return Object.entries(filenames).every(([platform, filename]) => {
        const entry = manifest.platforms?.[platform];
        return entry?.url === `${BETA_RELEASE_ORIGIN}/v1/releases/v${version}/${filename}`
          && typeof entry.signature === 'string' && entry.signature.length > 0 && entry.signature.length <= 4096;
      });
    } catch {
      return false;
    }
  }

  private async githubRelease(version: string): Promise<GitHubRelease | undefined> {
    const now = Math.floor(Date.now() / 1000);
    const cached = this.releaseCache.get(version);
    if (cached && cached.expiresAt >= now) return cached.release;
    const response = await fetch(
      `https://api.github.com/repos/${BETA_RELEASE_REPOSITORY}/releases/tags/v${version}`,
      { headers: this.githubHeaders('application/vnd.github+json') },
    );
    if (!response.ok) return undefined;
    const release = await response.json() as GitHubRelease;
    this.releaseCache.set(version, { release, expiresAt: now + 300 });
    return release;
  }

  private async downloadGitHubAsset(assetUrl: string): Promise<Response> {
    let response = await fetch(assetUrl, {
      headers: this.githubHeaders('application/octet-stream'),
      redirect: 'manual',
    });
    let current = new URL(assetUrl);
    for (let redirects = 0; [301, 302, 303, 307, 308].includes(response.status); redirects += 1) {
      if (redirects >= MAX_RELEASE_REDIRECTS) return new Response(null, { status: 502 });
      const location = response.headers.get('location');
      if (!location) return new Response(null, { status: 502 });
      const download = new URL(location, current);
      if (download.protocol !== 'https:' || !download.hostname.endsWith('.githubusercontent.com')) {
        return new Response(null, { status: 502 });
      }
      current = download;
      response = await fetch(download, { redirect: 'manual' });
    }
    return response;
  }

  private githubHeaders(accept: string): HeadersInit {
    return {
      accept,
      authorization: `Bearer ${this.env.GITHUB_RELEASE_TOKEN}`,
      'user-agent': 'stagepilot-beta-release-broker/1',
      'x-github-api-version': '2022-11-28',
    };
  }

  private async isAdmin(request: Request): Promise<boolean> {
    const token = bearer(request);
    return token.length >= 32 && equalSecret(token, this.env.ADMIN_API_TOKEN);
  }


  private async credential(id: string, generation = 0): Promise<string> {
    const key = await crypto.subtle.importKey(
      'raw',
      new TextEncoder().encode(this.env.INSTALLATION_SIGNING_KEY),
      { name: 'HMAC', hash: 'SHA-256' },
      false,
      ['sign'],
    );
    const signature = new Uint8Array(
      await crypto.subtle.sign(
        'HMAC',
        key,
        new TextEncoder().encode(`stagepilot-installation:${id}:${generation}`),
      ),
    );
    return `spi_${id}.${encodeBase64Url(signature)}`;
  }

  private async isInstallation(request: Request, installation: Installation): Promise<boolean> {
    const token = bearer(request);
    if (!token.startsWith(`spi_${installation.id}.`)) return false;
    return equalSecret(token, await this.credential(installation.id, installation.credentialGeneration ?? 0));
  }

  // Authenticated reactivation for plain Enable on a previously-known but
  // disabled/revoked installation: the caller already proved ownership of
  // `installation` via its still-valid installation credential (isInstallation),
  // so this never touches the anonymous, per-network ENROLLMENTS_PER_SOURCE
  // quota that /v1/installations/enroll enforces for first-time/unknown
  // clients. Reprovisions the SAME hostname/installation id with a fresh
  // credential generation -- identical outcome to the nonce-replay branch of
  // enroll(), just reached through a proof-of-ownership bearer token instead
  // of a replayed anonymous enrollment nonce.
  private async reactivate(installation: Installation): Promise<Response> {
    if (!installation.revoked) {
      return reply({
        ...publicInstallation(installation),
        installationCredential: await this.credential(installation.id, installation.credentialGeneration ?? 0),
      });
    }
    const stats = await this.stats();
    if ((this.env.ENROLLMENT_ENABLED ?? 'true') !== 'true') {
      await this.bumpDenied(stats, 'enrollmentDenied');
      throw new Limited(503, 300, 'enrollment unavailable');
    }
    if (stats.activeInstallations >= this.installationLimit()) {
      await this.bumpDenied(stats, 'enrollmentDenied');
      throw new Limited(503, 300, 'enrollment unavailable');
    }
    installation.revoked = false;
    installation.phase = 'disabled';
    installation.desiredEnabled = false;
    delete installation.generation;
    delete installation.tunnelId;
    delete installation.providerConfirmedAt;
    installation.credentialGeneration = (installation.credentialGeneration ?? 0) + 1;
    installation.updatedAt = new Date().toISOString();
    stats.activeInstallations += 1;
    await this.state.storage.put({
      [`installation:${installation.id}`]: installation,
      'registry:stats': stats,
    });
    return reply({
      ...publicInstallation(installation),
      installationCredential: await this.credential(installation.id, installation.credentialGeneration ?? 0),
    });
  }

  private async enroll(request: Request): Promise<Response> {
    const input = await body(request);
    const idempotencyKey = input.nonce;
    if (Object.keys(input).length !== 1 || typeof idempotencyKey !== 'string' || !IDEMPOTENCY_KEY.test(idempotencyKey)) {
      return reply({ error: 'invalid request' }, 400);
    }
    const requestKey = `enrollment:${String(idempotencyKey)}`;
    let id = await this.state.storage.get<string>(requestKey);
    let installation = id
      ? await this.state.storage.get<Installation>(`installation:${id}`)
      : undefined;
    if (installation?.revoked) {
      // The rightful owner (the only party who knows this installation's
      // enrollment nonce) is re-enabling after a prior disable/revoke.
      // Reprovision the SAME hostname with a fresh generation on next
      // /provision, instead of minting a brand-new installation identity.
      // A different owner can never reach this branch: they would need to
      // know this exact nonce, which is never exposed by any API response.
      const stats = await this.stats();
      if ((this.env.ENROLLMENT_ENABLED ?? 'true') !== 'true') {
        await this.bumpDenied(stats, 'enrollmentDenied');
        throw new Limited(503, 300, 'enrollment unavailable');
      }
      if (stats.activeInstallations >= this.installationLimit()) {
        await this.bumpDenied(stats, 'enrollmentDenied');
        throw new Limited(503, 300, 'enrollment unavailable');
      }
      installation.revoked = false;
      installation.phase = 'disabled';
      installation.desiredEnabled = false;
      delete installation.generation;
      delete installation.tunnelId;
      delete installation.providerConfirmedAt;
      installation.credentialGeneration = (installation.credentialGeneration ?? 0) + 1;
      installation.updatedAt = new Date().toISOString();
      stats.activeInstallations += 1;
      await this.state.storage.put({
        [`installation:${id}`]: installation,
        'registry:stats': stats,
      });
    }
    if (!installation) {
      const stats = await this.stats();
      if ((this.env.ENROLLMENT_ENABLED ?? 'true') !== 'true') {
        await this.bumpDenied(stats, 'enrollmentDenied');
        throw new Limited(503, 300, 'enrollment unavailable');
      }
      if (stats.activeInstallations >= this.installationLimit()) {
        await this.bumpDenied(stats, 'enrollmentDenied');
        throw new Limited(503, 300, 'enrollment unavailable');
      }
      const source = normalizeSourceAddress(request.headers.get('cf-connecting-ip') ?? '');
      if (!source) return reply({ error: 'invalid request' }, 400);
      const exempt = exemptSources(this.env.ENROLLMENT_EXEMPT_SOURCES).has(source);
      const sourceHash = await keyedHash(this.env.INSTALLATION_SIGNING_KEY, `enrollment-source:${source}`);
      const nowSeconds = Math.floor(Date.now() / 1000);
      const sourceKey = `enrollment-source:${sourceHash}`;
      const windowSeconds = this.enrollmentWindowSeconds();
      let quota = await this.state.storage.get<SourceQuota>(sourceKey);
      if (quota && nowSeconds - quota.startedAt >= windowSeconds) quota = undefined;
      if (!exempt && quota && quota.count >= ENROLLMENTS_PER_SOURCE) {
        await this.bumpDenied(stats, 'enrollmentDenied');
        throw new Limited(429, Math.max(1, windowSeconds - (nowSeconds - quota.startedAt)), 'enrollment rate limited');
      }
      const index = await this.pruneSourceQuotas(nowSeconds);
      if (!quota && !index.includes(sourceHash) && index.length >= MAX_SOURCE_QUOTAS) {
        await this.bumpDenied(stats, 'enrollmentDenied');
        throw new Limited(503, 300, 'enrollment unavailable');
      }
      // id lineage: 32-char (legacy) -> 16-char -> now 8-char for new
      // enrollments; see generateUniqueInstallationId's comment above.
      // Existing 16-char/32-char installations are untouched.
      id = await generateUniqueInstallationId(this.state.storage);
      const now = new Date().toISOString();
      installation = {
        id,
        hostname: `sp-${id}.${this.env.REMOTE_HOST_SUFFIX.toLowerCase()}`,
        label: '',
        phase: 'disabled',
        desiredEnabled: false,
        revoked: false,
        createdAt: now,
        updatedAt: now,
      };
      const nextQuota: SourceQuota = {
        startedAt: quota?.startedAt ?? nowSeconds,
        count: (quota?.count ?? 0) + 1,
        lastSeenAt: nowSeconds,
      };
      stats.activeInstallations += 1;
      stats.enrollments += 1;
      await this.state.storage.put({
        [requestKey]: id,
        [`installation:${id}`]: installation,
        [sourceKey]: nextQuota,
        'enrollment-source-index': index.includes(sourceHash) ? index : [...index, sourceHash],
        'registry:stats': stats,
      });
    }
    return reply({
      ...publicInstallation(installation),
      installationCredential: await this.credential(installation.id, installation.credentialGeneration ?? 0),
    }, 201);
  }

  private installationLimit(): number {
    return Number(this.env.BETA_INSTALLATION_LIMIT ?? '500');
  }

  private enrollmentWindowSeconds(): number {
    const configured = Number(this.env.ENROLLMENT_WINDOW_SECONDS ?? '');
    return Number.isFinite(configured) && configured > 0
      ? configured
      : DEFAULT_ENROLLMENT_WINDOW_SECONDS;
  }

  private async stats(): Promise<RegistryStats> {
    const existing = await this.state.storage.get<RegistryStats>('registry:stats');
    if (existing) return existing;
    const rows = await this.state.storage.list<Installation>({ prefix: 'installation:' });
    const stats: RegistryStats = {
      activeInstallations: [...rows.values()].filter((row) => !row.revoked).length,
      enrollments: rows.size,
      enrollmentDenied: 0,
      statusDenied: 0,
      mutationDenied: 0,
      providerDenied: 0,
    };
    await this.state.storage.put('registry:stats', stats);
    return stats;
  }

  private async bumpDenied(stats: RegistryStats, field: 'enrollmentDenied' | 'statusDenied' | 'mutationDenied' | 'providerDenied'): Promise<void> {
    stats[field] += 1;
    await this.state.storage.put('registry:stats', stats);
  }

  private async pruneSourceQuotas(now: number): Promise<string[]> {
    const index = await this.state.storage.get<string[]>('enrollment-source-index') ?? [];
    const retained: string[] = [];
    for (const hash of index) {
      const key = `enrollment-source:${hash}`;
      const quota = await this.state.storage.get<SourceQuota>(key);
      if (quota && now - quota.lastSeenAt < this.enrollmentWindowSeconds()) retained.push(hash);
      else await this.state.storage.delete(key);
    }
    return retained;
  }

  private async takeInstallationRate(installation: Installation, kind: 'status' | 'mutation'): Promise<void> {
    const now = Math.floor(Date.now() / 1000);
    const field = kind === 'status' ? 'statusRate' : 'mutationRate';
    const limit = kind === 'status' ? STATUS_REQUESTS_PER_MINUTE : MUTATION_REQUESTS_PER_MINUTE;
    const current = installation[field];
    const window = !current || now - current.startedAt >= 60
      ? { startedAt: now, count: 0 }
      : current;
    if (window.count >= limit) {
      await this.bumpDenied(await this.stats(), kind === 'status' ? 'statusDenied' : 'mutationDenied');
      throw new Limited(429, Math.max(1, 60 - (now - window.startedAt)), `${kind} rate limited`);
    }
    installation[field] = { ...window, count: window.count + 1 };
    await this.save(installation);
  }

  private async provision(request: Request, installation: Installation): Promise<Response> {
    const input = await body(request);
    const generation = input.generation;
    if (typeof generation !== 'string' || !GENERATION.test(generation)) {
      return reply({ error: 'invalid generation' }, 400);
    }
    if (installation.phase === 'revoking') return reply({ error: 'disable must finish first' }, 409);
    if (installation.generation && installation.generation !== generation) {
      return reply({ error: 'generation conflict' }, 409);
    }
    if (installation.generation === generation && installation.phase === 'provisioned') {
      const cached = this.cachedProvision(installation);
      if (cached) return cached;
    }
    if (!installation.generation && installation.lastGeneration === generation) {
      return reply({ error: 'a new generation is required' }, 409);
    }
    installation.generation = generation;
    installation.desiredEnabled = true;
    installation.phase = 'enabling';
    installation.updatedAt = new Date().toISOString();
    await this.save(installation);
    return this.ensureProvisioned(installation);
  }

  private async reconcile(installation: Installation): Promise<Response> {
    if (installation.desiredEnabled && installation.generation) {
      const cached = this.cachedProvision(installation);
      if (cached) return cached;
      return this.ensureProvisioned(installation);
    }
    if (installation.phase === 'disabled') return reply(publicInstallation(installation));
    return this.disable(installation, false);
  }

  private async ensureProvisioned(installation: Installation): Promise<Response> {
    const generation = installation.generation;
    if (!generation) throw new Error('missing generation');
    const cached = this.cachedProvision(installation);
    if (cached) return cached;
    const name = this.tunnelName(installation, generation);
    let tunnel = await this.tunnel(name);
    let record = await this.dns(installation.hostname);
    if (record && (!tunnel || !this.owned(record, name, installation.hostname, tunnel.id))) {
      throw new Error('hostname ownership conflict');
    }
    if (!tunnel) {
      await this.cf('POST', this.tunnelsPath(), { name, config_src: 'cloudflare' });
      tunnel = await this.tunnel(name);
      if (!tunnel) throw new Error('tunnel creation not confirmed');
    }
    await this.configure(tunnel.id, installation.hostname);
    if (!record) {
      await this.cf('POST', this.recordsPath(), {
        type: 'CNAME',
        name: installation.hostname,
        content: `${tunnel.id}.cfargotunnel.com`,
        proxied: true,
        ttl: 1,
        comment: name,
      });
      record = await this.dns(installation.hostname);
    }
    if (!record || !this.owned(record, name, installation.hostname, tunnel.id)) {
      throw new Error('hostname route not confirmed');
    }
    const token = await this.cf<unknown>('GET', `${this.tunnelsPath()}/${tunnel.id}/token`);
    if (typeof token !== 'string' || token.length < 20 || /\s/.test(token)) {
      throw new Error('installation credential unavailable');
    }
    installation.tunnelId = tunnel.id;
    installation.phase = 'provisioned';
    installation.providerConfirmedAt = Math.floor(Date.now() / 1000);
    installation.updatedAt = new Date().toISOString();
    await this.save(installation);
    this.tokenCache.set(installation.id, {
      token,
      expiresAt: Math.floor(Date.now() / 1000) + RECONCILE_CACHE_SECONDS,
    });
    return reply({
      ...publicInstallation(installation),
      tunnelId: tunnel.id,
      tunnelToken: token,
    });
  }

  private cachedProvision(installation: Installation): Response | undefined {
    const cached = this.tokenCache.get(installation.id);
    const now = Math.floor(Date.now() / 1000);
    if (installation.phase !== 'provisioned' || !installation.tunnelId
      || !installation.providerConfirmedAt || now - installation.providerConfirmedAt > RECONCILE_CACHE_SECONDS
      || !cached || cached.expiresAt < now) return undefined;
    return reply({
      ...publicInstallation(installation),
      tunnelId: installation.tunnelId,
      tunnelToken: cached.token,
    });
  }

  private async disable(installation: Installation, revoke: boolean): Promise<Response> {
    const wasRevoked = installation.revoked;
    installation.desiredEnabled = false;
    installation.phase = 'revoking';
    installation.revoked ||= revoke;
    installation.updatedAt = new Date().toISOString();
    await this.save(installation);
    const generation = installation.generation;
    if (generation) {
      const name = this.tunnelName(installation, generation);
      const tunnel = await this.tunnel(name);
      const tunnelId = tunnel?.id ?? installation.tunnelId;
      const record = await this.dns(installation.hostname);
      if (record) {
        if (!tunnelId || !this.owned(record, name, installation.hostname, tunnelId)) {
          throw new Error('hostname ownership conflict');
        }
        await this.cf('DELETE', `${this.recordsPath()}/${String(record.id)}`);
        if (await this.dns(installation.hostname)) throw new Error('hostname removal not confirmed');
      }
      if (tunnel) {
        await this.configure(tunnel.id);
        await this.cf('DELETE', `${this.tunnelsPath()}/${tunnel.id}/connections`);
        await this.cf('DELETE', `${this.tunnelsPath()}/${tunnel.id}`);
        if (await this.tunnel(name)) throw new Error('tunnel revocation not confirmed');
      }
    }
    installation.phase = 'disabled';
    installation.lastGeneration = installation.generation;
    delete installation.generation;
    delete installation.tunnelId;
    delete installation.providerConfirmedAt;
    this.tokenCache.delete(installation.id);
    installation.updatedAt = new Date().toISOString();
    await this.save(installation);
    if (revoke && !wasRevoked) {
      const stats = await this.stats();
      stats.activeInstallations = Math.max(0, stats.activeInstallations - 1);
      await this.state.storage.put('registry:stats', stats);
    }
    return reply(publicInstallation(installation));
  }

  private async adminRevoke(id: string): Promise<Response> {
    const installation = await this.state.storage.get<Installation>(`installation:${id}`);
    if (!installation) return reply({ error: 'not found' }, 404);
    if (installation.revoked && installation.phase === 'disabled') {
      return reply(publicInstallation(installation));
    }
    return this.disable(installation, true);
  }

  // Authenticated re-enrollment for "Regenerate Remote link": the caller
  // already proved ownership of `installation` (via isInstallation), so
  // this never touches the anonymous-enrollment source quota that protects
  // /v1/installations/enroll from arbitrary Internet clients. Fully revokes
  // the old installation's tunnel/DNS/credential first (same teardown as
  // disable(..., revoke=true)), then mints a brand-new installation record
  // -- so a legitimate owner can regenerate repeatedly without ever being
  // rate-limited by ENROLLMENTS_PER_SOURCE, while an attacker who does not
  // hold the installation credential still cannot reach this path at all.
  private async reenroll(installation: Installation): Promise<Response> {
    await this.disable(installation, true);
    const stats = await this.stats();
    if ((this.env.ENROLLMENT_ENABLED ?? 'true') !== 'true') {
      await this.bumpDenied(stats, 'enrollmentDenied');
      throw new Limited(503, 300, 'enrollment unavailable');
    }
    if (stats.activeInstallations >= this.installationLimit()) {
      await this.bumpDenied(stats, 'enrollmentDenied');
      throw new Limited(503, 300, 'enrollment unavailable');
    }
    const id = await generateUniqueInstallationId(this.state.storage);
    const now = new Date().toISOString();
    const fresh: Installation = {
      id,
      hostname: `sp-${id}.${this.env.REMOTE_HOST_SUFFIX.toLowerCase()}`,
      label: '',
      phase: 'disabled',
      desiredEnabled: false,
      revoked: false,
      createdAt: now,
      updatedAt: now,
    };
    stats.activeInstallations += 1;
    stats.enrollments += 1;
    await this.state.storage.put({
      [`installation:${id}`]: fresh,
      'registry:stats': stats,
    });
    return reply({
      ...publicInstallation(fresh),
      installationCredential: await this.credential(fresh.id, fresh.credentialGeneration ?? 0),
    }, 201);
  }

  private async save(installation: Installation): Promise<void> {
    await this.state.storage.put(`installation:${installation.id}`, installation);
  }

  private tunnelName(installation: Installation, generation: string): string {
    return `stagepilot-${installation.id}-${generation}`;
  }

  private tunnelsPath(): string {
    return `/accounts/${this.env.CLOUDFLARE_ACCOUNT_ID}/cfd_tunnel`;
  }

  private recordsPath(): string {
    return `/zones/${this.env.CLOUDFLARE_ZONE_ID}/dns_records`;
  }

  private async withProviderLane<T>(lane: 'normal' | 'recovery', operation: () => Promise<T>): Promise<T> {
    const previous = this.providerLane;
    this.providerLane = lane;
    try {
      return await operation();
    } finally {
      this.providerLane = previous;
    }
  }

  private async takeProviderBudget(): Promise<void> {
    const now = Math.floor(Date.now() / 1000);
    const current = await this.state.storage.get<RateWindow>('provider:budget');
    const window = !current || now - current.startedAt >= PROVIDER_WINDOW_SECONDS
      ? { startedAt: now, count: 0 }
      : current;
    const ceiling = this.providerLane === 'recovery' ? PROVIDER_TOTAL_BUDGET : PROVIDER_NORMAL_BUDGET;
    if (window.count >= ceiling) {
      await this.bumpDenied(await this.stats(), 'providerDenied');
      throw new Limited(503, Math.max(1, PROVIDER_WINDOW_SECONDS - (now - window.startedAt)), 'provider capacity unavailable');
    }
    await this.state.storage.put('provider:budget', { ...window, count: window.count + 1 });
  }

  private async cf<T>(method: string, path: string, requestBody?: unknown): Promise<T> {
    await this.takeProviderBudget();
    const response = await fetch(`https://api.cloudflare.com/client/v4${path}`, {
      method,
      headers: {
        authorization: `Bearer ${this.env.CLOUDFLARE_API_TOKEN}`,
        'content-type': 'application/json',
      },
      body: requestBody === undefined ? undefined : JSON.stringify(requestBody),
    });
    const operation = path.includes('/dns_records')
      ? 'DNS'
      : path.endsWith('/token')
        ? 'tunnel-token'
        : path.endsWith('/configurations')
          ? 'tunnel-configuration'
          : 'tunnel-lifecycle';
    if (response.status === 429) {
      const retryAfter = Number(response.headers.get('retry-after') ?? '60');
      throw new Limited(503, Number.isFinite(retryAfter) ? Math.max(1, Math.ceil(retryAfter)) : 60, 'provider capacity unavailable');
    }
    if (!response.ok) throw new Error(`provider ${operation} request failed (HTTP ${response.status})`);
    let envelope: CloudflareEnvelope<T>;
    try {
      envelope = (await response.json()) as CloudflareEnvelope<T>;
    } catch {
      throw new Error(`provider ${operation} returned invalid response`);
    }
    if (envelope.success !== true || !('result' in envelope)) {
      throw new Error(`provider rejected ${operation} request`);
    }
    return envelope.result;
  }

  private async tunnel(name: string): Promise<{ id: string; name: string; config_src: string } | undefined> {
    const query = new URLSearchParams({ name, is_deleted: 'false' });
    const rows = await this.cf<Array<{ id: string; name: string; config_src: string; deleted_at?: string }>>(
      'GET', `${this.tunnelsPath()}?${query}`,
    );
    const matches = rows.filter((row) => row.name === name && !row.deleted_at);
    if (matches.length > 1) throw new Error('ambiguous tunnel ownership');
    if (matches[0] && (matches[0].config_src !== 'cloudflare' || !/^[0-9a-f-]{36}$/i.test(matches[0].id))) {
      throw new Error('invalid tunnel ownership');
    }
    return matches[0];
  }

  private async dns(hostname: string): Promise<Record<string, unknown> | undefined> {
    const query = new URLSearchParams({ name: hostname });
    const rows = await this.cf<Array<Record<string, unknown>>>('GET', `${this.recordsPath()}?${query}`);
    const matches = rows.filter((row) => row.name === hostname);
    if (matches.length > 1) throw new Error('ambiguous hostname ownership');
    return matches[0];
  }

  private owned(record: Record<string, unknown>, name: string, hostname: string, tunnelId: string): boolean {
    return record.comment === name && record.name === hostname && record.type === 'CNAME'
      && record.content === `${tunnelId}.cfargotunnel.com` && record.proxied === true;
  }

  private async configure(tunnelId: string, hostname?: string): Promise<void> {
    const ingress = hostname
      ? [
          { hostname, service: `http://127.0.0.1:${this.remotePort()}` },
          { service: 'http_status:404' },
        ]
      : [{ service: 'http_status:404' }];
    const path = `${this.tunnelsPath()}/${tunnelId}/configurations`;
    const config = { ingress, 'warp-routing': { enabled: false } };
    await this.cf('PUT', path, { config });
    const actual = await this.cf<{ config?: { ingress?: unknown; 'warp-routing'?: { enabled?: unknown } } }>('GET', path);
    const actualIngress = actual.config?.ingress;
    const sameIngress = Array.isArray(actualIngress) && actualIngress.length === ingress.length
      && ingress.every((expected, index) => {
        const received = actualIngress[index];
        return received !== null && typeof received === 'object' && !Array.isArray(received)
          && Object.keys(received).length === Object.keys(expected).length
          && Object.entries(expected).every(([key, value]) => (received as Record<string, unknown>)[key] === value);
      });
    if (!sameIngress || actual.config?.['warp-routing']?.enabled !== false) {
      throw new Error('tunnel configuration not confirmed');
    }
  }

  private remotePort(): number {
    const value = Number(this.env.REMOTE_PORT ?? '8766');
    if (!Number.isInteger(value) || value < 1024 || value > 65535 || value === 8765) {
      throw new Error('invalid remote port');
    }
    return value;
  }
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    if (request.method === 'GET' && url.pathname === '/health') return reply({ status: 'ok' });
    const id = env.REGISTRY.idFromName('stagepilot-private-beta-v1');
    return env.REGISTRY.get(id).fetch(request);
  },
};
