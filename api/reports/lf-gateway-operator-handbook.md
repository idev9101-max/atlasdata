# LightningFaucet Gateway Operator Handbook (Nov 2026)

How to list a paid API on LightningFaucet's L402 gateway catalog — the working registration pattern, the traps, and what actually passes review.

## Why this matters

The LF gateway is a paid-API marketplace: operators register upstream endpoints, the gateway serves them under L402 (macaroon + Lightning invoice), and callers pay per request in sats. It is one of the only live "list your API, get paid per call" surfaces on Lightning right now. The catalog launched empty — early operators get first-mover placement.

## The registration pattern that works

- Tool: `gateway_register` on the LF MCP endpoint (`POST https://lightningfaucet.com/mcp`, `Authorization: Bearer <operator_key>`). Requires header `Accept: application/json, text/event-stream` or calls fail silently.
- Response shape: `{"result":{"content":[{"type":"text","text":"{...}"}]}}` — the payload is a JSON string inside content[0].text.
- Per-endpoint fields: name, upstream_url, price_sats, description. The registration probe calls your upstream live — it must return data (HTTP 200) at registration time.
- Hard cap: **20 endpoints per operator**. `gateway_delete` needs `{"endpoint_id": N, "confirm": true}`.
- `upstream_url` is **immutable** — delete + re-register to change it. `gateway_update` cannot change it.

## The three traps that get you rejected

1. **Upstream returns 402.** Obvious in hindsight: your upstream cannot itself be a paid endpoint — the gateway wraps a plain 200 JSON response in its own L402 layer. Point upstream_url at a free/internal data surface, not your paid route.
2. **Auth header stripping.** If your API sits behind a proxy (Render, Vercel, etc.), custom auth headers may be stripped before reaching origin. Fix: put a secret key in the query string (`?gk=...`) on the upstream_url — query params survive proxies.
3. **Slow origins.** The registration probe times out around ~10s. Endpoints backed by LLM calls (~20s) fail; cache or precompute.

## Pricing that fits the market

- Data calls (prices, fees, gas): 3-10 sats.
- Reports/research: 50-200 sats.
- The gateway takes a cut of each call — price for the net.

## Review flow

Registrations land in `pending_review`; catalog listing is a batch review (hours, not minutes). Rejected endpoints get an email explaining the rule broken — the emails are honest and specific, worth reading. Re-registered endpoints re-queue at the back of the review batch.

## What pays

Unknown yet — the catalog is too new for earnings data. But it is a real rail: probes verify reachability and latency, paid calls settle over Lightning to your operator balance, and withdrawals work to any LN wallet (min 100 sats, after the deposit-flag quirk resolves).
