import { test } from 'node:test';
import assert from 'node:assert/strict';
import { voiceAdapter } from '../src/lib/api.ts';
import { applyEvent } from '../src/lib/turn-state.ts';

test('SSE trace metadata stays with its turn through cancel, playback and final events', async () => {
  const traceId = '4bf92f3577b34da6a3ce929d0e0e4736';
  const traceparent = `00-${traceId}-00f067aa0ba902b7-01`;
  const nextTraceId = 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa';
  const nextTraceparent = `00-${nextTraceId}-bbbbbbbbbbbbbbbb-01`;
  const sessionId = '11111111-1111-4111-8111-111111111111';
  const nextSessionId = '22222222-2222-4222-8222-222222222222';
  const timing = { eos_to_playback_ms: 321, eos_to_reply_text_ms: 123 };
  const done = {
    type: 'turn.done', turn_id: 1, transcript: 'Вопрос', language: 'ru',
    scenarios: [], alternatives: [], slots: {}, reply: 'Ответ',
    latency_ms: { total: 100 }, context_version: 2,
  };
  const requests = [];
  const order = [];
  let streamNumber = 0;
  let turn = {
    id: 1, text: 'Вопрос', answer: '', language: 'ru', channel: 'text',
    scenarios: [], alternatives: [], slots: {}, facts: [], timings: {},
  };
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url, init = {}) => {
    const headers = new Headers(init.headers);
    requests.push({ url: String(url), ...init, headers });
    if (String(url).endsWith('/turns/text')) {
      const first = streamNumber++ === 0;
      const events = [
        { type: 'transcript', turn_id: 1, text: 'Вопрос', language: 'ru', source: 'text' },
        done,
      ];
      return new Response(events.map(event => `event: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`).join(''), {
        headers: {
          'Content-Type': 'text/event-stream',
          'x-trace-id': first ? traceId : nextTraceId,
          traceparent: first ? traceparent : nextTraceparent,
        },
      });
    }
    if (String(url).endsWith('/cancel') || String(url).endsWith('/playback')) {
      return new Response(null, { status: 204 });
    }
    throw new Error(`Unexpected request: ${url}`);
  };

  try {
    await voiceAdapter.stream(sessionId, 'Вопрос', event => {
      order.push(event.type);
      turn = applyEvent(turn, event);
    }, new AbortController().signal, metadata => {
      order.push('headers');
      assert.deepEqual(metadata, { traceId, traceparent });
      turn = { ...turn, ...metadata };
    });
    assert.deepEqual(order, ['headers', 'transcript', 'turn.done']);
    assert.equal(turn.traceId, traceId, 'final event without trace_id preserves response headers');
    assert.equal(turn.traceparent, traceparent);

    await voiceAdapter.cancel(sessionId, 1, turn.traceparent);
    await voiceAdapter.playback(sessionId, 1, timing, turn.traceparent);
    assert.equal(requests[1].url, `/api/calls/${sessionId}/turns/1/cancel`);
    assert.equal(requests[1].method, 'POST');
    assert.equal(requests[1].headers.get('traceparent'), traceparent);
    assert.equal(requests[2].url, `/api/calls/${sessionId}/turns/1/playback`);
    assert.equal(requests[2].headers.get('traceparent'), traceparent);
    assert.equal(requests[2].headers.get('content-type'), 'application/json');
    assert.deepEqual(JSON.parse(requests[2].body), timing);

    let nextMetadata;
    await voiceAdapter.stream(nextSessionId, 'Следующий вопрос', () => {}, new AbortController().signal,
      metadata => { nextMetadata = metadata; });
    assert.equal(requests[3].url, `/api/calls/${nextSessionId}/turns/text`);
    assert.equal(requests[3].headers.get('traceparent'), null, 'new turn does not inherit the prior trace');
    assert.deepEqual(nextMetadata, { traceId: nextTraceId, traceparent: nextTraceparent });
    assert.equal(requests[0].headers.get('traceparent'), null);

    const finalized = applyEvent({ ...turn, traceId: undefined }, { ...done, trace_id: traceId });
    assert.equal(finalized.traceId, traceId, 'turn.done supplies a trace ID if headers did not');
    assert.equal(applyEvent(finalized, done).traceId, traceId);
    assert.equal(applyEvent(finalized, { ...done, trace_id: null }).traceId, traceId);
    assert.equal(finalized.traceparent, traceparent);
  } finally {
    globalThis.fetch = originalFetch;
  }
});
