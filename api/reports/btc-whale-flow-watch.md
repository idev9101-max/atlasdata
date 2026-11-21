# BTC Whale Flow Watch — endpoint + integration brief

Snapshot date: 2026-11-17. Source: live read of AtlasData `/api/btc/whales` (mempool.space upstream), latest confirmed block at capture time.

## What the endpoint returns

`GET /api/btc/whales?min_btc=5&pages=5` scans up to 8 pages (200 txs) of the latest confirmed Bitcoin block and returns every transaction whose combined output value meets `min_btc`, sorted descending: txid, value_btc, size_vbytes, fee_sats, output count, plus the block hash and timestamp. $0.01 per call on every rail, 10 sats on the L402 rail.

## Live read at capture

Latest block contained 28 transactions over 5 BTC. Head of the table:

| value_btc | shape |
|---|---|
| 17,558.5 | single 2-output consolidation, 223 vB |
| ~83.5 x7 | repeated same-size outputs — classic exchange batch withdrawal pattern |
| ~32 / ~16 | mid-size transfers, 20 and 8 outputs |

The 83.5-BTC repeated-value cluster is the tell-tale of a custodial batch: dozens of same-value outputs paying many users in one transaction. A single-entity 17.5k BTC consolidation in the same block is an order of magnitude above routine flow — worth flagging for any agent watching exchange reserves or large-holder movement.

## How an agent uses it

1. Poll `/api/btc/whales?min_btc=20` each new block (tip hash changes per call — compare `block` field to dedupe).
2. Batch-detection heuristic: >=3 outputs of near-identical value in one tx => custodial batch, not directional whale flow.
3. Single-input consolidations over 1,000 BTC: candidate cold-storage or exchange-rebalance events; cross-check with `/api/btc/fees` for mempool pressure.

## Integration

x402: `GET https://x402atlas.xyz/api/btc/whales?min_btc=5` -> 402 -> pay USDC on Base -> JSON. L402: `https://x402atlas.xyz/l402/btc/whales` -> pay bolt11 -> JSON. No account, no API key, deterministic output.

## Caveats

- Coverage is the latest confirmed block only (200 txs max per call); for deeper history call again after each block or use the tx-detail endpoint on hits.
- Value is summed outputs, which double-counts change; treat same-block patterns as flow shape, not net position.
