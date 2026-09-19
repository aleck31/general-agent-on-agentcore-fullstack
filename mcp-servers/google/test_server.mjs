// Google MCP server tests. Node's own runner — this server has no dependencies and adding
// a toolchain for one file would be the wrong trade.
//
// What is worth asserting is not that Google answers (that is Google's job) but the two
// things this server could get wrong on its own: calling Google without the caller's token,
// and listing tools only to users who already consented.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';

const PORT = 8731;

async function withServer(fn) {
  const proc = spawn(process.execPath, [new URL('./server.js', import.meta.url).pathname],
    { env: { ...process.env, PORT: String(PORT) }, stdio: ['ignore', 'pipe', 'pipe'] });
  try {
    await once(proc.stdout, 'data');          // the listen line
    await fn();
  } finally {
    proc.kill();
  }
}

const post = (body, headers = {}) => fetch(`http://127.0.0.1:${PORT}/mcp`, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json', ...headers },
  body: JSON.stringify(body),
});

const frame = async (res) => {
  const text = await res.text();
  return JSON.parse(text.slice(text.indexOf('data: ') + 6));
};

test('tools are listed without a token, so an unconsented user still gets a session',
  async () => {
    await withServer(async () => {
      const out = await frame(await post({ jsonrpc: '2.0', id: 1, method: 'tools/list' }));
      const names = out.result.tools.map((t) => t.name);
      assert.deepEqual(names, ['google_whoami']);
    });
  });

test('a call without the user token is refused rather than made as the application',
  async () => {
    await withServer(async () => {
      const out = await frame(await post({
        jsonrpc: '2.0', id: 2, method: 'tools/call',
        params: { name: 'google_whoami', arguments: {} },
      }));
      assert.equal(out.result.isError, true);
      assert.match(out.result.content[0].text, /no user token/);
    });
  });

test('an unknown tool is an error, not a silent success', async () => {
  await withServer(async () => {
    const out = await frame(await post({
      jsonrpc: '2.0', id: 3, method: 'tools/call',
      params: { name: 'google_delete_everything', arguments: {} },
    }, { 'x-amzn-bedrock-agentcore-runtime-custom-lark-token': 'tok' }));
    assert.equal(out.result.isError, true);
    assert.match(out.result.content[0].text, /unknown tool/);
  });
});

test('the server holds no credential of its own', async () => {
  const src = await (await import('node:fs/promises')).readFile(
    new URL('./server.js', import.meta.url), 'utf8');
  // No client secret, no service account, no refresh: the only thing it ever acts with is
  // the token that arrived with the request.
  for (const forbidden of ['CLIENT_SECRET', 'client_secret', 'service_account', 'refresh_token']) {
    assert.ok(!src.includes(forbidden), `server.js must not mention ${forbidden}`);
  }
});
