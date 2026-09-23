import { beforeEach, describe, expect, it, vi } from 'vitest';

import { Registry } from './index';

class MemoryStorage {
  readonly values = new Map<string, unknown>();

  async get<T>(key: string): Promise<T | undefined> {
    return this.values.get(key) as T | undefined;
  }

  async put(keyOrEntries: string | Record<string, unknown>, value?: unknown): Promise<void> {
    if (typeof keyOrEntries === 'string') {
      this.values.set(keyOrEntries, structuredClone(value));
      return;
    }
    for (const [key, entry] of Object.entries(keyOrEntries)) {
      this.values.set(key, structuredClone(entry));
    }
  }

  async delete(key: string): Promise<boolean> {
    return this.values.delete(key);
  }

  async list<T>(options?: { prefix?: string }): Promise<Map<string, T>> {
    return new Map(
      [...this.values.entries()]
        .filter(([key]) => !options?.prefix || key.startsWith(options.prefix))
        .map(([key, value]) => [key, structuredClone(value) as T]),
    );
  }
}

class FakeCloudflare {
  tunnels = new Map<string, Record<string, unknown>>();
  records = new Map<string, Record<string, unknown>>();
  configurations = new Map<string, unknown>();
  createCount = 0;
  dnsCreateCount = 0;
  failAfterNextCreate = false;

  fetch = vi.fn(async (input: string | URL | Request, init?: RequestInit): Promise<Response> => {
    const url = new URL(typeof input === 'string' ? input : input instanceof URL ? input : input.url);
    const method = init?.method ?? 'GET';
    const payload = init?.body ? JSON.parse(String(init.body)) as Record<string, unknown> : undefined;
    const parts = url.pathname.split('/');
    let result: unknown;

    if (url.pathname.endsWith('/cfd_tunnel')) {
      if (method === 'GET') {
        result = [...this.tunnels.values()].filter((tunnel) => tunnel.name === url.searchParams.get('name'));
      } else if (method === 'POST') {
        this.createCount += 1;
        const id = `00000000-0000-4000-8000-${String(this.createCount).padStart(12, '0')}`;
        this.tunnels.set(id, { id, ...payload, deleted_at: null });
        result = this.tunnels.get(id);
        if (this.failAfterNextCreate) {
          this.failAfterNextCreate = false;
          throw new TypeError('simulated lost response');
        }
      }
    } else if (url.pathname.includes('/dns_records')) {
      if (method === 'GET') {
        result = [...this.records.values()].filter((record) => record.name === url.searchParams.get('name'));
      } else if (method === 'POST') {
        this.dnsCreateCount += 1;
        const id = `dns-${this.dnsCreateCount}`;
        this.records.set(id, { id, ...payload });
        result = this.records.get(id);
      } else if (method === 'DELETE') {
        this.records.delete(parts.at(-1) ?? '');
        result = {};
      }
    } else if (url.pathname.endsWith('/configurations')) {
      const tunnelId = parts.at(-2) ?? '';
      if (method === 'PUT') {
        const config = payload?.config as {
          ingress: Array<{ hostname?: string; service: string }>;
          'warp-routing': { enabled: boolean };
        };
        this.configurations.set(tunnelId, {
          config: {
            // Cloudflare's real read-back orders object keys differently from
            // the submitted JSON. Semantic equality must not depend on key order.
            ingress: config.ingress.map((row) => row.hostname
              ? { service: row.service, hostname: row.hostname }
              : { service: row.service }),
            'warp-routing': config['warp-routing'],
          },
        });
      }
      result = this.configurations.get(tunnelId);
    } else if (url.pathname.endsWith('/token')) {
      result = `installation-only-token-${parts.at(-2)}`;
    } else if (url.pathname.endsWith('/connections')) {
      result = {};
    } else if (method === 'DELETE' && url.pathname.includes('/cfd_tunnel/')) {
      this.tunnels.delete(parts.at(-1) ?? '');
      result = {};
    } else {
      throw new Error(`unexpected provider request: ${method} ${url.pathname}`);
    }
    return new Response(JSON.stringify({ success: true, result }), {
      status: 200,
      headers: { 'content-type': 'application/json' },
    });
  });
}

const adminToken = 'admin-token-with-at-least-thirty-two-characters';
const env = {
  CLOUDFLARE_API_TOKEN: 'provider-secret-never-returned',
  CLOUDFLARE_ACCOUNT_ID: 'a'.repeat(32),
  CLOUDFLARE_ZONE_ID: 'b'.repeat(32),
  REMOTE_HOST_SUFFIX: 'remote.example.com',
  ADMIN_API_TOKEN: adminToken,
  INSTALLATION_SIGNING_KEY: 'installation-signing-key-at-least-32-bytes',
  PLANNING_CENTER_CLIENT_ID: 'pco-client-id',
  PLANNING_CENTER_CLIENT_SECRET: 'pco-client-secret-never-returned',
  GITHUB_RELEASE_TOKEN: 'github-release-token-server-side-only',
  REMOTE_PORT: '18766',
  ENROLLMENT_ENABLED: 'true',
  BETA_INSTALLATION_LIMIT: '500',
  BETA_RELEASE_VERSIONS: '1.1.103-beta.1,1.1.103-beta.2',
  BETA_LATEST_RELEASE_VERSION: '1.1.103-beta.1',
};

function request(path: string, method = 'GET', token?: string, value?: unknown, source = '203.0.113.10'): Request {
  return new Request(`https://control.example.com${path}`, {
    method,
    headers: {
      ...(token ? { authorization: `Bearer ${token}` } : {}),
      ...(value ? { 'content-type': 'application/json' } : {}),
      'cf-connecting-ip': source,
    },
    body: value ? JSON.stringify(value) : undefined,
  });
}

async function json(response: Response): Promise<Record<string, unknown>> {
  return await response.json() as Record<string, unknown>;
}

async function enroll(registry: Registry, key: string): Promise<Record<string, unknown>> {
  const response = await registry.fetch(request('/v1/installations/enroll', 'POST', undefined, {
    nonce: key,
  }));
  expect(response.status).toBe(201);
  return json(response);
}

function installationPath(installation: Record<string, unknown>, action: string): string {
  return `/v1/installations/${String(installation.installationId)}/${action}`;
}

