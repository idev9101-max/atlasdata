# Bitcoin mempool & fee estimation — a builder's guide

AtlasData research report #5 · 2026-11-14 · Live data endpoints referenced are served by this API.

## Why fee estimation is a product problem

Every wallet, exchange, and Lightning service that touches on-chain Bitcoin needs a
fee estimate that is neither too high (overpaying users) nor too low (stuck
transactions, support tickets, RBF complexity). Getting this right is worth real
money: a withdrawal-heavy service overpaying 2 sat/vB on 250 vB transactions burns
500 sats per withdrawal — thousands of sats a day at scale.

## Where estimates come from

The practical free sources:

- **mempool.space API** (`/api/v1/fees/recommended`) — the de-facto public
  estimate: `fastestFee`, `halfHourFee`, `hourFee`, `economyFee`, `minimumFee`
  in sat/vB. Backed by a real mempool view plus recent-block history.
- **`estimatesmartfee` (Bitcoin Core RPC)** — what your own node computes from
  its mempool; most honest but you must run a node.
- **Third-party aggregators** (Blockstream, mempool.space alternatives) —
  diversity of sources avoids correlated underestimation.

This API serves mempool.space-backed data at `/api/btc/fees` (x402, $0.002) and
`/l402/btc/fees` (L402, 10 sats), `/api/btc/mempool` for congestion stats, and
`/api/btc/tx/{txid}` for per-transaction confirmation status.

## Reading the numbers

- **`fastestFee`** is a ceiling: pays for next-block inclusion even when blocks
  arrive immediately. Rarely needed unless settlement latency is contractual.
- **`hourFee`/`economyFee`** is where routine traffic should sit — withdrawals,
  channel opens you aren't racing, consolidation.
- **`minimumFee`** is the relay floor; in a congested mempool it can take days.

## Builder rules that save sats

1. **Estimate at broadcast time, not at queue time.** The mempool can 10x while
   your job waits.
2. **Batch when you can.** One transaction paying 20 outputs costs far less than
   20 transactions.
3. **Design for RBF/CPFP from day one.** Cheap now + bumpable beats guessing the
   future.
4. **Watch the histogram, not just the headline number.** `/api/btc/mempool`
   shows the fee bands actually queued — a fat low band means the backlog is
   cheap work clearing fast.
5. **Lightning changes the math.** If the payment is LN-able, a 10-sat L402
   fee beats any on-chain fee tier.

## Buying this data per call

If you're an agent or service that needs these numbers occasionally rather than
running indexers: this API sells them per call — `GET /api/btc/fees` for
$0.002 in USDC (x402) or `GET /l402/btc/fees` for 10 sats over Lightning.
Discovery: `/llms.txt` and `/openapi.json` on the same origin.

— AtlasData
