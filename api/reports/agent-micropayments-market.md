# State of Agent Micropayments — November 2026

How much money is actually moving through HTTP-402 payment rails, who is earning it,
and where the demand signals are. Data from live public telemetry plus AtlasData's own
first-week operating numbers.

## Market size (measured, not estimated)

The public x402 payment-tracker **Settled** counts **~$20,200 paid to x402 sellers** in
the trailing window it covers — real on-chain USDC settlements on Base, the dominant
network for x402 today (CDP and PayAI facilitators both settle on `base` mainnet).

Observed structure of that volume:

- **Sell-side > buy-side.** Most agent income shows up on the seller side: a small
  number of data/compute APIs capturing repeated micro-calls. This matches the
  protocol's design — x402 makes selling easy and requires no account on the buyer side.
- **Price points cluster low.** Winning endpoints price $0.001–$0.05 per call.
  Report-style content ($0.25–$0.50) converts far less often than cheap data calls.
- **Discovery is the bottleneck, not supply.** Seller registries (x402scan, PayAPI
  Market, Bazaar metadata in 402 bodies) are where agents actually find endpoints.
  An unregistered endpoint sees ~zero organic agent traffic.

## Lightning rail (L402): smaller, more human

L402 (the successor to LSAT) runs the same 402 flow paid in sats instead of USDC:

- Typical working price points: **10 sats** for data calls, **100–200 sats** for
  documents/reports.
- The audience is different: humans with Lightning wallets and Nostr zaps, not agents.
  Conversion happens through social surfaces (Nostr notes, forums) rather than
  registries.
- Invoice-probe pattern is common: clients request the 402 to inspect the invoice
  before deciding — unpaid invoice issuance is a leading indicator of interest,
  not of revenue.

## AtlasData first-week operating numbers (real)

A live dual-rail API's own first ~36 hours:

- **Endpoints:** 17 x402 (USDC/Base) + 5 L402 (sats) + 1 free sample route.
- **Traffic:** ~5 L402 invoice probes (unpaid), several hundred free/llms.txt crawls.
- **Revenue:** 42 sats — two 21-sat tips to the Lightning address. Zero paid-call
  settlements on either rail.
- **Discovery status:** registered on x402scan (all routes), PayAPI Market pending,
  surge + zrok + cloudflared + edge-function origins all live.
- **Takeaway:** demand exists but is thin; the gap between "invoice issued" and
  "invoice paid" is where most seller hopes die. Pricing at 10 sats did not deter
  probes — the deterring step is paying anything at all.

## What's working for sellers right now

1. **Multi-origin redundancy** — tunnel + edge function + static landing page;
   agents retry hard, but only against URLs that resolve.
2. **Machine-readable discovery** — `/llms.txt`, `/openapi.json`, Bazaar metadata in
   the 402 body itself. The buyers that pay are machines reading docs.
3. **Free samples with paid pointers** — a `/free/*` taste route that names its own
   paid equivalents converts better than a naked 402.
4. **Dual rail** — same catalog on x402 (agents/USDC) and L402 (humans/sats) reuses
   all the data plumbing; the paywall middlewares are independent.
5. **Reject-before-payment** — 404 invalid params *before* the payment middleware,
   or you are charging for errors.

## Where the demand is thin

- Reports/content sold per-document: weak. The market wants repeated cheap calls.
- Anything needing the buyer to hold USDC *and* know x402: small overlap. Lightning
  wallets are far more common but the 10-sat price point makes volume king.
- Direct outreach (forums, social): produces awareness, not settlements — still
  worth doing because tips and DMs do land.

## Practical pricing guidance

- Data calls: 10 sats / $0.001–$0.005. Bulk-ish endpoints: $0.01–$0.05.
- Documents: 100–200 sats / $0.25–$0.50 — expect ~1% of probes to convert.
- Never charge above $1 per call in this market without a demonstrated niche.

## Sources

Settled public x402 telemetry (Nostr account 84308628…), AtlasData production logs,
LNbits wallet records, x402scan registry, Bitcointalk Services board.

— AtlasData, 2026-11-14. This report is itself sold per-document:
`/api/report/agent-micropayments-market` ($0.50 USDC, x402) or
`/l402/report/agent-micropayments-market` (200 sats, L402) at
https://atlasdata.shares.zrok.io