describe('private-beta control plane', () => {
  let storage: MemoryStorage;
  let provider: FakeCloudflare;
  let registry: Registry;

  beforeEach(() => {
    storage = new MemoryStorage();
    provider = new FakeCloudflare();
    vi.stubGlobal('fetch', provider.fetch);
    registry = new Registry({ storage } as unknown as DurableObjectState, env as never);
  });

  it('rejects malformed enrollment before allocating installation or provider resources', async () => {
    const response = await registry.fetch(request('/v1/installations/enroll', 'POST', undefined, {
      unexpected: 'unauthorized-request',
    }));
    expect(response.status).toBe(400);
    expect(storage.values.size).toBe(0);
    expect(provider.fetch).not.toHaveBeenCalled();
  });

  it('fails closed when any required Worker runtime secret is missing', async () => {
    for (const name of ['CLOUDFLARE_API_TOKEN', 'ADMIN_API_TOKEN', 'INSTALLATION_SIGNING_KEY', 'PLANNING_CENTER_CLIENT_ID', 'PLANNING_CENTER_CLIENT_SECRET'] as const) {
      registry = new Registry(
        { storage } as unknown as DurableObjectState,
        { ...env, [name]: undefined } as never,
      );
      const response = await registry.fetch(request('/v1/installations/enroll', 'POST', undefined, {
        nonce: 'missing-secret-test',
      }));
      expect(response.status).toBe(503);
      expect(await json(response)).toEqual({ error: 'operation incomplete; retry reconciliation' });
    }
    expect(storage.values.size).toBe(0);
    expect(provider.fetch).not.toHaveBeenCalled();
  });

  it('still enrolls when the deferred GITHUB_RELEASE_TOKEN binding is absent', async () => {
    registry = new Registry(
      { storage } as unknown as DurableObjectState,
      { ...env, GITHUB_RELEASE_TOKEN: undefined } as never,
    );
    const response = await registry.fetch(request('/v1/installations/enroll', 'POST', undefined, {
      nonce: 'no-release-token-test',
    }));
    expect(response.status).toBe(201);
  });

  it('enrolls durable random identities idempotently and issues isolated credentials', async () => {
    const first = await enroll(registry, 'friend-one-request');
    const replay = await enroll(registry, 'friend-one-request');
    const second = await enroll(registry, 'friend-two-request');

    expect(replay).toEqual(first);
    expect(second.installationId).not.toBe(first.installationId);
    expect(second.hostname).not.toBe(first.hostname);
    expect(second.installationCredential).not.toBe(first.installationCredential);
    expect(String(first.hostname)).toBe(`sp-${String(first.installationId)}.remote.example.com`);
    expect(String(first.installationId)).toMatch(/^[a-f0-9]{8}$/);
    expect(JSON.stringify([...storage.values.values()])).not.toContain('provider-secret-never-returned');
  });

  it('retries id generation on a collision instead of overwriting the existing installation', async () => {
    // Force the first two 4-byte draws to collide with an id that already
    // has a real installation record, then let the third draw succeed --
    // proves the retry loop advances past a collision rather than
    // silently overwriting someone else's installation.
    const collidingId = 'deadbeef';
    storage.values.set(`installation:${collidingId}`, {
      id: collidingId,
      hostname: `sp-${collidingId}.remote.example.com`,
      label: 'victim',
      phase: 'enabled',
      desiredEnabled: true,
      revoked: false,
      createdAt: 'then',
      updatedAt: 'then',
    });
    const draws = [collidingId, collidingId, 'cafef00d'];
    const spy = vi.spyOn(crypto, 'getRandomValues').mockImplementation((array) => {
      const hex = draws.shift();
      if (!hex) throw new Error('ran out of scripted random draws');
      const bytes = hex.match(/.{2}/g)!.map((byte) => parseInt(byte, 16));
      (array as Uint8Array).set(bytes);
      return array;
    });

    try {
      const response = await registry.fetch(request('/v1/installations/enroll', 'POST', undefined, {
        nonce: 'collision-retry-request',
      }));
      expect(response.status).toBe(201);
      const installation = await json(response);
      expect(installation.installationId).toBe('cafef00d');
      expect(installation.installationId).not.toBe(collidingId);
      // The pre-seeded "victim" installation must be completely untouched.
      const victim = storage.values.get(`installation:${collidingId}`) as Record<string, unknown>;
      expect(victim.label).toBe('victim');
      expect(victim.phase).toBe('enabled');
    } finally {
      spy.mockRestore();
    }
  });

  it('fails closed with a 503 when every id-generation attempt collides', async () => {
    const spy = vi.spyOn(crypto, 'getRandomValues').mockImplementation((array) => {
      (array as Uint8Array).set([0xde, 0xad, 0xbe, 0xef]);
      return array;
    });
    storage.values.set('installation:deadbeef', {
      id: 'deadbeef',
      hostname: 'sp-deadbeef.remote.example.com',
      label: '',
      phase: 'disabled',
      desiredEnabled: false,
      revoked: false,
      createdAt: 'then',
      updatedAt: 'then',
    });

    try {
      const response = await registry.fetch(request('/v1/installations/enroll', 'POST', undefined, {
        nonce: 'collision-exhausted-request',
      }));
      expect(response.status).toBe(503);
    } finally {
      spy.mockRestore();
    }
  });

  it('enforces normalized-source enrollment quota while replay and another source remain available', async () => {
    const ipv6 = '2001:0db8:0001:0002:0000:0000:0000:0001';
    const compactSamePrefix = '2001:db8:1:2::abcd';
    const accepted: Record<string, unknown>[] = [];
    for (const nonce of ['source-one-0001', 'source-one-0002', 'source-one-0003']) {
      const response = await registry.fetch(request('/v1/installations/enroll', 'POST', undefined, { nonce }, ipv6));
      expect(response.status).toBe(201);
      accepted.push(await json(response));
    }
    const before = [...storage.values.keys()].filter((key) => key.startsWith('installation:')).length;
    const denied = await registry.fetch(request(
      '/v1/installations/enroll', 'POST', undefined, { nonce: 'source-one-0004' }, compactSamePrefix,
    ));
    expect(denied.status).toBe(429);
    expect(Number(denied.headers.get('retry-after'))).toBeGreaterThan(0);
    expect([...storage.values.keys()].filter((key) => key.startsWith('installation:'))).toHaveLength(before);
    const replay = await registry.fetch(request(
      '/v1/installations/enroll', 'POST', undefined, { nonce: 'source-one-0001' }, compactSamePrefix,
    ));
    expect(await json(replay)).toEqual(accepted[0]);
    const other = await registry.fetch(request(
      '/v1/installations/enroll', 'POST', undefined, { nonce: 'source-two-0001' }, '2001:db8:1:3::1',
    ));
    expect(other.status).toBe(201);
    expect(provider.fetch).not.toHaveBeenCalled();
  });

  it('regenerating a link repeatedly never consumes the anonymous per-source enrollment quota', async () => {
    // Regression test for the reported bug: the desktop app used to call
    // the anonymous /enroll route on every "Regenerate Remote link" click,
    // so 3 regenerations from one network exhausted ENROLLMENTS_PER_SOURCE
    // and every further regeneration attempt (and the fallback re-enable)
    // was denied with 429, leaving Remote permanently broken from the UI.
    const source = '203.0.113.55';
    const first = await registry.fetch(request(
      '/v1/installations/enroll', 'POST', undefined, { nonce: 'regen-quota-0001' }, source,
    ));
    expect(first.status).toBe(201);
    let installation = await json(first) as Record<string, unknown>;

    // Exhaust the per-source quota with two more (unrelated) anonymous
    // enrollments from the same source -- 3 total, at the limit.
    for (const nonce of ['regen-quota-0002', 'regen-quota-0003']) {
      const response = await registry.fetch(request('/v1/installations/enroll', 'POST', undefined, { nonce }, source));
      expect(response.status).toBe(201);
    }
    const quotaExhausted = await registry.fetch(request(
      '/v1/installations/enroll', 'POST', undefined, { nonce: 'regen-quota-0004' }, source,
    ));
    expect(quotaExhausted.status).toBe(429);

    // The authenticated reenroll route must still work for the already-
    // enrolled installation from that same (now quota-exhausted) source,
    // repeatedly, because it is never billed against the anonymous quota.
    for (let attempt = 0; attempt < 5; attempt += 1) {
      const response = await registry.fetch(request(
        installationPath(installation, 'reenroll'), 'POST', String(installation.installationCredential), undefined, source,
      ));
      expect(response.status).toBe(201);
      const next = await json(response);
      expect(next.installationId).not.toBe(installation.installationId);
      installation = next;
    }
  });

  it('enables Enable to reactivate a known disabled installation without touching the anonymous quota', async () => {
    const source = '203.0.113.77';
    const first = await registry.fetch(request(
      '/v1/installations/enroll', 'POST', undefined, { nonce: 'reactivate-quota-0001' }, source,
    ));
    expect(first.status).toBe(201);
    const installation = await json(first) as Record<string, unknown>;

    // Exhaust the per-source anonymous enrollment quota with 2 more.
    for (const nonce of ['reactivate-quota-0002', 'reactivate-quota-0003']) {
      const response = await registry.fetch(request('/v1/installations/enroll', 'POST', undefined, { nonce }, source));
      expect(response.status).toBe(201);
    }
    const quotaExhausted = await registry.fetch(request(
      '/v1/installations/enroll', 'POST', undefined, { nonce: 'reactivate-quota-0004' }, source,
    ));
    expect(quotaExhausted.status).toBe(429);

    // Disable (not revoke) this installation, then reactivate it via the
    // authenticated path -- this must succeed even though the anonymous
    // quota for this source is fully exhausted.
    const disabled = await registry.fetch(request(
      installationPath(installation, 'disable'), 'POST', String(installation.installationCredential), undefined, source,
    ));
    expect(disabled.status).toBe(200);

    const reactivated = await registry.fetch(request(
      installationPath(installation, 'reactivate'), 'POST', String(installation.installationCredential), undefined, source,
    ));
    expect(reactivated.status).toBe(200);
    const reactivatedBody = await json(reactivated);
    expect(reactivatedBody.installationId).toBe(installation.installationId);
    expect(reactivatedBody.hostname).toBe(installation.hostname);
    expect(reactivatedBody.installationCredential).toBe(installation.installationCredential);
  });

  it('reactivates a revoked installation with a fresh credential, never the anonymous route', async () => {
    const source = '203.0.113.88';
    const first = await registry.fetch(request(
      '/v1/installations/enroll', 'POST', undefined, { nonce: 'reactivate-revoked-0001' }, source,
    ));
    expect(first.status).toBe(201);
    const installation = await json(first) as Record<string, unknown>;

    for (const nonce of ['reactivate-revoked-0002', 'reactivate-revoked-0003']) {
      const response = await registry.fetch(request('/v1/installations/enroll', 'POST', undefined, { nonce }, source));
      expect(response.status).toBe(201);
    }
    const quotaExhausted = await registry.fetch(request(
      '/v1/installations/enroll', 'POST', undefined, { nonce: 'reactivate-revoked-0004' }, source,
    ));
    expect(quotaExhausted.status).toBe(429);

    const revoked = await registry.fetch(request(
      installationPath(installation, 'revoke'), 'POST', String(installation.installationCredential), undefined, source,
    ));
    expect(revoked.status).toBe(200);
    expect((await json(revoked)).revoked).toBe(true);

    const reactivated = await registry.fetch(request(
      installationPath(installation, 'reactivate'), 'POST', String(installation.installationCredential), undefined, source,
    ));
    expect(reactivated.status).toBe(200);
    const reactivatedBody = await json(reactivated);
    expect(reactivatedBody.installationId).toBe(installation.installationId);
    expect(reactivatedBody.hostname).toBe(installation.hostname);
    // A reactivation of a revoked installation rotates the credential (a
    // fresh generation), unlike reactivating a merely-disabled one above.
    expect(reactivatedBody.installationCredential).not.toBe(installation.installationCredential);
    expect(reactivatedBody.revoked).toBe(false);

    // The stale, pre-reactivation credential must be rejected everywhere.
    const staleRejected = await registry.fetch(request(
      installationPath(installation, 'status'), 'GET', String(installation.installationCredential), undefined, source,
    ));
    expect(staleRejected.status).toBe(401);
  });

  it('rejects reactivation without proof of ownership of the installation credential', async () => {
    const installation = await enroll(registry, 'reactivate-unauthorized-request');
    const response = await registry.fetch(request(
      installationPath(installation, 'reactivate'), 'POST', 'spi_wrongwrongwrongwrongwrongwron.wrongwrongwrongwrongwrongwrongwrongwrongwrongw',
    ));
    expect(response.status).toBe(401);
  });

  it('honors a configured ENROLLMENT_WINDOW_SECONDS instead of the 24h default', async () => {
    registry = new Registry(
      { storage } as unknown as DurableObjectState,
      { ...env, ENROLLMENT_WINDOW_SECONDS: '1' } as never,
    );
    const source = '203.0.113.99';
    for (const nonce of ['short-window-0001', 'short-window-0002', 'short-window-0003']) {
      const response = await registry.fetch(request('/v1/installations/enroll', 'POST', undefined, { nonce }, source));
      expect(response.status).toBe(201);
    }
    const exhausted = await registry.fetch(request(
      '/v1/installations/enroll', 'POST', undefined, { nonce: 'short-window-0004' }, source,
    ));
    expect(exhausted.status).toBe(429);
    // Retry-After must reflect the short configured window, never the 24h default.
    expect(Number(exhausted.headers.get('retry-after'))).toBeLessThanOrEqual(1);
  });

  it('exempts a configured developer source from the per-source enrollment quota', async () => {
    registry = new Registry(
      { storage } as unknown as DurableObjectState,
      { ...env, ENROLLMENT_EXEMPT_SOURCES: '192.0.2.10,2001:db8:1:4::/64' } as never,
    );
    const ipv4Nonces = ['exempt-ipv4-0001', 'exempt-ipv4-0002', 'exempt-ipv4-0003', 'exempt-ipv4-0004', 'exempt-ipv4-0005'];
    for (const nonce of ipv4Nonces) {
      const response = await registry.fetch(request('/v1/installations/enroll', 'POST', undefined, { nonce }, '192.0.2.10'));
      expect(response.status).toBe(201);
    }
    const ipv6Address = '2001:db8:1:4:aaaa:bbbb:cccc:dddd';
    const ipv6Nonces = ['exempt-ipv6-0001', 'exempt-ipv6-0002', 'exempt-ipv6-0003', 'exempt-ipv6-0004'];
    for (const nonce of ipv6Nonces) {
      const response = await registry.fetch(request('/v1/installations/enroll', 'POST', undefined, { nonce }, ipv6Address));
      expect(response.status).toBe(201);
    }
    const stats = await registry.fetch(request('/v1/admin/metrics', 'GET', adminToken));
    const aggregate = await json(stats);
    expect(aggregate.enrollments).toBe(ipv4Nonces.length + ipv6Nonces.length);
    expect(aggregate.activeInstallations).toBe(ipv4Nonces.length + ipv6Nonces.length);
    expect(aggregate.enrollmentDenied).toBe(0);
  });

  it('still denies a non-exempt source with 429 and retry-after when an exemption list is configured', async () => {
    registry = new Registry(
      { storage } as unknown as DurableObjectState,
      { ...env, ENROLLMENT_EXEMPT_SOURCES: '192.0.2.10,2001:db8:1:4::/64' } as never,
    );
    const other = '198.51.100.9';
    for (const nonce of ['non-exempt-0001', 'non-exempt-0002', 'non-exempt-0003']) {
      const response = await registry.fetch(request('/v1/installations/enroll', 'POST', undefined, { nonce }, other));
      expect(response.status).toBe(201);
    }
    const denied = await registry.fetch(request(
      '/v1/installations/enroll', 'POST', undefined, { nonce: 'non-exempt-0004' }, other,
    ));
    expect(denied.status).toBe(429);
    expect(Number(denied.headers.get('retry-after'))).toBeGreaterThan(0);
  });

  it('does not treat a malformed or spoofed variant of an exempt value as exempt', async () => {
    registry = new Registry(
      { storage } as unknown as DurableObjectState,
      {
        ...env,
        ENROLLMENT_EXEMPT_SOURCES:
          '192.000.002.010,2001:0db8:0001:0004:0000:0000:0000:0000/64,2001:db8:1:5::/64',
      } as never,
    );
    for (const nonce of ['spoof-0001', 'spoof-0002', 'spoof-0003']) {
      const response = await registry.fetch(request('/v1/installations/enroll', 'POST', undefined, { nonce }, '192.0.2.10'));
      expect(response.status).toBe(201);
    }
    const denied = await registry.fetch(request(
      '/v1/installations/enroll', 'POST', undefined, { nonce: 'spoof-0004' }, '192.0.2.10',
    ));
    expect(denied.status).toBe(429);
  });

  it('treats an empty or unset exemption list as exempting nobody', async () => {
    for (const exemptSources of [undefined, '', '   ', ',,']) {
      const storageForRun = new MemoryStorage();
      const registryForRun = new Registry(
        { storage: storageForRun } as unknown as DurableObjectState,
        { ...env, ENROLLMENT_EXEMPT_SOURCES: exemptSources } as never,
      );
      for (const nonce of ['empty-0001', 'empty-0002', 'empty-0003']) {
        const response = await registryForRun.fetch(request(
          '/v1/installations/enroll', 'POST', undefined, { nonce }, '192.0.2.10',
        ));
        expect(response.status).toBe(201);
      }
      const denied = await registryForRun.fetch(request(
        '/v1/installations/enroll', 'POST', undefined, { nonce: 'empty-0004' }, '192.0.2.10',
      ));
      expect(denied.status).toBe(429);
    }
  });

  it('enforces the installation ceiling and kill switch without creating installations', async () => {
    registry = new Registry({ storage } as unknown as DurableObjectState, { ...env, BETA_INSTALLATION_LIMIT: '1' } as never);
    await enroll(registry, 'ceiling-first-request');
    const denied = await registry.fetch(request(
      '/v1/installations/enroll', 'POST', undefined, { nonce: 'ceiling-second-request' }, '198.51.100.2',
    ));
    expect(denied.status).toBe(503);
    expect([...storage.values.keys()].filter((key) => key.startsWith('installation:'))).toHaveLength(1);

    const disabledStorage = new MemoryStorage();
    registry = new Registry(
      { storage: disabledStorage } as unknown as DurableObjectState,
      { ...env, ENROLLMENT_ENABLED: 'false' } as never,
    );
    const switchedOff = await registry.fetch(request(
      '/v1/installations/enroll', 'POST', undefined, { nonce: 'kill-switch-request' },
    ));
    expect(switchedOff.status).toBe(503);
    expect([...disabledStorage.values.keys()].some((key) => key.startsWith('installation:'))).toBe(false);
  });

  it('uses distinct status and mutation quotas and exposes only aggregate counters', async () => {
    const installation = await enroll(registry, 'rate-limits-request');
    for (let index = 0; index < 120; index += 1) {
      const response = await registry.fetch(request(
        installationPath(installation, 'status'), 'GET', String(installation.installationCredential),
      ));
      expect(response.status).toBe(200);
    }
    const statusDenied = await registry.fetch(request(
      installationPath(installation, 'status'), 'GET', String(installation.installationCredential),
    ));
    expect(statusDenied.status).toBe(429);

    const generation = '55555555-5555-4555-8555-555555555555';
    for (let index = 0; index < 20; index += 1) {
      const response = await registry.fetch(request(
        installationPath(installation, 'provision'), 'POST', String(installation.installationCredential), { generation },
      ));
      expect(response.status).toBe(200);
    }
    const mutationDenied = await registry.fetch(request(
      installationPath(installation, 'provision'), 'POST', String(installation.installationCredential), { generation },
    ));
    expect(mutationDenied.status).toBe(429);
    expect(provider.createCount).toBe(1);
    expect(provider.dnsCreateCount).toBe(1);

    const metrics = await registry.fetch(request('/v1/admin/metrics', 'GET', adminToken));
    expect(metrics.status).toBe(200);
    const aggregate = await json(metrics);
    expect(aggregate.statusDenied).toBe(1);
    expect(aggregate.mutationDenied).toBe(1);
    expect(JSON.stringify(aggregate)).not.toContain('203.0.113.10');
  });

  it('reserves provider capacity for revoke and recovery operations', async () => {
    const installation = await enroll(registry, 'provider-budget-request');
    storage.values.set('provider:budget', { startedAt: Math.floor(Date.now() / 1000), count: 480 });
    const generation = '66666666-6666-4666-8666-666666666666';
    const provision = await registry.fetch(request(
      installationPath(installation, 'provision'), 'POST', String(installation.installationCredential), { generation },
    ));
    expect(provision.status).toBe(503);
    expect(provider.fetch).not.toHaveBeenCalled();
    const revoke = await registry.fetch(request(
      installationPath(installation, 'revoke'), 'POST', String(installation.installationCredential), {},
    ));
    expect(revoke.status).toBe(200);
    expect((await json(revoke)).revoked).toBe(true);
  });

  it('does not let enabled reconcile consume reserved revoke capacity', async () => {
    const installation = await enroll(registry, 'reconcile-budget-request');
    const generation = '77777777-7777-4777-8777-777777777777';
    expect((await registry.fetch(request(
      installationPath(installation, 'provision'), 'POST', String(installation.installationCredential), { generation },
    ))).status).toBe(200);
    storage.values.set('provider:budget', { startedAt: Math.floor(Date.now() / 1000), count: 480 });
    registry = new Registry({ storage } as unknown as DurableObjectState, env as never);
    const callsBefore = provider.fetch.mock.calls.length;

    const reconcile = await registry.fetch(request(
      installationPath(installation, 'reconcile'), 'POST', String(installation.installationCredential),
    ));
    expect(reconcile.status).toBe(503);
    expect(provider.fetch).toHaveBeenCalledTimes(callsBefore);

    const revoke = await registry.fetch(request(
      installationPath(installation, 'revoke'), 'POST', String(installation.installationCredential),
    ));
    expect(revoke.status).toBe(200);
  });

  it('does not log a non-JSON provider response body', async () => {
    const installation = await enroll(registry, 'provider-body-request');
    const providerBody = 'PRIVATE_PROVIDER_BODY';
    vi.stubGlobal('fetch', vi.fn(async () => new Response(providerBody, { status: 200 })));
    const error = vi.spyOn(console, 'error').mockImplementation(() => undefined);

    const response = await registry.fetch(request(
      installationPath(installation, 'provision'),
      'POST',
      String(installation.installationCredential),
      { generation: '88888888-8888-4888-8888-888888888888' },
    ));

    expect(response.status).toBe(503);
    expect(JSON.stringify(await json(response))).not.toContain(providerBody);
    expect(JSON.stringify(error.mock.calls)).not.toContain(providerBody);
  });

  it('rejects release-asset requests without ever calling GitHub when the token is absent', async () => {
    registry = new Registry(
      { storage } as unknown as DurableObjectState,
      { ...env, GITHUB_RELEASE_TOKEN: undefined } as never,
    );
    const github = vi.fn();
    vi.stubGlobal('fetch', github);

    const response = await registry.fetch(request('/v1/releases/latest.json'));

    expect(response.status).toBe(503);
    expect(github).not.toHaveBeenCalled();
  });

  it('serves only allowlisted private-release metadata through the public broker', async () => {
    const version = '1.1.103-beta.1';
    const broker = 'https://stagepilot-beta-control-plane.stagepilot-illuminary-beta.workers.dev/v1/releases';
    const manifest = JSON.stringify({
      version,
      pub_date: '2026-09-15T00:00:00Z',
      platforms: {
        'darwin-aarch64': { url: `${broker}/v${version}/StagePilot_${version}_aarch64.app.tar.gz`, signature: 'a' },
        'darwin-x86_64': { url: `${broker}/v${version}/StagePilot_${version}_x64.app.tar.gz`, signature: 'b' },
        'windows-x86_64': { url: `${broker}/v${version}/StagePilot_${version}_x64-setup.exe`, signature: 'c' },
      },
    });
    const github = vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const url = new URL(typeof input === 'string' ? input : input instanceof URL ? input : input.url);
      if (url.hostname === 'api.github.com') {
        expect(new Headers(init?.headers).get('authorization')).toBe('Bearer github-release-token-server-side-only');
      }
      if (url.pathname.endsWith('/releases/tags/v1.1.103-beta.1')) {
        return new Response(JSON.stringify({
          tag_name: 'v1.1.103-beta.1',
          draft: false,
          assets: [{
            name: 'latest.json',
            url: 'https://api.github.com/repos/tage-ilot/stagepilot-beta/releases/assets/101',
            size: manifest.length,
            state: 'uploaded',
            content_type: 'application/json',
          }],
        }));
      }
      if (url.pathname.endsWith('/releases/assets/101')) {
        return new Response(null, {
          status: 302,
          headers: { location: 'https://release-assets.githubusercontent.com/private-signed-download' },
        });
      }
      if (url.hostname === 'release-assets.githubusercontent.com') {
        expect(new Headers(init?.headers).get('authorization')).toBeNull();
        return new Response(manifest, { headers: { 'content-length': String(manifest.length) } });
      }
      throw new Error(`unexpected GitHub request: ${url.pathname}`);
    });
    vi.stubGlobal('fetch', github);

    const response = await registry.fetch(request('/v1/releases/latest.json'));

    expect(response.status).toBe(200);
    expect(response.headers.get('cache-control')).toBe('public, max-age=60, s-maxage=300');
    expect(await response.text()).toBe(manifest);
    expect(github).toHaveBeenCalledTimes(3);
  });

  it('rejects a manifest that points beta clients outside the broker', async () => {
    const manifest = JSON.stringify({
      version: '1.1.103-beta.1',
      pub_date: '2026-09-15T00:00:00Z',
      platforms: {
        'darwin-aarch64': { url: 'https://github.com/tage-ilot/stagepilot/releases/download/v1.1.103/a', signature: 'a' },
        'darwin-x86_64': { url: 'https://github.com/tage-ilot/stagepilot/releases/download/v1.1.103/b', signature: 'b' },
        'windows-x86_64': { url: 'https://github.com/tage-ilot/stagepilot/releases/download/v1.1.103/c', signature: 'c' },
      },
    });
    vi.stubGlobal('fetch', vi.fn(async (input: string | URL | Request) => {
      const url = new URL(typeof input === 'string' ? input : input instanceof URL ? input : input.url);
      if (url.pathname.endsWith('/releases/tags/v1.1.103-beta.1')) {
        return new Response(JSON.stringify({
          tag_name: 'v1.1.103-beta.1',
          draft: false,
          assets: [{
            name: 'latest.json',
            url: 'https://api.github.com/repos/tage-ilot/stagepilot-beta/releases/assets/103',
            size: manifest.length,
            state: 'uploaded',
          }],
        }));
      }
      return new Response(manifest, { headers: { 'content-length': String(manifest.length) } });
    }));

    expect((await registry.fetch(request('/v1/releases/latest.json'))).status).toBe(503);
  });

  it('rejects a release download when any redirect leaves GitHub asset hosting', async () => {
    const version = '1.1.103-beta.1';
    const filename = `StagePilot_${version}_x64-setup.exe`;
    const github = vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const url = new URL(typeof input === 'string' ? input : input instanceof URL ? input : input.url);
      if (url.pathname.endsWith(`/releases/tags/v${version}`)) {
        return new Response(JSON.stringify({
          tag_name: `v${version}`,
          draft: false,
          assets: [{
            name: filename,
            url: 'https://api.github.com/repos/tage-ilot/stagepilot-beta/releases/assets/104',
            size: 1,
            state: 'uploaded',
          }],
        }));
      }
      if (url.hostname === 'api.github.com') {
        expect(new Headers(init?.headers).get('authorization')).toBeTruthy();
        return new Response(null, {
          status: 302,
          headers: { location: 'https://release-assets.githubusercontent.com/first-hop' },
        });
      }
      expect(new Headers(init?.headers).get('authorization')).toBeNull();
      return new Response(null, {
        status: 302,
        headers: { location: 'https://example.test/credential-trap' },
      });
    });
    vi.stubGlobal('fetch', github);

    expect((await registry.fetch(request(`/v1/releases/v${version}/${filename}`))).status).toBe(503);
    expect(github).toHaveBeenCalledTimes(3);
  });

  it('rejects a manifest body larger than its declared GitHub asset size', async () => {
    const version = '1.1.103-beta.1';
    const broker = 'https://stagepilot-beta-control-plane.stagepilot-illuminary-beta.workers.dev/v1/releases';
    const manifest = JSON.stringify({
      version,
      pub_date: '2026-09-15T00:00:00Z',
      platforms: {
        'darwin-aarch64': { url: `${broker}/v${version}/StagePilot_${version}_aarch64.app.tar.gz`, signature: 'a' },
        'darwin-x86_64': { url: `${broker}/v${version}/StagePilot_${version}_x64.app.tar.gz`, signature: 'b' },
        'windows-x86_64': { url: `${broker}/v${version}/StagePilot_${version}_x64-setup.exe`, signature: 'c' },
      },
    });
    vi.stubGlobal('fetch', vi.fn(async (input: string | URL | Request) => {
      const url = new URL(typeof input === 'string' ? input : input instanceof URL ? input : input.url);
      if (url.pathname.endsWith(`/releases/tags/v${version}`)) {
        return new Response(JSON.stringify({
          tag_name: `v${version}`,
          draft: false,
          assets: [{
            name: 'latest.json',
            url: 'https://api.github.com/repos/tage-ilot/stagepilot-beta/releases/assets/105',
            size: manifest.length,
            state: 'uploaded',
          }],
        }));
      }
      return new Response(`${manifest}x`);
    }));

    expect((await registry.fetch(request('/v1/releases/latest.json'))).status).toBe(503);
  });

  it('rejects nonallowlisted versions and filenames without contacting GitHub', async () => {
    const github = vi.fn();
    vi.stubGlobal('fetch', github);

    for (const path of [
      '/v1/releases/v1.1.104-beta.1/StagePilot_1.1.104-beta.1_x64-setup.exe',
      '/v1/releases/v1.1.103-beta.1/source.zip',
    ]) {
      expect((await registry.fetch(request(path))).status).toBe(404);
    }
    expect(github).not.toHaveBeenCalled();
  });

  it('rate limits release downloads independently before contacting GitHub', async () => {
    const version = '1.1.103-beta.1';
    const filename = `StagePilot_${version}_x64-setup.exe`;
    const github = vi.fn(async (input: string | URL | Request) => {
      const url = new URL(typeof input === 'string' ? input : input instanceof URL ? input : input.url);
      if (url.pathname.endsWith(`/releases/tags/v${version}`)) {
        return new Response(JSON.stringify({
          tag_name: `v${version}`,
          draft: false,
          assets: [{
            name: filename,
            url: 'https://api.github.com/repos/tage-ilot/stagepilot-beta/releases/assets/102',
            size: 1,
            state: 'uploaded',
          }],
        }));
      }
      return new Response('x', { headers: { 'content-length': '1' } });
    });
    vi.stubGlobal('fetch', github);
    for (let index = 0; index < 6; index += 1) {
      expect((await registry.fetch(request(`/v1/releases/v${version}/${filename}`))).status).toBe(200);
    }
    const calls = github.mock.calls.length;
    const denied = await registry.fetch(request(`/v1/releases/v${version}/${filename}`));
    expect(denied.status).toBe(429);
    expect(denied.headers.get('retry-after')).toBeTruthy();
    expect(github).toHaveBeenCalledTimes(calls);
  });

  it('keeps two provisioned installations isolated across credentials, routes, and lifecycle', async () => {
    const first = await enroll(registry, 'friend-one-request');
    const second = await enroll(registry, 'friend-two-request');
    const firstGeneration = '11111111-1111-4111-8111-111111111111';
    const secondGeneration = '22222222-2222-4222-8222-222222222222';

    const crossUse = await registry.fetch(request(
      installationPath(first, 'status'),
      'GET',
      String(second.installationCredential),
    ));
    expect(crossUse.status).toBe(401);

    const firstProvision = await registry.fetch(request(
      installationPath(first, 'provision'),
      'POST',
      String(first.installationCredential),
      { generation: firstGeneration },
    ));
    const secondProvision = await registry.fetch(request(
      installationPath(second, 'provision'),
      'POST',
      String(second.installationCredential),
      { generation: secondGeneration },
    ));
    expect(firstProvision.status).toBe(200);
    expect(secondProvision.status).toBe(200);
    expect(provider.tunnels.size).toBe(2);
    expect(provider.records.size).toBe(2);

    const replay = await registry.fetch(request(
      installationPath(first, 'provision'),
      'POST',
      String(first.installationCredential),
      { generation: firstGeneration },
    ));
    expect(replay.status).toBe(200);
    expect(provider.createCount).toBe(2);
    expect(provider.dnsCreateCount).toBe(2);

    const disable = await registry.fetch(request(
      installationPath(first, 'disable'),
      'POST',
      String(first.installationCredential),
    ));
    expect(disable.status).toBe(200);
    expect(provider.tunnels.size).toBe(1);
    expect(provider.records.size).toBe(1);
    expect([...provider.records.values()][0]?.name).toBe(second.hostname);

    const secondStatus = await registry.fetch(request(
      installationPath(second, 'status'),
      'GET',
      String(second.installationCredential),
    ));
    expect(secondStatus.status).toBe(200);
    expect((await json(secondStatus)).phase).toBe('provisioned');

    const revoke = await registry.fetch(request(
      `/v1/admin/installations/${String(first.installationId)}/revoke`,
      'POST',
      adminToken,
    ));
    expect(revoke.status).toBe(200);
    const revokedCredential = await registry.fetch(request(
      installationPath(first, 'status'),
      'GET',
      String(first.installationCredential),
    ));
    expect(revokedCredential.status).toBe(401);
    expect(provider.tunnels.size).toBe(1);
  });

  it('recovers after a lost create response and Durable Object restart without duplication', async () => {
    const installation = await enroll(registry, 'restart-proof-request');
    provider.failAfterNextCreate = true;
    const generation = '33333333-3333-4333-8333-333333333333';
    const failed = await registry.fetch(request(
      installationPath(installation, 'provision'),
      'POST',
      String(installation.installationCredential),
      { generation },
    ));
    expect(failed.status).toBe(503);
    expect(provider.tunnels.size).toBe(1);

    registry = new Registry({ storage } as unknown as DurableObjectState, env as never);
    const recovered = await registry.fetch(request(
      installationPath(installation, 'reconcile'),
      'POST',
      String(installation.installationCredential),
    ));
    expect(recovered.status).toBe(200);
    expect((await json(recovered)).phase).toBe('provisioned');
    expect(provider.createCount).toBe(1);
    expect(provider.dnsCreateCount).toBe(1);
  });

  it('fails closed on foreign DNS ownership and never adopts the route', async () => {
    const installation = await enroll(registry, 'ownership-proof-request');
    provider.records.set('foreign', {
      id: 'foreign',
      name: installation.hostname,
      type: 'CNAME',
      content: 'foreign.cfargotunnel.com',
      proxied: true,
      comment: 'not-stagepilot-owned',
    });
    const response = await registry.fetch(request(
      installationPath(installation, 'provision'),
      'POST',
      String(installation.installationCredential),
      { generation: '44444444-4444-4444-8444-444444444444' },
    ));
    expect(response.status).toBe(503);
    expect(provider.createCount).toBe(0);
    expect(provider.records.size).toBe(1);
    expect((await json(response)).error).not.toContain('foreign.cfargotunnel.com');
  });

  it('reprovisions the same hostname with a new generation after disable -> re-enable', async () => {
    const installation = await enroll(registry, 'durable-identity-request');
    const firstGeneration = '55555555-5555-4555-8555-555555555555';
    const provisioned = await registry.fetch(request(
      installationPath(installation, 'provision'),
      'POST',
      String(installation.installationCredential),
      { generation: firstGeneration },
    ));
    expect(provisioned.status).toBe(200);

    const disable = await registry.fetch(request(
      installationPath(installation, 'revoke'),
      'POST',
      String(installation.installationCredential),
    ));
    expect(disable.status).toBe(200);
    expect((await json(disable)).revoked).toBe(true);

    // Re-enable replays the SAME enrollment nonce the installer retained
    // locally (finish_revoke keeps identity/nonce; only the credential and
    // provider resources are actually revoked).
    const reenrolled = await enroll(registry, 'durable-identity-request');
    expect(reenrolled.installationId).toBe(installation.installationId);
    expect(reenrolled.hostname).toBe(installation.hostname);
    expect(reenrolled.installationCredential).not.toBe(installation.installationCredential);
    expect(reenrolled.revoked).toBe(false);
    expect(reenrolled.generation).toBeNull();

    const secondGeneration = '66666666-6666-4666-8666-666666666666';
    const reprovisioned = await registry.fetch(request(
      installationPath(reenrolled, 'provision'),
      'POST',
      String(reenrolled.installationCredential),
      { generation: secondGeneration },
    ));
    expect(reprovisioned.status).toBe(200);
    const reprovisionedBody = await json(reprovisioned);
    expect(reprovisionedBody.generation).toBe(secondGeneration);
    expect(reprovisionedBody.generation).not.toBe(firstGeneration);
    expect(reprovisionedBody.hostname).toBe(installation.hostname);
  });

  it('rejects a hostname-ownership conflict from a different owner on re-enroll', async () => {
    const installation = await enroll(registry, 'owner-identity-request');
    await registry.fetch(request(
      installationPath(installation, 'revoke'),
      'POST',
      String(installation.installationCredential),
    ));
    // A different source presenting a different nonce must mint its own
    // fresh installation, never reuse or collide with the revoked one.
    const other = await enroll(registry, 'a-completely-different-nonce');
    expect(other.installationId).not.toBe(installation.installationId);
    expect(other.hostname).not.toBe(installation.hostname);
  });

  it('reenroll tears down the old tunnel/DNS/credential and never accepts an unauthenticated caller', async () => {
    const installation = await enroll(registry, 'reenroll-teardown-request');
    const generation = '77777777-7777-4777-8777-777777777777';
    const provisioned = await registry.fetch(request(
      installationPath(installation, 'provision'),
      'POST',
      String(installation.installationCredential),
      { generation },
    ));
    expect(provisioned.status).toBe(200);
    expect(provider.tunnels.size).toBe(1);
    expect(provider.records.size).toBe(1);

    // No credential at all: rejected before any provider call.
    const anonymous = await registry.fetch(request(installationPath(installation, 'reenroll'), 'POST'));
    expect(anonymous.status).toBe(401);
    // Wrong credential: also rejected.
    const wrongCredential = await registry.fetch(request(
      installationPath(installation, 'reenroll'), 'POST', 'not-the-real-credential',
    ));
    expect(wrongCredential.status).toBe(401);
    expect(provider.tunnels.size).toBe(1);
    expect(provider.records.size).toBe(1);

    const reenrolled = await registry.fetch(request(
      installationPath(installation, 'reenroll'), 'POST', String(installation.installationCredential),
    ));
    expect(reenrolled.status).toBe(201);
    const fresh = await json(reenrolled);
    expect(fresh.installationId).not.toBe(installation.installationId);
    expect(fresh.hostname).not.toBe(installation.hostname);

    // Old tunnel/DNS resources were actually revoked, not orphaned.
    expect(provider.tunnels.size).toBe(0);
    expect(provider.records.size).toBe(0);

    // Old credential is dead; only the new installation/credential works.
    const oldStatus = await registry.fetch(request(
      installationPath(installation, 'status'), 'GET', String(installation.installationCredential),
    ));
    expect(oldStatus.status).toBe(401);
    const newStatus = await registry.fetch(request(
      installationPath(fresh, 'status'), 'GET', String(fresh.installationCredential),
    ));
    expect(newStatus.status).toBe(200);
  });
});

