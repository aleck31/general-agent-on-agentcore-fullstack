// Google MCP server for AgentCore Runtime — the second downstream system.
//
// It exists to test one claim the ADRs make and had never exercised: that adding a system
// costs an OAuth provider and an IDP_REGISTRY entry, and no change to the router or the
// agent. Everything structural is therefore copied from mcp-servers/lark-cli deliberately —
// same custom passthrough header, same streamable-HTTP contract, same one-line inbound log
// whose `token=yes|no` is the only trustworthy evidence that a per-user token arrived.
//
// What differs is that there is no CLI to wrap: Google's APIs are called directly with the
// caller's own access token. And that token is all this server has — it holds no client
// secret and no service account, so it can reach exactly what the user consented to.
//
// Deliberately one tool. `google_whoami` proves the identity chain end to end (the address
// it returns is the consenting user's) without choosing between Drive, Gmail and Calendar,
// and without a scope that would need Google's verification review.
'use strict';

const http = require('http');
const https = require('https');

const PORT = parseInt(process.env.PORT || '8000', 10);
// Same header the agent sets for the Lark server. Node lowercases header names.
const TOKEN_HEADER = 'x-amzn-bedrock-agentcore-runtime-custom-lark-token';

const TOOLS = [
  {
    name: 'google_whoami',
    description:
      "Return the calling user's own Google account (email, name). Proves the agent is "
      + 'acting as that person against Google, not as an application.',
    inputSchema: { type: 'object', properties: {} },
    call: () => googleGet('https://www.googleapis.com/oauth2/v3/userinfo'),
  },
];

function googleGet(url) {
  // Returns a function of the token rather than taking it here, so a tool definition stays
  // free of credentials and the token only ever passes through the request that uses it.
  return (userToken) => new Promise((resolve) => {
    const req = https.request(url, {
      method: 'GET',
      headers: { Authorization: `Bearer ${userToken}`, Accept: 'application/json' },
      timeout: 15000,
    }, (res) => {
      let body = '';
      res.on('data', (c) => (body += c));
      res.on('end', () => {
        const ok = res.statusCode >= 200 && res.statusCode < 300;
        // Google's own error text is passed through: it distinguishes an expired token from
        // a missing scope, and guessing between those wastes an afternoon.
        resolve({ text: ok ? body : `google ${res.statusCode}: ${body.slice(0, 400)}`,
                  isError: !ok });
      });
    });
    req.on('timeout', () => { req.destroy(); resolve({ text: 'google request timed out', isError: true }); });
    req.on('error', (e) => resolve({ text: `google request failed: ${e.message}`, isError: true }));
    req.end();
  });
}

function sse(res, obj) {
  res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache', Connection: 'keep-alive' });
  res.end(`event: message\ndata: ${JSON.stringify(obj)}\n\n`);
}

const server = http.createServer((req, res) => {
  if (req.method === 'GET') { res.writeHead(200); res.end('ok'); return; }
  let body = '';
  req.on('data', (c) => (body += c));
  req.on('end', async () => {
    const userToken = req.headers[TOKEN_HEADER] || '';
    let mcp;
    try {
      mcp = JSON.parse(body);
    } catch {
      console.log(`inbound ${req.method} ${req.url} unparseable body (${body.length}B) `
        + `token=${userToken ? 'yes' : 'no'}`);
      res.writeHead(400); res.end('bad json'); return;
    }
    // Scheme only, never the credential. AWS4-HMAC-SHA256 means the transport signed this;
    // Bearer means a per-user token was injected — the distinction this log exists for.
    const authScheme = String(req.headers['authorization'] || '').split(' ')[0] || '(none)';
    console.log(`inbound ${req.method} ${req.url} mcp=${mcp.method || '(none)'} `
      + `token=${userToken ? 'yes' : 'no'} auth=${authScheme}`);

    if (mcp.method === 'initialize') {
      return sse(res, { jsonrpc: '2.0', id: mcp.id, result: {
        protocolVersion: '2025-11-25',
        capabilities: { tools: { listChanged: false } },
        serverInfo: { name: 'google-mcp', version: '1.0.0' },
      } });
    }
    if (mcp.method === 'notifications/initialized') { res.writeHead(202); res.end(); return; }
    if (mcp.method === 'tools/list') {
      // Listed without a token, like the Lark server: the token gates calls, not discovery,
      // so an unconsented user still gets a working session and is only asked when a tool
      // is actually reached.
      const tools = TOOLS.map((t) => ({ name: t.name, description: t.description, inputSchema: t.inputSchema }));
      return sse(res, { jsonrpc: '2.0', id: mcp.id, result: { tools } });
    }
    if (mcp.method === 'tools/call') {
      const name = mcp.params && mcp.params.name;
      const tool = TOOLS.find((t) => t.name === name);
      if (!tool) {
        return sse(res, { jsonrpc: '2.0', id: mcp.id, result: {
          content: [{ type: 'text', text: `unknown tool: ${name}` }], isError: true } });
      }
      if (!userToken) {
        return sse(res, { jsonrpc: '2.0', id: mcp.id, result: {
          content: [{ type: 'text', text: 'no user token (authorize Google first)' }], isError: true } });
      }
      const out = await tool.call(mcp.params.arguments || {})(userToken);
      return sse(res, { jsonrpc: '2.0', id: mcp.id, result: {
        content: [{ type: 'text', text: out.text }], isError: out.isError } });
    }
    return sse(res, { jsonrpc: '2.0', id: mcp.id || null,
      error: { code: -32601, message: `method not found: ${mcp.method}` } });
  });
});

server.listen(PORT, '0.0.0.0', () => {
  console.log(`google-mcp on :${PORT} (${TOOLS.length} tools) node=${process.version}`);
});
