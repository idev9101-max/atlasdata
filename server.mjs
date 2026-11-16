// AtlasData mirror on Netlify — emits its own 402 challenge (netlify resource.url),
// proxies signed requests to the zrok backend for settlement + content.
const UPSTREAM = "https://atlasdata.shares.zrok.io";
const PAY_TO = "0x299cb831e8580f559e58064d4F145389dB418eCE";
const USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913";

const ROUTES = [
  { re: /^\/api\/price\/([A-Za-z0-9-]+)$/, params: ["symbol"], atomic: "5000", desc: "Real-time USD spot price for any crypto asset (Coinbase data)." },
  { re: /^\/api\/ticker\/([A-Za-z0-9-]+)$/, params: ["symbol"], atomic: "10000", desc: "24h ticker: price, volume, change for any crypto asset." },
  { re: /^\/api\/gas\/base$/, params: [], atomic: "5000", desc: "Current Base mainnet gas price and latest block." },
  { re: /^\/api\/exchange\/([A-Za-z0-9-]+)$/, params: ["pair"], atomic: "10000", desc: "Exchange rates and currency conversion for a pair like BTC-ETH." },
  { re: /^\/api\/token\/(0x[0-9a-fA-F]{40})$/, params: ["address"], atomic: "10000", desc: "ERC-20 token metadata on Base for a contract address." },
  { re: /^\/api\/wallet\/(0x[0-9a-fA-F]{40})$/, params: ["address"], atomic: "10000", desc: "ETH balance and tx count for any address on Base mainnet." },
  { re: /^\/api\/tx\/base\/(0x[0-9a-fA-F]{64})$/, params: ["txhash"], atomic: "15000", desc: "Decoded transaction details for a Base mainnet transaction." },
  { re: /^\/api\/block\/base\/latest$/, params: [], atomic: "10000", desc: "Latest Base block with timestamp and gas metrics." },
  { re: /^\/api\/btc\/fees$/, params: [], atomic: "10000", desc: "Live Bitcoin mempool fee estimates (fast/half-hour/hour) plus mempool stats." },
  { re: /^\/api\/btc\/mempool$/, params: [], atomic: "15000", desc: "Current Bitcoin mempool size, fee histogram and recent block stats." },
  { re: /^\/api\/btc\/tip$/, params: [], atomic: "10000", desc: "Bitcoin tip height, hash, median fee and unconfirmed tx count." },
  { re: /^\/api\/sol\/slot$/, params: [], atomic: "10000", desc: "Current Solana slot, block height and recent performance samples." },
  { re: /^\/api\/feargreed$/, params: [], atomic: "10000", desc: "Crypto Fear & Greed index: value, classification, timestamp, history." },
  { re: /^\/api\/market\/snapshot$/, params: [], atomic: "25000", desc: "Multi-source crypto market snapshot: BTC+ETH price, gas, mempool, fear/greed." },
  { re: /^\/api\/dex\/token\/(0x[0-9a-fA-F]{40})$/, params: ["address"], atomic: "15000", desc: "DEX pairs, liquidity and price for a token address (DexScreener)." },
  { re: /^\/api\/x402\/discover$/, params: [], atomic: "30000", desc: "Search 33k+ x402 services: keyword, network, curated filters, risk flags." },
  { re: /^\/api\/x402\/pulse$/, params: [], atomic: "20000", desc: "Daily x402 market leaderboard + price/call medians from 33k listings." },
  { re: /^\/api\/x402\/recommend$/, params: [], atomic: "20000", desc: "Vendor pick for a need: scored on proven demand with risk_flags." },
  { re: /^\/api\/chain\/wallet-report\/(0x[0-9a-fA-F]{40}|[1-9A-HJ-NP-Za-km-z]{32,44})$/, params: ["address"], atomic: "150000", desc: "Multi-chain wallet risk report: balances, tx counts, holdings, scoring." },
];

const b64 = (o) => Buffer.from(JSON.stringify(o)).toString("base64");

function challenge(url, route, pathParams) {
  const pathProps = {};
  for (const p of route.params) pathProps[p] = { type: "string" };
  const inputSchema = {
    type: "object",
    properties: {
      type: { type: "string", const: "http" },
      method: { type: "string", enum: ["GET"] },
      ...(route.params.length
        ? { pathParams: { type: "object", properties: pathProps, required: route.params } }
        : {}),
    },
    required: ["type", "method"],
    additionalProperties: false,
  };
  return {
    x402Version: 2,
    error: "Payment required",
    resource: { url, description: route.desc, mimeType: "application/json" },
    accepts: [{
      scheme: "exact", network: "eip155:8453", amount: route.atomic, asset: USDC,
      payTo: PAY_TO, maxTimeoutSeconds: 300,
      extra: { name: "USD Coin", version: "2" },
    }],
    extensions: {
      bazaar: {
        info: {
          input: { type: "http", method: "GET", ...(route.params.length ? { pathParams } : {}) },
          output: { type: "json", example: { status: "ok" } },
        },
        schema: {
          $schema: "https://json-schema.org/draft/2020-12/schema",
          type: "object",
          properties: {
            input: inputSchema,
            output: { type: "object", properties: { type: { type: "string" }, example: { type: "object" } }, required: ["type"] },
          },
        },
      },
    },
  };
}


import http from "node:http";

const server = http.createServer(async (req, res) => {
  const u = new URL(req.url, "http://x");
  const path = u.pathname;
  for (const route of ROUTES) {
    const m = path.match(route.re);
    if (!m) continue;
    const vals = {};
    route.params.forEach((p, i) => (vals[p] = m[i + 1]));
    const sig = req.headers["payment-signature"];
    const origin = "https://" + (req.headers.host || "atlasdata.onrender.com");
    if (!sig) {
      const body = JSON.stringify({ error: "payment required" });
      res.writeHead(402, { "content-type": "application/json", "payment-required": b64(challenge(origin + path + u.search, route, vals)) });
      return res.end(body);
    }
    try {
      const r = await fetch(UPSTREAM + path + u.search, { headers: { "payment-signature": sig } });
      const body = Buffer.from(await r.arrayBuffer());
      const h = { "content-type": r.headers.get("content-type") || "application/json" };
      if (r.headers.get("payment-response")) h["payment-response"] = r.headers.get("payment-response");
      res.writeHead(r.status, h);
      return res.end(body);
    } catch (e) {
      res.writeHead(502, { "content-type": "application/json" });
      return res.end(JSON.stringify({ error: "upstream unreachable" }));
    }
  }
  if (path === "/" || path === "/api") {
    res.writeHead(200, { "content-type": "application/json" });
    return res.end(JSON.stringify({ service: "AtlasData", upstream: UPSTREAM, endpoints: ROUTES.map(r => ({ pattern: r.re.source, price: r.atomic, desc: r.desc })) }));
  }
  if (path === "/healthz") { res.writeHead(200); return res.end("ok"); }
  res.writeHead(404, { "content-type": "application/json" });
  res.end(JSON.stringify({ error: "not found", endpoints: ROUTES.length }));
});

const PORT = process.env.PORT || 10000;
server.listen(PORT, () => console.log("atlasdata render mirror on :" + PORT));
