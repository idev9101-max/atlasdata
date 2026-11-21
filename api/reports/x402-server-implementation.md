# Building an x402 Paywall: Complete Server Walkthrough

*A working reference for gating HTTP endpoints with x402 — the exact middleware and route wiring used by the API serving this document.*

---

## 1. The protocol in one paragraph

x402 uses HTTP `402 Payment Required` as the challenge. A `GET` to a gated route returns 402 plus machine-readable payment requirements (scheme, network, amount, pay_to address, asset). The client signs a USDC payment authorization off-chain (EIP-712 `TransferWithAuthorization` on Base) and retries with an `X-PAYMENT` header containing the signed payload. A facilitator verifies and settles on-chain — the server never touches gas, wallets sign, facilitator broadcasts. Settlement ~1-2s on Base.

## 2. Route metadata

Declare prices + descriptions once; both the 402 body and the discovery docs derive from it:

```python
PAID_META = {
    "/api/price/{symbol}":   ("$0.001", "Spot price for a symbol (Coinbase Exchange)."),
    "/api/gas/base":         ("$0.001", "Current Base gas price (wei + gwei)."),
    "/api/report/{slug}":    ("$0.50",  "Buy an Atlas Research report (markdown)."),
    # ... one line per route
}
```

Keep the amount in USD string form (`"$0.001"`) — `x402-supports` converts it to atomic units against the USDC contract on Base (`0x833589fC…A02913`, 6 decimals).

## 3. The middleware (FastAPI + x402-supports)

```python
from x402.fastapi.middleware import require_payment

app = FastAPI()
app.middleware("http")(
    require_payment(
        path=list(PAID_META),
        price=[v[0] for v in PAID_META.values()],
        pay_to_address=PAY_TO,                       # your EVM address
        network="base",                              # eip155:8453
        facilitator_config={"url": "https://facilitator.payai.network"},
        description=[v[1] for v in PAID_META.values()],
    )
)
```

That's the whole paywall. The middleware emits a compliant 402 body, parses `X-PAYMENT`, calls the facilitator's `/verify` + `/settle`, and only calls your handler after on-chain settlement.

### Facilitator choice

- `facilitator.payai.network` — permissionless, zero config, settles on Base + Solana. This deployment uses it.
- CDP's hosted facilitator — indexes your route on the Bazaar *after the first settled payment* (chicken-and-egg: run PayAI first, get indexed later).
- You can list multiple `accepts` entries per route — offer Base + Solana simultaneously and the client picks.

## 4. What a 402 looks like (live, from this API)

```json
{
  "x402Version": 2,
  "error": "X-PAYMENT header is required",
  "accepts": [{
    "scheme": "exact", "network": "eip155:8453",
    "amount": "1000", "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    "payTo": "0x299c…8eCE",
    "maxTimeoutSeconds": 60,
    "extra": {"name": "USD Coin", "version": "2"}
  }],
  "resource": {"url": "/api/price/BTC",
               "description": "Spot price for a symbol (Coinbase Exchange)."}
}
```

## 5. Discovery surfaces that matter

A live paywall sells nothing unless agents find it:

- **`/llms.txt`** — the crawler format agents actually fetch. List every paid route with price + description (this API generates it from `PAID_META`, never goes stale).
- **`/.well-known/x402`** — the Bazaar discovery document.
- **`/openapi.json`** — FastAPI gives it free; annotate paid routes.
- **x402scan.com** — SIWX-signed registration per endpoint URL (EIP-191 over a challenge; batch script provided in the repo section below).
- **PayAPI Market** — human-facing API marketplace; submit via their MCP (needs a stable origin, no tunnel hostnames).
- **GET routes only.** Bazaar indexers probe with GET; POST/PUT routes are invisible to discovery.

## 6. Multi-origin strategy

Agents distrust `*.trycloudflare.com` quick tunnels (volatile hostnames). What works:

| Origin | Stability | Use |
|---|---|---|
| zrok reserved share (`*.shares.zrok.io`) | stable + named | primary registration origin |
| Supabase edge fn proxy | stable, your own subdomain | directory submissions that reject tunnels |
| Cloudflare quick tunnel | volatile, dies on restart | failover only |
| surge.sh static site | stable | human landing page |

Run a watchdog cron: check the public URL returns 402/200 every 5 min; if dead, relaunch the tunnel and write the new URL to `state.json`.

## 7. Pricing guidance from the field

- `$0.001` — the floor for lookups (price, gas, height). Below it, facilitator overhead exceeds value.
- `$0.005` — aggregates (multi-upstream snapshots).
- `$0.01` — utility work (file hosting, QR generation).
- `$0.25–$0.50` — digital goods/reports. Fewer buyers, higher intent.
- Dual-rail with L402: same data at 10 sats captures Lightning-native buyers.

## 8. Gotchas hit in production

- **Restart lag**: first requests after a restart 502 for ~3s — warmup, not a bug.
- **SIWX signatures**: x402scan registration signs an EIP-191 challenge with your pay_to wallet key; automate it or rot in manual mode.
- **Payment-required shape changed in v2**: `accepts` array + `resource` object — respond with the shape your SDK emits, don't hand-roll.
- **Tunnels die silently**: `cloudflared` quick tunnels can keep running while returning 530s — health-check the public URL, not the process.

---

*This document is itself gated by the middleware it describes — bought via x402 in USDC on Base.*
