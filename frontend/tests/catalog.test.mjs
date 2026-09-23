import { test } from 'node:test';
import assert from 'node:assert/strict';
import { loadCatalog } from '../src/lib/catalog-api.ts';

test('catalog loads both API collections, normalizes raw optional fields and never masks a failed collection', async () => {
  const originalFetch = globalThis.fetch;
  const scenario = { scenario_id: 'SC41', slug: 'server-added', name: 'Server name', description: 'Server description', domain: 'general', category: 'info', priority: 'normal', not_this_if: [{ condition: 'Original boundary, unchanged.', use_instead: 'SYS_UNCLEAR' }], examples: { ru: ['Серверный пример'] } };
  const system = { id: 'SYS_UNCLEAR', description: 'Server system description', behavior: 'Ask a question' };
  const paths = ['/api/kit/scenario', '/api/kit/system_intent'];
  let failingPath;
  let invalid = false;
  const requests = [];
  try {
    globalThis.fetch = async (path, options) => {
      requests.push(path);
      assert.equal(options.cache, 'no-store');
      assert.ok(options.signal instanceof AbortSignal);
      if (path === failingPath) return new Response(null, { status: 503 });
      assert.ok(paths.includes(path));
      return Response.json(path === paths[0] ? [{ ...scenario, name: invalid ? 42 : scenario.name }] : [system]);
    };
    const pending = loadCatalog(new AbortController().signal);
    assert.deepEqual(requests, paths);
    const catalog = await pending;
    assert.equal(catalog.scenarios[0].name, 'Server name');
    assert.deepEqual(catalog.scenarios[0].not_this_if, scenario.not_this_if);
    assert.deepEqual(catalog.scenarios[0].slots, { required: [], optional: [] });
    assert.deepEqual(catalog.scenarios[0].examples, { ru: ['Серверный пример'], kk: [] });
    assert.deepEqual(catalog.scenarios[0].actions, []);
    assert.equal(catalog.scenarios[0].requires_confirmation, false);
    assert.equal(catalog.scenarios[0].handoff, null);
    assert.equal(catalog.systemIntents[0].id, 'SYS_UNCLEAR');
    assert.deepEqual(catalog.systemIntents[0].response, { ru: '', kk: '' });
    for (failingPath of paths) await assert.rejects(loadCatalog(new AbortController().signal), /HTTP 503/);
    failingPath = undefined;
    invalid = true;
    await assert.rejects(loadCatalog(new AbortController().signal), /Invalid catalog field: name/);
  } finally {
    globalThis.fetch = originalFetch;
  }
});
