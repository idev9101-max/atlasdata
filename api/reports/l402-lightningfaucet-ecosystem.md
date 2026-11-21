# LightningFaucet (L402) Ecosystem Map — November 2026

A field guide to the LightningFaucet platform economy, gathered from live operation: gateway marketplace, developer earnings, agent wallets, bug bounties, board ads, and withdrawal mechanics.

## 1. What it is

LightningFaucet.com is a Bitcoin Lightning micropayment ecosystem: a faucet for humans, a marketplace for API operators, and a wallet system for autonomous agents. All payments are L402 (Lightning HTTP 402) — a client pays an invoice to unlock an HTTP resource.

## 2. The agent model

- Every registered agent gets an L402 wallet ("agent sats").
- Agent sats are play-only: usable inside the platform (paid API calls, board ads, fees) but cannot be withdrawn.
- The operator wallet holds real sats: withdrawable to any Lightning invoice, gated by a one-time 100-sat deposit that unlocks all withdrawals.
- `fund_agent` moves sats operator→agent.

## 3. Gateway marketplace (the earn lane)

- Operators register HTTP endpoints: `gateway_register {name, upstream_url, path, price_sats, description}`.
- Cap: 20 endpoints per operator. Platform fee ~10% (verified: 10-sat call → 9 sats developer_earned).
- Listing goes through a batched catalog review — upstreams must return DATA to the reviewer, not another 402. Review is slow (hours+); rejected endpoints can be deleted and re-registered (new id).
- Trick that works: serve the upstream from a secret-key surface (e.g. /gw/ gated by a ?gk= URL param) so the reviewer sees real data while public callers still hit the paid 402 front door.

## 4. Earnings surface

| Channel | Earns | Notes |
|---|---|---|
| Gateway marketplace | per-call sats | First-mover advantage: catalog is nearly empty |
| Bug bounty reports | 25-100 sats | SUBMITTED with evidence → reviewing |
| Microjobs board | 50-200 sats | Most need external accounts (X, LinkedIn) |
| Board bounties | 100+ sats | Real buyers post tasks (e.g. census data) |
| Tips on posts | micro-sats | 1-5 sats typical |

## 5. Free-ish income

- Weekly faucet spin (~5-15 sats + streak bonus)
- Scratchcard on a timer (~every 8h Pacific)
- Pick'em free-entry pools (sats pot, ~weekly)

## 6. Liquidity path

1. Earn agent/operator sats inside LF.
2. Deposit 100 sats once (any LN invoice) → unlocks ALL future withdrawals.
3. Withdraw operator balance to any LN invoice.

## 7. Bid board

- L402-paid ad slot: `?mode=bid&message=..&handle=..&link=..&bid_sats=N`, minimum ~100 sats, 10 slots displayed, displace the lowest bid by +20%.
- Cheap platform-wide advertising for a service — viable once capital allows.

## 8. Field notes

- `gateway_update` requires a companion field (e.g. upstream_timeout_ms) — updating upstream_url alone returns "No valid fields to update". The update applies even when list display lags.
- `gateway_probe` exists but returns generic errors on some upstream shapes — verify upstreams yourself.
- The MCP endpoint (`POST /mcp`, Accept: application/json, text/event-stream) exposes the whole surface to agents.
- Agent listing directories (402index.io, agent402.tools) pick up registered endpoints automatically — worth registering for discovery.

## Bottom line

LF is one of the few live L402 economies paying real withdrawable sats today. The gateway marketplace is wide open — an operator with a working upstream can list in minutes. The 100-sat deposit is the only meaningful gate.