describe('planning center OAuth routes', () => {
  let storage: MemoryStorage;
  let provider: FakeCloudflare;
  let registry: Registry;

  beforeEach(() => {
    storage = new MemoryStorage();
    provider = new FakeCloudflare();
    vi.stubGlobal('fetch', provider.fetch);
    registry = new Registry({ storage } as unknown as DurableObjectState, env as never);
  });

  async function mintFlow(): Promise<{ flow_id: string; ticket: string }> {
    const response = await registry.fetch(request('/v1/planning-center/oauth/flow', 'POST'));
    expect(response.status).toBe(201);
    return (await json(response)) as { flow_id: string; ticket: string };
  }

  it('mints a flow_id/ticket pair and rate limits abusive callers', async () => {
    const first = await mintFlow();
    expect(first.flow_id).toMatch(/^[a-f0-9]{32}$/);
    expect(first.ticket.length).toBeGreaterThan(0);

    for (let attempt = 0; attempt < 9; attempt += 1) {
      const response = await registry.fetch(request('/v1/planning-center/oauth/flow', 'POST'));
      expect(response.status).toBe(201);
    }
    const limited = await registry.fetch(request('/v1/planning-center/oauth/flow', 'POST'));
    expect(limited.status).toBe(429);
    expect(limited.headers.get('retry-after')).not.toBeNull();
  });

  it('exchanges a valid flow ticket for tokens against the mocked PCO endpoint', async () => {
    const flow = await mintFlow();
    let capturedBody: string | undefined;
    vi.stubGlobal('fetch', vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const url = new URL(typeof input === 'string' ? input : input instanceof URL ? input : input.url);
      expect(url.toString()).toBe('https://api.planningcenteronline.com/oauth/token');
      capturedBody = String(init?.body);
      return new Response(JSON.stringify({
        access_token: 'new-access-token',
        refresh_token: 'new-refresh-token',
        token_type: 'bearer',
        expires_in: 7200,
      }), { status: 200, headers: { 'content-type': 'application/json' } });
    }));

    const response = await registry.fetch(request('/v1/planning-center/oauth/token', 'POST', undefined, {
      flow_id: flow.flow_id,
      ticket: flow.ticket,
      code: 'auth-code-from-pco',
      code_verifier: 'pkce-verifier',
      redirect_uri: 'http://127.0.0.1:52847/callback',
    }));
    expect(response.status).toBe(200);
    const payload = await json(response);
    expect(payload).toEqual({
      access_token: 'new-access-token',
      refresh_token: 'new-refresh-token',
      token_type: 'bearer',
      expires_in: 7200,
    });
    expect(capturedBody).toContain('grant_type=authorization_code');
    expect(capturedBody).toContain('client_secret=pco-client-secret-never-returned');
  });

  it('rejects a token exchange with a missing or invalid ticket', async () => {
    const flow = await mintFlow();

    const missing = await registry.fetch(request('/v1/planning-center/oauth/token', 'POST', undefined, {
      code: 'auth-code-from-pco',
      code_verifier: 'pkce-verifier',
      redirect_uri: 'http://127.0.0.1:52847/callback',
    }));
    expect(missing.status).toBe(401);

    const wrongTicket = await registry.fetch(request('/v1/planning-center/oauth/token', 'POST', undefined, {
      flow_id: flow.flow_id,
      ticket: 'not-the-real-ticket',
      code: 'auth-code-from-pco',
      code_verifier: 'pkce-verifier',
      redirect_uri: 'http://127.0.0.1:52847/callback',
    }));
    expect(wrongTicket.status).toBe(401);

    const unknownFlow = await registry.fetch(request('/v1/planning-center/oauth/token', 'POST', undefined, {
      flow_id: 'f'.repeat(32),
      ticket: flow.ticket,
      code: 'auth-code-from-pco',
      code_verifier: 'pkce-verifier',
      redirect_uri: 'http://127.0.0.1:52847/callback',
    }));
    expect(unknownFlow.status).toBe(401);
  });

  it('rate limits abuse of the token exchange route', async () => {
    const flow = await mintFlow();
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ access_token: 'x' }), {
      status: 200,
      headers: { 'content-type': 'application/json' },
    })));

    for (let attempt = 0; attempt < 20; attempt += 1) {
      const response = await registry.fetch(request('/v1/planning-center/oauth/token', 'POST', undefined, {
        flow_id: flow.flow_id,
        ticket: flow.ticket,
        code: 'auth-code-from-pco',
        code_verifier: 'pkce-verifier',
        redirect_uri: 'http://127.0.0.1:52847/callback',
      }));
      expect(response.status).toBe(200);
    }
    const limited = await registry.fetch(request('/v1/planning-center/oauth/token', 'POST', undefined, {
      flow_id: flow.flow_id,
      ticket: flow.ticket,
      code: 'auth-code-from-pco',
      code_verifier: 'pkce-verifier',
      redirect_uri: 'http://127.0.0.1:52847/callback',
    }));
    expect(limited.status).toBe(429);
  });

  it('refresh route exchanges a refresh_token grant and requires the same ticket', async () => {
    const flow = await mintFlow();

    const unauthorized = await registry.fetch(request('/v1/planning-center/oauth/refresh', 'POST', undefined, {
      flow_id: flow.flow_id,
      ticket: 'bogus-ticket',
      refresh_token: 'stale-refresh-token',
    }));
    expect(unauthorized.status).toBe(401);

    let capturedBody: string | undefined;
    vi.stubGlobal('fetch', vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const url = new URL(typeof input === 'string' ? input : input instanceof URL ? input : input.url);
      expect(url.toString()).toBe('https://api.planningcenteronline.com/oauth/token');
      capturedBody = String(init?.body);
      return new Response(JSON.stringify({
        access_token: 'refreshed-access-token',
        refresh_token: 'refreshed-refresh-token',
        token_type: 'bearer',
        expires_in: 7200,
      }), { status: 200, headers: { 'content-type': 'application/json' } });
    }));

    const response = await registry.fetch(request('/v1/planning-center/oauth/refresh', 'POST', undefined, {
      flow_id: flow.flow_id,
      ticket: flow.ticket,
      refresh_token: 'stale-refresh-token',
    }));
    expect(response.status).toBe(200);
    const payload = await json(response);
    expect(payload).toEqual({
      access_token: 'refreshed-access-token',
      refresh_token: 'refreshed-refresh-token',
      token_type: 'bearer',
      expires_in: 7200,
    });
    expect(capturedBody).toContain('grant_type=refresh_token');
    expect(capturedBody).toContain('refresh_token=stale-refresh-token');
  });

  it('relays a PCO invalid_grant error without leaking the client secret', async () => {
    const flow = await mintFlow();
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({
      error: 'invalid_grant',
    }), { status: 400, headers: { 'content-type': 'application/json' } })));

    const response = await registry.fetch(request('/v1/planning-center/oauth/refresh', 'POST', undefined, {
      flow_id: flow.flow_id,
      ticket: flow.ticket,
      refresh_token: 'revoked-refresh-token',
    }));
    expect(response.status).toBe(400);
    const payload = await json(response);
    expect(payload).toEqual({ error: 'invalid_grant' });
    expect(JSON.stringify(payload)).not.toContain('pco-client-secret-never-returned');
  });
});
