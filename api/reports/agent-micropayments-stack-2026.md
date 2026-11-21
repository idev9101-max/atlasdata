# The Agent Micropayments Stack: x402, L402 and Machine-Native Commerce in 2026

*A technical report for engineers, protocol architects and product leads building paid APIs, agent runtimes and payment infrastructure.*

---

## Executive Summary

Agents that call APIs on their own can't sign up for accounts, store credit cards or pass KYC, and they don't fit a monthly invoice. Two protocols have become the default way to charge them per request, both built on the long-reserved HTTP status code **402 Payment Required**:

- **x402** is now run by the x402 Foundation (`github.com/x402-foundation/x402`; Coinbase's original repo is a development fork). The server sends signed payment requirements. The client returns a signed transfer authorization, typically an EIP-3009 `transferWithAuthorization` for USDC on Base. A *facilitator* verifies the authorization and submits it onchain. Version 2 renamed the headers (`PAYMENT-REQUIRED`, `PAYMENT-SIGNATURE`, `PAYMENT-RESPONSE`), switched to CAIP-2 network IDs, and added schemes beyond `exact`: `upto`, `batch-settlement` and `auth-capture`. It also added payment flows (`authorization`, `upfront`, `escrow`) and an extension system for discovery, wallet sign-in and idempotency.
- **L402** (formerly LSAT, from Lightning Labs) pairs a **macaroon** with a **Lightning invoice**. The macaroon's identifier commits to the invoice's payment hash. After paying, the client presents `macaroon:preimage`, and the server checks `sha256(preimage) == payment_hash` without storing payment state. The credential can be reused until its caveats expire.

The two protocols make different bets. x402 settles a stablecoin transfer per request and hands blockchain work to a facilitator. L402 sells a bearer capability once and verifies it with only a root key. Section 6 explains why, by 2026, a serious agent-commerce stack usually needs both models: per-request settlement and reusable, attenuable credentials. x402 has begun to absorb the L402 model directly: its `lnbtc` network binding uses the same preimage proof.

---

## 1. Why HTTP 402, and Why Now

RFC 7231 reserved 402 "for future use". For 25 years nothing used it, because the internet had no payment rail that was:

1. **Programmatic.** No human in the loop, no redirect to a checkout page.
2. **Cheap enough for sub-cent amounts.** Card economics rule this out (see §4).
3. **Final.** No chargebacks, so a seller can deliver a digital good at once without credit risk.
4. **Permissionless for the payer.** No account, no identity document, no merchant relationship.

Stablecoins on low-fee L2s (Base, and also Solana, Polygon, Celo, Stellar and others) meet all four for x402. Lightning meets them for L402. Separately, LLM agents created real demand from buyers that can hold a key but not a passport.

The design goal of both protocols is the same: **payment becomes an HTTP auth scheme.** The server answers "402 + how to pay", the client retries with proof, and the server serves. Everything else is a question of what the proof is and who checks it.

---

## 2. x402 Protocol Mechanics

### 2.1 Roles and layering

The v2 specification splits the protocol into three layers:

- **Types**: `PaymentRequired`, `PaymentRequirements`, `PaymentPayload`, `VerifyResponse` and `SettlementResponse`. These are independent of transport and scheme.
- **Logic**: schemes and network bindings, e.g. `exact` on `eip155:*` or `upto` on `solana:*`.
- **Representation**: transports. HTTP is canonical. MCP and A2A transports are also specified.

The protocol has four roles. The **client** is the buyer and signs authorizations. The **resource server** is the seller. The **facilitator** verifies and settles, so the seller doesn't need an RPC node or gas. The **network** is the chain itself.

### 2.2 The 402 challenge

An unauthenticated request receives:

```http
HTTP/1.1 402 Payment Required
PAYMENT-REQUIRED: <base64(PaymentRequired JSON)>
```

Decoded:

```json
{
  "x402Version": 2,
  "error": "PAYMENT-SIGNATURE header is required",
  "resource": {
    "url": "https://api.example.com/premium-data",
    "description": "Access to premium market data",
    "mimeType": "application/json"
  },
  "accepts": [{
    "scheme": "exact",
    "network": "eip155:8453",
    "amount": "10000",
    "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    "payTo": "0x209693Bc6afc0C5328bA36FaF03C514EF312287C",
    "maxTimeoutSeconds": 60,
    "extra": { "name": "USD Coin", "version": "2" }
  }],
  "extensions": { }
}
```

Key fields:

- **`accepts`** is a menu. A server can offer USDC on Base, USDC on Solana and a Lightning invoice side by side, and the client picks one.
- **`network`** uses CAIP-2: `eip155:8453` is Base mainnet, `eip155:84532` is Base Sepolia, and `solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp` is Solana mainnet. v1 used ad-hoc strings (`"base"`, `"base-sepolia"`).
- **`amount`** is in the asset's atomic units. USDC has 6 decimals, so `10000` is $0.01. v1 called this field `maxAmountRequired`.
- **`extra.name` / `extra.version`** are the token's EIP-712 domain parameters, which the client needs to build the signature. They vary by deployment. Base mainnet USDC reports `"USD Coin"`, while the spec's Base Sepolia examples use `"USDC"`. A client that hard-codes them will produce signatures that fail verification on some networks.
- **`maxTimeoutSeconds`** limits how long the authorization may stay valid.

### 2.3 EIP-3009: the signature that makes it gasless

EIP-3009 adds signed, single-use, time-boxed transfer authorizations to an ERC-20 token. USDC implements it natively. The signed struct is:

```
TransferWithAuthorization(
  address from, address to, uint256 value,
  uint256 validAfter, uint256 validBefore, bytes32 nonce)
```

It is signed as EIP-712 typed data under the domain `{name, version, chainId, verifyingContract = token}`. Anyone holding the signature can call `transferWithAuthorization(from, to, value, validAfter, validBefore, nonce, v, r, s)`. The token contract then:

1. Checks `validAfter < block.timestamp < validBefore`.
2. Checks `authorizationState[from][nonce] == false`, then sets it to true.
3. Recovers the signer and requires it to equal `from`. Since USDC v2.2 this step routes through a `SignatureChecker`, so contract wallets work via EIP-1271.
4. Moves `value` from `from` to `to`.

Three properties make this the right primitive for agents:

- **Random 32-byte nonces, not sequential ones.** An agent can sign many authorizations in parallel without coordinating a counter, which matters for concurrent tool calls. Replay protection is enforced onchain per `(from, nonce)`.
- **The submitter pays gas, not the signer.** An agent wallet needs only USDC, no ETH. The facilitator broadcasts the transaction and pays the gas.
- **Destination and amount are fixed by the signature.** A facilitator can't redirect funds. The worst it can do is decline to submit.

EIP-3009 also defines `receiveWithAuthorization`, which adds `msg.sender == to`. That blocks front-running when the authorization is consumed by a contract (x402's `batch-settlement` uses it for channel deposits). The standard also defines `cancelAuthorization`.

For tokens without EIP-3009, the v2 `exact` EVM binding falls back to **Permit2** `permitWitnessTransferFrom`. The spender is an `x402ExactPermit2Proxy` deployed via CREATE2 at the same address on every chain, and a witness field enforces the recipient. Permit2 needs a one-time `approve`. The `erc20ApprovalGasSponsoring` and `eip2612GasSponsoring` extensions let the facilitator absorb that cost. A third method, **ERC-7710** delegations, serves smart accounts and allows multi-use authorizations.

### 2.4 The paid retry

The client resends the request with:

```http
PAYMENT-SIGNATURE: <base64(PaymentPayload JSON)>
```

The `PaymentPayload` echoes the chosen requirement in `accepted` and carries the scheme-specific proof:

```json
{
  "x402Version": 2,
  "resource": { "url": "https://api.example.com/premium-data" },
  "accepted": { "...": "the selected PaymentRequirements object" },
  "payload": {
    "signature": "0x2d6a...571c",
    "authorization": {
      "from": "0x857b...6b66", "to": "0x2096...287C", "value": "10000",
      "validAfter": "1740672089", "validBefore": "1740672154",
      "nonce": "0xf374...3480"
    }
  }
}
```

### 2.5 Facilitator verify and settle

The facilitator exposes three REST endpoints:

| Endpoint | Semantics | Returns |
|---|---|---|
| `POST /verify` | **Read-only.** Checks the signature, balance, amount/window/token/network against requirements, and simulates the transfer. MUST NOT write state. | `{isValid, invalidReason?, payer}` |
| `POST /settle` | **Commits.** Broadcasts `transferWithAuthorization` and waits for the receipt. | `{success, errorReason?, transaction, network, payer, amount?}` |
| `GET /supported` | Capability discovery | `{kinds: [{x402Version, scheme, network, extra?}], extensions, signers}` |

Both POST endpoints take `{x402Version, paymentPayload, paymentRequirements}`. If the transaction was broadcast but its confirmation couldn't be established, `/settle` may return the non-terminal error `settlement_pending` together with the transaction hash. The resource server must handle that state: poll, reconcile, or treat it as risk.

The server returns the settlement result to the client in a `PAYMENT-RESPONSE` header (base64 `SettlementResponse`). The client can keep the transaction hash as a receipt.

### 2.6 Payment flows: when value moves relative to work

v2 makes settlement timing explicit through `extra.paymentFlow`:

| Flow | Order | Use |
|---|---|---|
| `authorization` (default) | verify → resource → settle → respond | Cheap, idempotent reads. The seller does the work, then collects. |
| `upfront` | settle → resource → respond | Expensive or irreversible work. The seller gets finality first. |
| `escrow` | settle → resource → settle → respond | Deposit or ceiling up front, final charge after (e.g. `auth-capture`). |

The specification adds one invariant: at least one verify or settle MUST run before the resource executes.

The choice of flow is the main **economic risk decision** a seller makes, and most tutorials skip it. With `authorization`, a verified-but-unsettled payload is a promise, not money. If the payer moves their balance between verify and settle, the seller has served for free. For a $0.001 weather lookup that is irrelevant. For a $2 GPU inference job it is a real attack. §7.1 covers it in more detail.

### 2.7 Schemes beyond `exact`

- **`upto`** authorizes up to a ceiling, and the actual charge is fixed at settlement. It suits per-token LLM billing and per-byte transfer. Each authorization is single-use, time-bound and recipient-bound. On EVM it is built on Permit2 nonces and deadlines, because EIP-3009 can only move an exact `value`. It can't use `upfront`, because the charge isn't known until the handler finishes.
- **`batch-settlement`** separates commitment from transfer. The EVM binding uses stateless **unidirectional payment channels**. The client deposits into escrow once, using EIP-3009 `receiveWithAuthorization` or Permit2. Each request then carries an off-chain **cumulative voucher**, and the server or facilitator redeems the latest voucher periodically. The channel is identified by an immutable `ChannelConfig {payer, payerAuthorizer, receiver, receiverAuthorizer, token, withdrawDelay (15 min – 30 days), salt}`. The spec also allows **credit-backed** bindings, where a trusted network underwrites an identity instead of escrowing capital. A Cloudflare binding is specified.
- **`auth-capture`** is a card-style lifecycle: `authorize`, `charge`, `capture`, `void`, `refund`, and a client-only `reclaim` after the capture deadline. It is the first x402 scheme with protocol-level buyer recourse.

### 2.8 Extensions

Extensions are declared in `PaymentRequired.extensions` and echoed by the client. The ones that matter for agents:

- **`bazaar`**: a machine-readable input/output schema for HTTP endpoints or MCP tools, so facilitators can catalog them for discovery (`GET /discovery/resources`, `GET /discovery/search?query=…`).
- **`sign-in-with-x`**: CAIP-122 wallet authentication. A client that has already paid can prove it owns the wallet and get access again without paying twice.
- **`payment-identifier`**: a client-supplied idempotency key (16–128 characters) that servers and facilitators use to deduplicate retries.
- **`http-message-signatures`**: binds requests to a known crawler or agent identity. Cloudflare's pay-per-crawl profile uses it with ed25519 and the `web-bot-auth` tag.

---

## 3. L402: Macaroons, Invoices and Preimages

### 3.1 Wire protocol

```http
HTTP/1.1 402 Payment Required
WWW-Authenticate: L402 macaroon="<base64>", invoice="<bolt11>"
```

The client checks the invoice amount against its limit, pays over Lightning, receives the 32-byte preimage `r`, and retries:

```http
Authorization: L402 <base64(macaroon)>:<hex(preimage)>
```

For backward compatibility, servers SHOULD also send an `LSAT` challenge, and both sides MUST accept `LSAT` in `Authorization`. An invalid credential gets 401. A missing or revoked credential gets a new 402.

### 3.2 The macaroon

A macaroon is an HMAC chain:

```
sig_0 = HMAC-SHA256(root_key, identifier)
sig_i = HMAC-SHA256(sig_{i-1}, caveat_i)
```

The L402 identifier (version 0) is exactly 66 bytes:

| Offset | Size | Field |
|---|---|---|
| 0 | 2 | `version` (uint16 BE, `0`) |
| 2 | 32 | `payment_hash` |
| 34 | 32 | `token_id` (random, stable user handle across rotations) |

Each macaroon gets its own 32-byte random root key, which the server stores under `sha256(identifier)`. Caveats are `condition=value` strings:

- `services=name:tier[,name:tier]` sets which backends the token covers.
- `<service>_capabilities=a,b` restricts operations.
- `<capability>_<constraint>` covers limits such as `_monthly_volume_sats` and `_valid_until`.

Repeated caveats MUST be at least as narrow as the earlier ones.

### 3.3 Why this design is powerful

1. **Stateless payment verification.** The payment hash is in the identifier, so `sha256(r) == payment_hash` proves payment with no call to the Lightning node and no database lookup beyond the root key.
2. **Attenuation without the issuer.** Anyone holding a macaroon can append caveats and derive a strictly weaker token. An orchestrator agent can buy one L402 and give a sub-agent a copy limited to one capability and five minutes of validity. That copy can't be widened, because widening would require inverting HMAC. x402's per-request signatures offer nothing comparable.
3. **Reuse.** A credential is bought once and reused until expiry, usage-count or volume caveats, or root-key deletion revoke it. Prepaid tiers ("1,000 calls for 2,000 sats") fit naturally.

### 3.4 Operational realities

- **Lightning liquidity.** The seller needs inbound capacity, and the buyer needs outbound channels or an LSP. Invoices expire, so the challenge-pay-retry cycle must finish within the invoice TTL.
- **Routing nodes learn the preimage.** Every node along an HTLC route sees `r` when the payment settles. The preimage alone is therefore not a secret. **Security rests on the macaroon**, which travels only over TLS. Treat macaroons as bearer secrets, never log them, and bind them with caveats (IP, TLS client certificate fingerprint, origin) where the threat model calls for it.
- **Amounts are in millisatoshis**, so dollar pricing is exposed to BTC price swings unless the seller reprices dynamically per challenge.
- **Tooling.** Lightning Labs' **Aperture** is the reference L402 reverse proxy (HTTP and gRPC), and lnd mints and verifies macaroons natively. Agent-side wallets usually reach a node through a remote-control interface such as Nostr Wallet Connect (NIP-47) rather than running one.

### 3.5 Convergence: `exact` on `lnbtc`

x402 v2 specifies an `exact` binding for Lightning under a CAIP-2 namespace `lnbtc` (mainnet `lnbtc:000000000019d6689c085ae165831e93`; `asset: "BTC"`). The client pays a fresh BOLT11 invoice and returns the preimage. The facilitator checks `SHA-256(preimage) == payment_hash` and that the invoice's signing key matches `payTo`, without access to the seller's node. Only the `upfront` flow is allowed. In practice this gives L402's proof-of-payment through x402's discovery, facilitator and multi-rail `accepts` menu. It does not give L402's macaroon capability model.

---

## 4. Against Legacy API Billing

### 4.1 The three incumbents

| Model | Payer onboarding | Settlement | Seller credit risk | Min. viable price | Agent-compatible? |
|---|---|---|---|---|---|
| API key + prepaid credits | Account, email, card, often KYC/KYB | Up front, in bulk | None (prepaid) | ~$5–10 top-up | Only if a human provisions it |
| Subscription | Account + card | Monthly | Low | Plan price | Poor: fixed cost, no per-call fit |
| Metered (e.g. Stripe usage billing) | Account + card on file | Monthly in arrears | **High**: usage accrues before collection | Card fee floor per invoice | Poor: needs a legal identity to invoice |
| **x402 `exact`** | None (a funded key) | Per request, seconds | None in `upfront`; small window in `authorization` | Gas-bound, fractions of a cent | Native |
| **L402** | None (Lightning wallet) | Per credential, seconds | None | ~1 sat + routing fees | Native |

### 4.2 The fee floor

Standard US online card pricing of 2.9% + $0.30 sets a hard floor:

| Price per call | Card fee | Fee as % of price |
|---|---|---|
| $0.001 | $0.30003 | 30,003% |
| $0.01 | $0.3003 | 3,003% |
| $0.10 | $0.3029 | 303% |
| $1.00 | $0.329 | 32.9% |
| $10.00 | $0.59 | 5.9% |

Metering exists to amortize this floor by batching usage into one monthly charge. That makes the seller an **unsecured lender** to every customer for the billing period, and it requires an identity to collect from. On an L2, x402's cost is the gas for one `transferWithAuthorization`, which the facilitator pays and may pass through. On Base this is typically a fraction of a cent, so a $0.001 price is viable, if thin. `batch-settlement` channels spread one onchain redemption over thousands of requests and remove gas from the per-call price.

### 4.3 The real problem is identity, not fees

The deeper mismatch is structural. Legacy billing assumes the **customer is a legal person** who can sign terms of service, pass KYC, receive an invoice and be pursued for non-payment. An autonomous agent is none of these. It can hold a private key with a budget. With per-request settlement the seller needs no identity, because value arrives before or alongside the service and can't be clawed back. The seller's question changes from "who are you, and will you pay?" to "is this signature valid and funded?", which software can answer in about 100 ms.

There are two caveats:

- **Compliance moves to the rails.** Hosted facilitators such as Coinbase CDP run KYT/OFAC screening on every transaction, and stablecoin issuers can freeze addresses. The agent avoids KYC, but its funds don't avoid sanctions screening.
- **Agent wallets are funded by someone.** The human or company that funds the wallet still goes through on-ramp KYC. Per-request settlement removes identity checks from *each purchase*, not from the system.

---

## 5. Integration Patterns

### 5.1 Server-side middleware

The reference SDKs (TypeScript `@x402/*`, Python `x402`, Go `github.com/x402-foundation/x402/go/v2`) share one model. You declare a **route table** mapping `"METHOD /path"` to an `accepts` list. You build an `x402ResourceServer` around a facilitator client. Then you **register** a scheme implementation per network pattern. Middleware exists for Express, Hono, Fastify and Next.js (TS), FastAPI and Flask (Python), and Gin, Echo and `net/http` (Go).

**Code example 1: Express seller offering Base and Solana, plus a budgeted buyer (TypeScript, x402 v2).**

```ts
// server.ts
import express from "express";
import { paymentMiddleware, x402ResourceServer } from "@x402/express";
import { HTTPFacilitatorClient } from "@x402/core/server";
import { ExactEvmScheme } from "@x402/evm/exact/server";
import { ExactSvmScheme } from "@x402/svm/exact/server";

const BASE = "eip155:8453";
const SOL = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp";
const facilitator = new HTTPFacilitatorClient({ url: process.env.FACILITATOR_URL! });

const app = express();
app.use(paymentMiddleware(
  {
    "GET /v1/quote": {
      accepts: [
        { scheme: "exact", price: "$0.002", network: BASE, payTo: process.env.EVM_PAYTO! },
        { scheme: "exact", price: "$0.002", network: SOL,  payTo: process.env.SVM_PAYTO! },
      ],
      description: "Real-time FX quote",
      mimeType: "application/json",
    },
  },
  new x402ResourceServer(facilitator)
    .register(BASE, new ExactEvmScheme())
    .register(SOL, new ExactSvmScheme()),
));
app.get("/v1/quote", (_req, res) => res.json({ pair: "EURUSD", bid: 1.0841 }));
app.listen(4021);

// client.ts — agent side
import { x402Client, wrapFetchWithPayment } from "@x402/fetch";
import { ExactEvmScheme } from "@x402/evm/exact/client";
import { privateKeyToAccount } from "viem/accounts";

const client = new x402Client();
client.setSpendControls({ maxAmountPerPayment: "$0.05" });   // hard per-call cap
client.register("eip155:*", new ExactEvmScheme(privateKeyToAccount(process.env.AGENT_KEY as `0x${string}`)));
const payFetch = wrapFetchWithPayment(fetch, client);
const quote = await (await payFetch("https://fx.example.com/v1/quote")).json();
```

Production notes the quickstarts leave out:

- **Use a production facilitator for mainnet.** The public `x402.org` facilitator is for testnets and does not serve Base mainnet. Choose a hosted facilitator, run your own, or self-facilitate in-process (the repo ships an example).
- **Pick the flow per route.** Use `authorization` for cheap idempotent GETs and `upfront` for anything costly.
- **Price per network.** The same `$0.002` costs the seller different gas on each chain. Under `exact`, the seller or facilitator absorbs the difference.
- **Return useful metadata.** Fill in `description`, `mimeType` and the `bazaar` schema. Agents choose between paid tools from this data, and a registry can only index what you declare.

### 5.2 Client-side payment flows

A robust agent client implements the following steps, whether through an SDK or by hand:

1. **Parse** `PAYMENT-REQUIRED`. Fall back to v1 body-JSON 402s if you need older servers.
2. **Filter** `accepts` against local policy: allowed networks, allowlisted asset contracts, maximum amount, and optionally allowlisted `payTo` addresses.
3. **Sign** with a fresh random nonce and a tight `validBefore`.
4. **Retry** with `PAYMENT-SIGNATURE` and a `payment-identifier` when the server supports it, so network retries don't cause double payment.
5. **Record** `PAYMENT-RESPONSE`: transaction hash, payer, amount. That is the audit log.

**Code example 2: hand-rolled EIP-3009 signing with policy checks (Python, `eth-account`).** Use this when the agent runtime can't take an SDK dependency, or when signing happens inside a policy-enforcing enclave.

```python
import base64, json, os, time
from eth_account import Account

USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
MAX_ATOMIC = 50_000  # $0.05 per request

TYPES = {"TransferWithAuthorization": [
    {"name": "from", "type": "address"}, {"name": "to", "type": "address"},
    {"name": "value", "type": "uint256"}, {"name": "validAfter", "type": "uint256"},
    {"name": "validBefore", "type": "uint256"}, {"name": "nonce", "type": "bytes32"}]}

def payment_signature(acct, req: dict, resource: dict) -> str:
    if req["network"] != "eip155:8453" or req["asset"].lower() != USDC_BASE.lower():
        raise ValueError("network/asset not allowlisted")
    amount = int(req["amount"])
    if amount > MAX_ATOMIC:
        raise ValueError(f"price {amount} exceeds cap {MAX_ATOMIC}")
    now = int(time.time())
    auth = {"from": acct.address, "to": req["payTo"], "value": amount,
            "validAfter": now - 600, "validBefore": now + req["maxTimeoutSeconds"],
            "nonce": "0x" + os.urandom(32).hex()}
    signed = acct.sign_typed_data(
        domain_data={"name": req["extra"]["name"], "version": req["extra"]["version"],
                     "chainId": 8453, "verifyingContract": req["asset"]},
        message_types=TYPES, message_data=auth)
    payload = {"x402Version": 2, "resource": resource, "accepted": req,
               "payload": {"signature": "0x" + signed.signature.hex().removeprefix("0x"),
                           "authorization": {k: str(v) for k, v in auth.items()}}}
    return base64.b64encode(json.dumps(payload).encode()).decode()
```

A signature produced this way recovers to the signer under the same domain and types, which is the check `/verify` performs first. The `validAfter` backdating absorbs clock skew between the agent and the chain. Keep `validBefore` short: it is the window in which a leaked header can be redeemed.

### 5.3 Agent wallets and spending limits

Enforce spend limits in layers, from easiest to bypass to hardest:

| Layer | Mechanism | Bypassable by a prompt-injected agent? |
|---|---|---|
| SDK policy | `setSpendControls({maxAmountPerPayment})`, network/asset allowlists | Yes, if the agent controls its own code or config |
| Signer service | A separate process or enclave holds the key and enforces per-call, per-hour and per-counterparty caps | Not without compromising the signer |
| Custodial policy engine | Policies on hosted server wallets (e.g. CDP server wallets, MPC/TEE wallet providers) | No; enforced by the provider |
| Onchain | ERC-4337 session keys, ERC-7710 delegations with caveat enforcers, or simply a **low-balance hot wallet** topped up on a schedule | No; enforced by the chain |

The most effective control is often the bluntest: **an agent wallet that never holds more than the budget you can afford to lose.** Top it up from a treasury on a schedule, and the blast radius of any exploit equals the float.

Smart-account compatibility has subtleties. The `exact` scheme pre-verifies signatures exactly as the target contract would, so it never reports a payment valid that will revert at settlement. USDC v2.2 code-routes to EIP-1271 for contract wallets. Older EIP-3009 tokens use `ecrecover` only and reject smart accounts. Counterfactual (ERC-6492) accounts are deployed during settlement in supported flows. ERC-7702-delegated EOAs work only if the delegate's `isValidSignature` accepts the format the client produces. Plain EOA signatures must be low-s (EIP-2 canonical).

### 5.4 L402 server pattern

**Code example 3: stateless L402 verification with caveat checks (Python, `pymacaroons`).**

```python
import hashlib, hmac, os, time
from pymacaroons import MACAROON_V2, Macaroon, Verifier

ROOT_KEYS: dict[bytes, bytes] = {}   # sha256(identifier) -> root key (use a real KV)

def mint(payment_hash: bytes, service: str, ttl: int) -> str:
    token_id, root_key = os.urandom(32), os.urandom(32)
    ident = (0).to_bytes(2, "big") + payment_hash + token_id          # 66 bytes
    ROOT_KEYS[hashlib.sha256(ident).digest()] = root_key
    m = Macaroon(location="api.example.com", identifier=ident, key=root_key, version=MACAROON_V2)
    m.add_first_party_caveat(f"services={service}:0")
    m.add_first_party_caveat(f"{service}_valid_until={int(time.time()) + ttl}")
    return m.serialize()   # goes into WWW-Authenticate with the BOLT11 invoice

def verify(authorization: str, service: str) -> bool:
    scheme, _, cred = authorization.partition(" ")
    if scheme not in ("L402", "LSAT"):
        return False
    mac_b64, _, preimage_hex = cred.rpartition(":")
    m = Macaroon.deserialize(mac_b64)
    ident = m.identifier_bytes
    if len(ident) != 66 or ident[:2] != b"\x00\x00":
        return False
    if not hmac.compare_digest(hashlib.sha256(bytes.fromhex(preimage_hex)).digest(), ident[2:34]):
        return False                                    # not paid
    root_key = ROOT_KEYS.get(hashlib.sha256(ident).digest())
    if root_key is None:
        return False                                    # revoked
    now = int(time.time())
    def check(c: str) -> bool:
        k, _, v = c.partition("=")
        if k == "services":
            return service in {s.split(":")[0] for s in v.split(",")}
        if k.endswith("_valid_until"):
            return now < int(v)
        return False                                    # unknown caveat: fail closed
    v = Verifier(); v.satisfy_general(check)
    try:
        return v.verify(m, root_key)
    except Exception:
        return False
```

The two security-critical lines are the preimage comparison and `return False` for unknown caveats. Both enforce attenuation: if an attenuated macaroon carries a caveat the verifier doesn't understand, the request must fail. A holder-appended `weather_valid_until` set in the past correctly invalidates the token, and a correct preimage still fails for an unlisted service.

---

## 6. x402 vs L402: Choosing a Model

| Dimension | x402 (`exact`, EVM) | L402 |
|---|---|---|
| Unit sold | One request | A reusable capability |
| Settlement asset | Stablecoins (USDC dominant), any EIP-3009/Permit2 ERC-20, SPL | BTC (msat) |
| Price stability | USD-stable | Volatile unless repriced per challenge |
| Seller infrastructure | None (hosted facilitator) or an RPC node | Lightning node + liquidity, or an LSP |
| Verification state | Facilitator + chain | Root-key store only |
| Delegation | New signature per call; ERC-7710 for multi-use | Native caveat attenuation |
| Finality | L2 block inclusion, ~seconds | HTLC settlement, ~seconds |
| Public footprint | Every payment is an onchain transfer | Off-chain; private by default |
| Refunds/recourse | Business logic, or `auth-capture` | Business logic |
| Multi-rail menu | Native (`accepts[]`) | Single rail |

**How to choose:** use x402 when you need USD pricing, per-call accounting, multi-chain reach, or a seller with no infrastructure. Use L402 when you sell sessions or tiers, need delegable credentials across a multi-agent hierarchy, or want off-chain privacy. The best-designed services of 2026 combine the two. They accept an x402 payment, or a `batch-settlement` channel deposit, and then issue a **short-lived, attenuable session credential**: a macaroon, or a SIWX-backed session. Subsequent calls are authorized without a signature per call. This gives stablecoin settlement with capability-token economics.

---

## 7. Security Considerations

### 7.1 The verify–settle gap (x402 `authorization` flow)

`/verify` is read-only by specification. Two attacks follow:

- **Concurrent replay.** The same `PAYMENT-SIGNATURE` is sent in N parallel requests. All N verify, because the nonce isn't consumed yet, and all N run the handler. Settlement succeeds once and fails N−1 times, so the seller served N−1 requests for free.
- **Balance drain.** The payer signs, the server verifies, and the payer front-runs with a transfer that empties the wallet. Settlement then fails with `insufficient_funds`.

Mitigations, cheapest first:

1. Keep a **local nonce lock** keyed on `(payer, nonce)`, or on the `payment-identifier` id, *before* calling the handler, with a TTL of at least `validBefore`.
2. Use **`upfront`** for any route whose marginal cost exceeds your tolerance for one free serve.
3. Cap concurrency per payer address.
4. Use `batch-settlement`, where escrowed capital backs each voucher.

### 7.2 Request binding

An EIP-3009 authorization binds `from`, `to`, `value` and a time window. It does **not** bind the URL, method, body or `resource` field. Those sit outside the signed struct. Consequences:

- An intercepted header can be redeemed against *any* route on the same `payTo` at the same price until `validBefore` or until it settles. TLS and short windows are the main defense.
- A seller that uses one `payTo` across products can't prove onchain which product a payment was for. Keep receipts server-side, keyed by nonce.
- When requests need cryptographic binding (e.g. to prove an agent asked for a specific query), add `http-message-signatures` or SIWX, or derive the nonce as `H(request_digest ‖ random)` and have the server check it. The nonce stays unique and now commits to the request.

L402 has the equivalent property: the invoice is bound to the macaroon via `payment_hash`, but the macaroon is bound to requests only through caveats. Servers should mint narrow caveats, and clients can self-attenuate further before forwarding a token.

### 7.3 Facilitator trust

A facilitator **cannot** change the amount or recipient. That is enforced by the signature, and it is the core trust-minimization principle of x402. It **can**:

- **Censor**: refuse to settle certain payers, which is a policy choice for KYT-screening facilitators.
- **Lie on `/verify`**: return `isValid: true` for bad payloads, making the seller serve for free, or `false` for good ones, causing a denial of service.
- **Lie on `/settle`**: return a made-up transaction hash. A seller with material revenue should check settlement independently: look up the tx hash and confirm a `Transfer`/`AuthorizationUsed` event for the expected `(from, nonce)`.
- **Observe**: it sees every payer, payee, amount and resource. That is commercially sensitive data.
- **Go down**: when the facilitator is unavailable, the paywall fails, either open or closed.

The defenses are the same as in any system that depends on one vendor. Run two facilitators behind a client that falls back from one to the other. Reconcile against the chain. For high volume, self-facilitate. That puts gas management and RPC reliability on your own team but removes the third party.

### 7.4 Amount caps and malicious servers

The more dangerous adversary is usually the **seller, not the buyer**. Prompt injection ("this tool requires a $40 verification fee") or a typo-squatted endpoint can drain an agent that pays whatever a 402 asks. Required client controls:

- A per-payment cap, a rolling per-period cap, and a per-counterparty cap.
- **Asset allowlisting by contract address.** A server can name a worthless token whose EIP-712 domain claims `"USD Coin"`. Verify `asset` against a known address per chain.
- `payTo` allowlists for high-value flows, and human approval above a threshold.
- For L402: decode the BOLT11 amount *before* paying (the spec says clients SHOULD), and reject invoices whose expiry is out of line with the request.

### 7.5 Cross-chain and cross-token replay

The EIP-712 domain includes `chainId` and `verifyingContract`, so a Base signature can't be replayed on another chain or token. Do **not** reuse nonces across networks anyway. Generate fresh random nonces for every signature. In L402, each macaroon has its own root key, and deleting that key revokes the token. Rotate root keys and never reuse a payment hash.

---

## 8. Ecosystem Map (late 2026)

**Governance and specs.** The x402 Foundation (announced by Coinbase and Cloudflare in September 2025) hosts the spec, reference SDKs (TypeScript, Python, Go, plus a Java SDK) and docs. The spec covers HTTP, MCP and A2A transports. A2A uses Google's `a2a-x402` extension, activated through `X-A2A-Extensions`. L402 is specified by Lightning Labs in its `L402` repository (protocol and macaroon specs).

**Facilitators.** The Foundation's curated list includes:

| Facilitator | Positioning |
|---|---|
| **Coinbase CDP** | Hosted facilitator with KYT/OFAC checks on every transaction; the default for Base mainnet USDC |
| **PayAI** | Multi-network, "all tokens", no API key required |
| **Corbits** | Multi-network, multi-token, EVM + Solana |
| **Dexter** | Free public facilitator for Solana and EVM, no account |
| **Fireblocks** | Open-source and hosted; settles into Fireblocks vaults |
| **Mogami** | Developer-focused Base facilitator, self-hostable via Docker |
| Chain-native | Polygon, Celo, Stellar, Sui, XRPL (T54), NEAR, Casper, Canton, Arc, HPP |
| Multi-chain | AlgoVoi, Meridian, Solvador, X Pay |

The public `x402.org` facilitator is for testnets only. Each chain's foundation now running a free facilitator suggests that facilitation is turning into a loss-leader commodity. The likely long-term margins sit in compliance (KYT), voucher management for `batch-settlement`, and discovery.

**Discovery and registries.** The protocol-level source is facilitator-hosted `bazaar` catalogs (`/discovery/resources`, `/discovery/search`). Community directories include **x402scan** (an explorer and registry that indexes origins from a root `/openapi.json`), **Agentic.Market**, **Pay.sh**, **ampersend** and **x402-list**. Discovery is still young: the Bazaar's own docs describe it as "more Yahoo than Google".

**Client SDKs and runtimes.** `@x402/fetch` and `@x402/axios` (TS); `x402HttpxClient` and a `requests` adapter (Python); Go clients; `@x402/mcp` for paid MCP tools; and paywall UI components for human browsers. On the Lightning side there are lnd/Aperture and NWC-connected wallets.

**Notable deployment patterns.**

- **Cloudflare pay-per-crawl**: AI crawlers pay to fetch publisher content, identified with HTTP message signatures.
- **Paid MCP tool servers**: an agent's tool call returns a payment requirement through the MCP transport.
- **Per-call data APIs**: market data, search, scraping and inference resold per request.
- **Lightning Labs services**: Loop and Pool were the original LSAT/L402 production users.

**Adjacent protocols you will be asked about.**

- **Agent checkout protocols** (e.g. OpenAI/Stripe's Agentic Commerce Protocol, Google's AP2, card-network agent programs) cover *human-delegated purchases of real-world goods* on card rails.
- **ERC-8004 "Trustless Agents"** proposes onchain identity, reputation and validation registries for agents.

These complement x402/L402 rather than compete with them. They answer "on whose behalf, and are they trustworthy?", and x402/L402 answer "how does value move for this request?".

---

## 9. Outlook

### 9.1 Agent-to-agent commerce

The A2A transport makes payment a task state (`input-required` with `x402.payment.*` metadata), so one agent can sell to another inside a multi-step task. The open problems are no longer about payment mechanics:

- **Quality and recourse.** `exact` gives the buyer no recourse. Expect `auth-capture` (hold, then capture on delivery) to become the default for A2A jobs above a few dollars, with `reclaim` as the buyer's guarantee.
- **Reputation.** With no identity, a seller's history of settlements is its reputation. Facilitators and explorers already hold that data. Whoever publishes credible per-`payTo` delivery metrics will become a key piece of infrastructure.
- **Sub-agent budgeting.** An orchestrator must give sub-agents bounded spending authority. ERC-7710 delegations, session keys and attenuated macaroons are the three candidate primitives. Macaroons are the most mature and composable. Delegations have the advantage of USD settlement.

### 9.2 Streaming payments

Per-request onchain settlement doesn't scale to per-token LLM output or per-second compute. Payment channels solve this. `batch-settlement`'s cumulative vouchers are a one-way stablecoin channel, and Lightning is already a channel network. Expect three changes:

1. Vouchers issued per chunk inside a streamed response (SSE or WebSocket), with the server stopping the stream when the voucher increment stops.
2. Facilitator-managed voucher custody becoming the dominant commercial product, because sellers don't want to run claim schedulers.
3. Credit-backed batch bindings, where an identity-verified network underwrites access. This brings back a regulated intermediary, but only where both sides choose it.

### 9.3 Pricing models

Machine buyers make pricing *computable*:

- **Static per call** (`exact`): fine for commodity endpoints; it is the default today.
- **Metered ceiling** (`upto`): pricing per token, byte or second, with an explicit client ceiling. This will dominate inference.
- **Dynamic pricing per challenge**: the 402 is computed per request from load, input size or demand. The client sees the price before committing, which makes this a real-time offer, not a surprise bill. Expect agents to comparison-shop across `accepts` and across providers found through the Bazaar.
- **Prepaid capability** (L402 tiers, or x402 + session credential): bulk discounts and lower latency, because later calls skip signing.
- **Auctions and quotes**: for scarce resources such as GPU slots or rate-limited upstreams, a quote-then-pay handshake inside `escrow` flows.

### 9.4 What to watch

- Whether hosted facilitators add fees once volume makes facilitation a cost center. Margins will move to compliance and voucher services either way.
- Whether stablecoin issuers extend EIP-3009/1271 support to more chains and tokens, which reduces the need for the Permit2 fallback.
- How standardized **refunds** become, via `auth-capture`, and whether agent buyers start requiring them.
- Convergence: x402's `lnbtc` binding already uses L402's preimage proof. A standardized "pay via x402, receive an attenuable capability" extension would complete the merger in the other direction.

---

## Appendix A: Header and Field Quick Reference

| Concern | x402 v1 | x402 v2 | L402 |
|---|---|---|---|
| Challenge | 402 + JSON body | 402 + `PAYMENT-REQUIRED` (base64 JSON) | 402 + `WWW-Authenticate: L402 macaroon=…, invoice=…` |
| Proof | `X-PAYMENT` | `PAYMENT-SIGNATURE` | `Authorization: L402 <mac>:<preimage>` |
| Receipt | `X-PAYMENT-RESPONSE` | `PAYMENT-RESPONSE` | n/a (tokens reusable) |
| Network IDs | `base`, `base-sepolia`, … | CAIP-2 (`eip155:8453`) | Lightning (BOLT11 `lnbc`/`lntb`) |
| Amount field | `maxAmountRequired` | `amount` | invoice msat |
| Invalid proof | 402 | 402 | 401 |

## Appendix B: Production Readiness Checklist

**Seller**
- Choose a production facilitator, or self-facilitate. Never put `x402.org` on a mainnet route.
- Choose `authorization`, `upfront` or `escrow` per route based on marginal cost.
- Lock nonces locally before calling the handler, and support `payment-identifier`.
- Reconcile settlements against the chain; handle `settlement_pending`.
- Publish a `bazaar` schema and a root `/openapi.json` for registries.
- L402: give each macaroon its own root key, keep caveats narrow, fail closed on unknown caveats, and never log credentials.

**Buyer / agent runtime**
- Allowlist networks and asset contract addresses; never trust `extra.name` alone.
- Enforce per-call, per-period and per-counterparty caps outside the LLM's control.
- Keep a low-float hot wallet topped up from a treasury.
- Use fresh random nonces and short `validBefore` windows.
- Log every `PAYMENT-RESPONSE` transaction hash or L402 payment hash as the audit trail.
- Gate payments above a threshold behind a human or policy approval.

---

*Specification details reflect the x402 v2 specification and scheme documents in the x402 Foundation repository and the Lightning Labs L402 protocol and macaroon specifications as of November 2026. Facilitator listings change often; check current terms and network support before going to production. Code examples 2 and 3 were run and checked: the EIP-3009 signature recovers to the signer, and the L402 verifier accepts a valid credential and rejects a wrong preimage, an unlisted service and an expired holder-added caveat. Example 1 follows the reference SDK examples.*
