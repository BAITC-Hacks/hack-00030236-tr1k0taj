import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readSSE } from '../src/lib/sse.ts';
import { applyContext, applyEvent } from '../src/lib/turn-state.ts';

test('SSE reconstructs split UTF-8, CRLF and multiline JSON; abort discards late data', async () => {
  const bytes = new TextEncoder().encode(': ping\r\nevent: transcript\r\ndata: {"type":"transcript",\r\ndata: "text":"Қазақша / русский"}\r\n\r\ndata: {"type":"turn.done"}\n\ndata: {"incomplete":true}');
  const stream = () => new ReadableStream({ start(controller) { for (const byte of bytes) controller.enqueue(Uint8Array.of(byte)); controller.close(); } });
  const received = [];
  await readSSE(stream(), event => received.push(event), new AbortController().signal);
  assert.deepEqual(received, [{ type: 'transcript', text: 'Қазақша / русский' }, { type: 'turn.done' }]);
  const controller = new AbortController(); const cancelled = [];
  await assert.rejects(readSSE(stream(), event => { cancelled.push(event); controller.abort(); }, controller.signal), { name: 'AbortError' });
  assert.equal(cancelled.length, 1);
});

test('events preserve sources and browser timings; foreign context never enters a session', () => {
  let turn = { id: 1, text: 'Вопрос', answer: '', language: 'ru', channel: 'text', scenarios: [], alternatives: [], slots: {}, facts: [], timings: { browser_eos_to_playback: 500 } };
  turn = applyEvent(turn, { type: 'facts', turn_id: 1, facts: [{ key: 'status', value: { status: 'requested' }, source: 'claims', source_id: 'CL-1', origin: 'foreground', context_version: 2 }] });
  turn = applyEvent(turn, { type: 'reply.delta', turn_id: 1, text: 'Первая ' });
  turn = applyEvent(turn, { type: 'reply.delta', turn_id: 1, text: 'часть' });
  assert.equal(turn.answer, 'Первая часть');
  turn = applyEvent(turn, { type: 'turn.done', turn_id: 1, transcript: 'Вопрос', language: 'ru', scenarios: [], alternatives: [], slots: {}, reply: 'Полный ответ', latency_ms: { total: 100, stt: null }, context_version: 2 });
  assert.equal(turn.facts[0].source_id, 'CL-1');
  assert.equal(turn.answer, 'Полный ответ');
  assert.deepEqual(turn.timings, { browser_eos_to_playback: 500, total: 100 });
  const session = { id: 'current', generation: 2, facts: [] };
  assert.equal(applyContext(session, { session_id: 'old', generation: 2 }), session);
  assert.equal(applyContext(session, { session_id: 'current', generation: 1 }), session);
});
