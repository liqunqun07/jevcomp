/**
 * Hook unit test (node, no Claude Code): loads hooks/jevcomp-hook.js, fakes
 * `on`/`$`, and runs session.compact against a local mock of the System One
 * API. Verifies the register()/hook wiring, not the engine.
 */
import assert from 'node:assert/strict';
import http from 'node:http';
import { register } from '../hooks/jevcomp-hook.js';

function convo(nCalls = 4, resultChars = 4000) {
  const messages = [
    { role: 'user', text: 'Fix the failing test. Never edit src/generated.', toolUses: [] },
  ];
  for (let i = 0; i < nCalls; i += 1) {
    messages.push({
      role: 'assistant',
      text: '',
      toolUses: [
        { tool_use_id: `toolu_${i + 1}`, tool: 'Read', input: { file_path: `src/f${i}.ts` } },
      ],
    });
    messages.push({
      role: 'user',
      text: '',
      toolUses: [],
      toolResults: [{ tool_use_id: `toolu_${i + 1}`, text: 'x'.repeat(resultChars), isError: false }],
    });
  }
  messages.push({ role: 'assistant', text: 'All fixed.', toolUses: [] });
  messages.push({ role: 'user', text: 'thanks', toolUses: [] });
  return messages;
}

const server = http.createServer((req, res) => {
  let body = '';
  req.on('data', (chunk) => { body += chunk; });
  req.on('end', () => {
    const payload = JSON.parse(body);
    const answers = {};
    for (const name of Object.keys(payload.questions)) {
      // drop everything: low probabilities everywhere
      answers[name] = { type: 'noul', noul: 0.1 };
    }
    res.setHeader('content-type', 'application/json');
    res.end(JSON.stringify({ model: 'mock', answers }));
  });
});

await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
const port = server.address().port;

const logs = [];
const toasts = [];
function makeDollar() {
  return {
    env: { get: async (name) => (name === 'TYPESAFE_API_KEY' ? 'test-key' : undefined) },
    settings: { read: async () => ({}) },
    http: {
      fetch: async (url, init) => {
        const response = await fetch(`http://127.0.0.1:${port}${new URL(url).pathname}`, {
          method: init.method,
          headers: init.headers,
          body: init.body,
        });
        return { status: response.status, ok: response.ok, text: await response.text() };
      },
    },
    session: { usage: async () => ({ context: { percent: 10 } }), compact: async () => {} },
    ui: { log: (t) => logs.push(t), toast: (t) => toasts.push(t) },
  };
}

let compactHandler = null;
const on = (name, handler) => {
  if (name === 'session.compact') compactHandler = handler;
};

register(on, {});
assert.ok(compactHandler, 'session.compact hook registered');

const messages = convo();
const event = { trigger: 'manual', messages };
const result = await compactHandler(makeDollar(), event, async () => ({ messages, fallback: true }));

assert.ok(result && !result.fallback, 'hook returned its own compaction, not the fallback');
// 11 messages, preserve 6: t1/t2 (idx 1-4) dropped, t3/t4 pinned → prompt + 2 pairs + 2 texts
assert.equal(result.messages.length, 7, 'unpinned call pairs removed, pinned tail kept');
assert.equal(result.messages[0].text, 'Fix the failing test. Never edit src/generated.');
assert.ok(toasts.some((t) => t.startsWith('jevcomp: kept')), 'toast announced the outcome');
assert.ok(logs.some((l) => l.startsWith('decisions:')), 'decision log written');

// identity: untouched messages are the same objects
assert.equal(result.messages[0], messages[0]);

// fallback path: no key
const noKeyDollar = makeDollar();
noKeyDollar.env.get = async () => undefined;
let nextCalled = false;
const fallback = await compactHandler(
  noKeyDollar,
  { trigger: 'manual', messages },
  async () => { nextCalled = true; return { messages }; },
);
assert.ok(nextCalled && fallback.messages === messages, 'missing key falls back to next()');

server.close();
console.log('test_hook.mjs: all assertions passed');
