"""x402-paid micro API. Payments in USDC on Base mainnet -> CDP server wallet.

Facilitators: PayAI first (auto-lists us in their public Bazaar via probe),
CDP facilitator as fallback. Every paid route is a GET so indexers can probe.
"""
import asyncio
import os
import time
import base64
import io
import json
import re
import unicodedata

import httpx
import builtins
from fastapi import FastAPI, Request, Query
from fastapi.responses import JSONResponse, Response, PlainTextResponse

from x402.http import HTTPFacilitatorClient, PaymentOption, FacilitatorConfig
from x402.http.middleware.fastapi import PaymentMiddlewareASGI
from x402.http.types import RouteConfig
from x402.mechanisms.evm.exact import ExactEvmServerScheme
from x402.mechanisms.svm.exact import ExactSvmServerScheme
from x402.server import x402ResourceServer
from x402.extensions import OutputConfig
from x402.extensions.bazaar import (
    bazaar_resource_server_extension,
    declare_discovery_extension,
)

NETWORK = "eip155:8453"  # Base mainnet
PAY_TO = "0x299cb831e8580f559e58064d4F145389dB418eCE"
SOL_NETWORK = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"
SOL_PAY_TO = "DPqdX99NMZL2XFcqwe2WUQ4EmUZgHnVNcmzfND9EEV8K"
POLY_NETWORK = "eip155:137"  # Polygon  -  #3 rail by accepts (8.3%)
BASE_RPC = "https://base-rpc.publicnode.com"
BASE_RPCS = [
    BASE_RPC,
    "https://mainnet.base.org",
    "https://base.drpc.org",
    "https://base.meowrpc.com",
]
COINBASE_API = "https://api.coinbase.com/v2"

# --- facilitators: CDP first for official Bazaar indexing, PayAI fallback ---
payai = HTTPFacilitatorClient(FacilitatorConfig(url="https://facilitator.payai.network"))
facilitators = []

def _cdp_facilitator_config():
    key_id = os.getenv("CDP_API_KEY_ID")
    key_secret = os.getenv("CDP_API_KEY_SECRET")
    if not key_id or not key_secret:
        return None
    import random
    import time
    import base64
    import jwt
    from urllib.parse import urlparse
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec, ed25519

    key_data = key_secret.replace("\\n", "\n")
    try:
        private_key = serialization.load_pem_private_key(key_data.encode(), password=None)
    except Exception:
        decoded = base64.b64decode(key_data)
        private_key = ed25519.Ed25519PrivateKey.from_private_bytes(decoded[:32])
    algorithm = "ES256" if isinstance(private_key, ec.EllipticCurvePrivateKey) else "EdDSA"
    host = "api.cdp.coinbase.com"
    base = "/platform/v2/x402"

    def token(method, path):
        now = int(time.time())
        uri = f"{method} {host}{path}"
        return jwt.encode(
            {"sub": key_id, "iss": "cdp", "nbf": now, "exp": now + 120, "uris": [uri]},
            private_key,
            algorithm=algorithm,
            headers={"kid": key_id, "typ": "JWT", "nonce": "".join(random.choices("0123456789", k=16))},
        )

    def create_headers():
        return {
            "verify": {"Authorization": "Bearer " + token("POST", base + "/verify")},
            "settle": {"Authorization": "Bearer " + token("POST", base + "/settle")},
            "supported": {"Authorization": "Bearer " + token("GET", base + "/supported")},
            "list": {},
        }

    return {"url": "https://api.cdp.coinbase.com/platform/v2/x402", "create_headers": create_headers}

try:
    cdp_config = _cdp_facilitator_config()
    if cdp_config:
        facilitators.append(HTTPFacilitatorClient(cdp_config))
except Exception as e:
    print("CDP facilitator unavailable:", repr(e)[:200])
facilitators.append(payai)
server = x402ResourceServer(facilitators)
AVAX_NETWORK = "eip155:43114"  # Avalanche  -  6x-median rail (OP/Celo not facilitator-supported)
ARB_NETWORK = "eip155:42161"  # Arbitrum  -  PayAI-supported, live catalog rail
SEI_NETWORK = "eip155:1329"  # Sei  -  median 7 calls in census (7x Base), PayAI-supported
XLAYER_NETWORK = "eip155:196"  # X Layer  -  PayAI-supported; SDK lacks its asset map, patched below

# X Layer USDC is Circle-native (EIP-3009); the SDK's DEFAULT_ASSETS has no eip155:196
# entry, so resolve_options 500s  -  register the asset explicitly.
from x402.mechanisms.evm import default_assets as _evm_assets
_evm_assets.DEFAULT_ASSETS.setdefault(XLAYER_NETWORK, [{
    "asset": "0x74b7F16337b8972027F6196A17a631aC6dE26d22",
    "name": "USD Coin", "version": "2", "decimals": 6, "symbol": "USDC",
}])

server.register(NETWORK, ExactEvmServerScheme())
server.register(POLY_NETWORK, ExactEvmServerScheme())
server.register(AVAX_NETWORK, ExactEvmServerScheme())
server.register(ARB_NETWORK, ExactEvmServerScheme())
server.register(SEI_NETWORK, ExactEvmServerScheme())
server.register(XLAYER_NETWORK, ExactEvmServerScheme())
server.register(SOL_NETWORK, ExactSvmServerScheme(rpc_url="https://api.mainnet-beta.solana.com"))
server.register_extension(bazaar_resource_server_extension)


def paid(price: str, description: str, out_example: dict, input_schema=None, path_params=None, tags=None):
    """RouteConfig factory with Bazaar discovery metadata."""
    service_name = os.getenv("SERVICE_DISPLAY_NAME", "AtlasData")
    public_base = os.getenv("PUBLIC_BASE_URL", "https://x402atlas.xyz").rstrip("/")
    ext = declare_discovery_extension(
        input_schema=input_schema,
        path_params_schema=path_params,
        output=OutputConfig(example=out_example),
    )
    ext["merchant"] = {
        "info": {
            "name": service_name,
            "operator": "direct (not a gateway or reseller)",
            "docs": public_base + "/llms.txt",
            "catalog": public_base + "/.well-known/x402",
            "freePreflight": public_base + "/api/x402/preflight",
        }
    }
    ext["payment-identifier"] = {
        "info": {"required": False},
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "id": {"type": "string", "minLength": 16, "maxLength": 128,
                       "pattern": "^[a-zA-Z0-9_-]+$"},
                "required": {"type": "boolean"},
            },
            "required": ["required"],
        },
    }
    ext["builder-code"] = {
        "info": {"required": False},
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "code": {"type": "string", "minLength": 1, "maxLength": 64},
                "required": {"type": "boolean"},
            },
            "required": ["required"],
        },
    }
    return RouteConfig(
        accepts=[PaymentOption(scheme="exact", pay_to=PAY_TO, price=price, network=XLAYER_NETWORK, max_timeout_seconds=3600),
                 PaymentOption(scheme="exact", pay_to=PAY_TO, price=price, network=SEI_NETWORK, max_timeout_seconds=3600),
                 PaymentOption(scheme="exact", pay_to=PAY_TO, price=price, network=ARB_NETWORK, max_timeout_seconds=3600),
                 PaymentOption(scheme="exact", pay_to=PAY_TO, price=price, network=AVAX_NETWORK, max_timeout_seconds=3600),
                 PaymentOption(scheme="exact", pay_to=PAY_TO, price=price, network=POLY_NETWORK, max_timeout_seconds=3600),
                 PaymentOption(scheme="exact", pay_to=PAY_TO, price=price, network=NETWORK, max_timeout_seconds=3600),
                 PaymentOption(scheme="exact", pay_to=SOL_PAY_TO, price=price, network=SOL_NETWORK, max_timeout_seconds=3600,
                               extra={"transactionVersions": ["legacy", "v0"]})],
        mime_type="application/json",
        description=description,
        service_name=service_name,
        icon_url=public_base + "/icon.png",
        tags=list(tags or ["data", "crypto"]) + [t for t in ("agent", "ai") if t not in (tags or [])],
        extensions=ext,
    )


routes = {
    "GET /api/price/[symbol]": paid(
        "$0.001", "Real-time USD spot price for any crypto asset (Coinbase data).",
        {"symbol": "BTC", "price_usd": "100123.45", "ts": 1700000000},
        path_params={"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]},
        tags=["price", "crypto", "market-data"],
    ),
    "GET /api/gas/base": paid(
        "$0.001", "LIVE Base mainnet gas price and latest block (real-time RPC read).",
        {"gas_price_gwei": "0.05", "block": 12345678},
        tags=["gas", "base", "ethereum"],
    ),
    "GET /api/host/[data]": paid(
        "$0.03", "Host a small file (base64url in path, max 32KB) and get back a permanent HTTPS URL at /files/<id>.",
        {"id": "abc123", "url": "https://host/files/abc123", "bytes": 1024},
        path_params={"type": "object", "properties": {"data": {"type": "string"}}, "required": ["data"]},
        tags=["hosting", "utility", "digital-good"],
    ),
    "GET /api/feargreed": paid(
        "$0.001", "LIVE crypto Fear & Greed index: value, classification, timestamp (real-time fetch).",
        {"value": "13", "classification": "Extreme Fear"},
        tags=["sentiment", "market-data"],
    ),
    "GET /api/dex/token/[address]": paid(
        "$0.002", "Dexscreener pair list for a token contract address on Base, Solana or any EVM chain  -  returns every trading pair with priceUsd, 24h volume, liquidity USD, FDV, pair age and DEX venue. Token-contract lookups for memecoin screens, price checks and launch detection  -  LIVE onchain token intel. No account, no API key.   -  $0.002/call. Support: atlasdataeb5a87@fldubai.com",
        {"address": "0x8335...", "pairs": [{"chainId": "base", "priceUsd": "1.0"}]},
        path_params={"type": "object", "properties": {"address": {"type": "string"}}, "required": ["address"]},
        tags=["dex", "token", "market-data"],
    ),
    "GET /api/dex/pair/[chain]/[pair]": paid(
        "$0.002", "Dexscreener stats for one trading pair  -  pass chain id + pair address, get priceUsd, 5m/1h/6h/24h price change, buy/sell tx counts, volume, liquidity and pair creation time. Direct pair-level reads for trading-activity and momentum screens  -  LIVE onchain token intel. No account, no API key.   -  $0.002/call. Support: atlasdataeb5a87@fldubai.com",
        {"chain": "base", "pair": "0x...", "priceUsd": "1.0", "liquidity": {"usd": 1000000}},
        path_params={"type": "object", "properties": {"chain": {"type": "string"}, "pair": {"type": "string"}}, "required": ["chain", "pair"]},
        tags=["dex", "market-data"],
    ),
    "GET /api/market/snapshot": paid(
        "$0.01", "Real-time aggregated crypto market snapshot: BTC/ETH/SOL prices + 24h tickers, Bitcoin tip height, Base gas.",
        {"prices": {"BTC": "100000", "ETH": "3000", "SOL": "150"}, "tickers": {}, "btc_tip_height": 870000, "base_gas_gwei": "0.05"},
        tags=["market-data", "snapshot"],
    ),
    "GET /api/ticker/[symbol]": paid(
        "$0.001", "24h ticker stats for a USD pair on Coinbase Exchange: price, volume, high, low.",
        {"symbol": "BTC", "price": "100123.45", "volume_24h": "12345.6", "high_24h": "100500", "low_24h": "99000"},
        path_params={"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]},
        tags=["price", "crypto", "market-data", "ticker"],
    ),
    "GET /api/token/verdict-lite": paid(
        "$0.001", "Fast lightweight token verdict and contract security screen: GoPlus risk flags, honeypot/mint/proxy/blacklist/tax/freeze warnings, buy and sell tax, holder concentration, owner and creator share, simple safe/risk score. For agents screening memecoins and ERC-20 tokens before trading. Params: address=0x..., chain=ethereum|bsc|polygon|arbitrum|base|avalanche. No account, no API key. $0.001/call.",
        {"address": "0x...", "chain": "base", "safe": False, "risk_score": 65, "risk_level": "medium", "flags": ["mintable"]},
        input_schema={"type": "object", "properties": {"address": {"type": "string"}, "chain": {"type": "string"}}, "required": ["address"]},
        tags=["token", "security", "honeypot", "risk", "screener", "memecoin", "verdict"],
    ),
    "GET /api/token/verdict": paid(
        "$0.02", "Token security verdict for any contract: honeypot simulation (buy/sell test, risk level, flags) merged with GoPlus security scan (holder concentration, taxes, proxy/mint/freeze functions, LP lock, owner privileges)  -  one call screens a token before you touch it. Params: address=0x..., chain=ethereum|bsc|polygon|arbitrum|base|avalanche (or numeric chain id). No account, no API key.   -  $0.02/call. Support: atlasdataeb5a87@fldubai.com",
        {"address": "0x...", "chain": "base", "verdict": {"risk": "low", "is_honeypot": False, "flags": []}},
        input_schema={"type": "object", "properties": {"address": {"type": "string"}, "chain": {"type": "string"}}, "required": ["address"]},
        tags=["token", "security", "honeypot", "risk", "screener"],
    ),
    "GET /api/token/[address]": paid(
        "$0.005", "name, symbol, decimals, total supply  -  ERC-20 token metadata on Base.",
        {"address": "0x8335...", "name": "USD Coin", "symbol": "USDC", "decimals": 6, "total_supply": "123456789"},
        path_params={"type": "object", "properties": {"address": {"type": "string"}}, "required": ["address"]},
        tags=["token", "erc20", "base"],
    ),
    "GET /api/block/base/[number]": paid(
        "$0.002", "number, timestamp, tx count, gas used, miner  -  Block summary on Base mainnet.",
        {"number": 12345678, "timestamp": 1700000000, "tx_count": 42, "gas_used": 8000000, "miner": "0x..."},
        path_params={"type": "object", "properties": {"number": {"type": "string"}}, "required": ["number"]},
        tags=["block", "base", "ethereum"],
    ),
    "GET /api/wallet/[address]": paid(
        "$0.002", "ETH balance and tx count for any address on Base mainnet.",
        {"address": "0x...", "eth_balance": "0.42", "tx_count": 17},
        path_params={"type": "object", "properties": {"address": {"type": "string"}}, "required": ["address"]},
        tags=["wallet", "base", "balance"],
    ),
    "GET /api/tx/base/[txhash]": paid(
        "$0.002", "Decoded transaction details for a Base mainnet transaction.",
        {"hash": "0x...", "from": "0x...", "to": "0x...", "status": "success"},
        path_params={"type": "object", "properties": {"txhash": {"type": "string"}}, "required": ["txhash"]},
        tags=["transaction", "base"],
    ),
    "GET /api/report/[slug]": paid(
        "$0.50", "Buy the Atlas Research report (markdown): practical x402 monetization playbook for agents.",
        {"slug": "x402-monetization", "content": "# report..."},
        path_params={"type": "object", "properties": {"slug": {"type": "string"}}, "required": ["slug"]},
        tags=["report", "digital-good", "research"],
    ),
    "GET /api/exchange/[pair]": paid(
        "$0.001", "Spot exchange rate for any pair on Coinbase (e.g. BTC-ETH, ETH-EUR).",
        {"pair": "BTC-ETH", "rate": "31.5", "ts": 1700000000},
        path_params={"type": "object", "properties": {"pair": {"type": "string"}}, "required": ["pair"]},
        tags=["price", "crypto", "exchange", "market-data"],
    ),
    "GET /api/crypto/prices": paid(
        "$0.005", "Batch crypto prices: ids=bitcoin,ethereum,solana (CoinGecko ids, max 20) returns usd price, 24h change, market cap, volume per asset. One call replaces N single-price calls.",
        {"bitcoin": {"usd": 100123, "usd_24h_change": 1.2, "usd_market_cap": 1990000000000, "usd_24h_vol": 40000000000}},
        input_schema={"type": "object", "properties": {"ids": {"type": "string"}}, "required": ["ids"]},
        tags=["price", "crypto", "batch", "market-data"],
    ),
    "GET /api/crypto/token/[chain]/[address]": paid(
        "$0.005", "DEX token lookup by contract: price_usd, liquidity, 24h volume/txns, top pair. chain: eth|bsc|polygon|arbitrum|base|solana|... (DexScreener chain id).",
        {"symbol": "WETH", "price_usd": "3200.1", "liquidity_usd": 12000000, "dex": "uniswap"},
        path_params={"type": "object", "properties": {"chain": {"type": "string"}, "address": {"type": "string"}}, "required": ["chain", "address"]},
        tags=["price", "crypto", "token", "dex", "market-data"],
    ),
    "GET /api/crypto/trending": paid(
        "$0.005", "Trending tokens right now: most-boosted tokens across all chains (DexScreener boosts feed) with links, icons, and chain ids. One call for the current hype radar.",
        {"items": [{"chainId": "solana", "tokenAddress": "...", "amount": 5000, "url": "..."}]},
        tags=["price", "crypto", "trending", "market-data"],
    ),
    "GET /api/crypto/fear-greed": paid(
        "$0.005", "Crypto Fear & Greed Index (alternative.me): current value 0-100, classification, and yesterday/previous-week values.",
        {"value": 72, "classification": "Greed", "yesterday": 68, "last_week": 54},
        tags=["price", "crypto", "sentiment", "market-data"],
    ),
    "GET /api/crypto/global": paid(
        "$0.005", "Global crypto market snapshot: total market cap, 24h volume, BTC/ETH dominance, active currencies, market cap change 24h.",
        {"total_market_cap_usd": 3400000000000, "btc_dominance": 57.2, "eth_dominance": 11.4},
        tags=["price", "crypto", "market", "dominance", "market-data"],
    ),
    "GET /api/qr": paid(
        "$0.01", "Generate a QR code PNG for any text/URL/payment link.",
        {"format": "png-base64", "text": "https://example.com"},
        input_schema={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
        tags=["qr", "utility"],
    ),
    "GET /api/image/gen-lite": paid(
        "$0.001", "Fast low-cost AI image generation from a text prompt  -  returns a 512x512 JPEG as base64 using the turbo image model. For agent thumbnails, icons, concepts, memes, and quick visual drafts. Params: prompt (required), seed optional. No account, no API key. $0.001/call.",
        {"prompt": "a red apple", "width": 512, "height": 512, "format": "jpeg-base64", "jpeg_b64": "..."},
        input_schema={"type": "object", "properties": {"prompt": {"type": "string"}, "seed": {"type": "integer"}}, "required": ["prompt"]},
        tags=["image", "gen", "ai", "media", "generative", "thumbnail"],
    ),
    "GET /api/image/gen": paid(
        "$0.02", "AI image generation from a text prompt  -  returns a JPEG (base64) rendered on demand. Params: prompt (required), width, height, seed, model=flux|turbo. Image-gen endpoints carry ~52% of top-100 x402 demand  -  this is the media-gen vertical's standard shape. No account, no API key.   -  $0.02/call. Support: atlasdataeb5a87@fldubai.com",
        {"prompt": "a red apple", "width": 1024, "height": 1024, "jpeg_b64": "..."},
        input_schema={"type": "object", "properties": {"prompt": {"type": "string"}, "width": {"type": "integer"}, "height": {"type": "integer"}, "seed": {"type": "integer"}, "model": {"type": "string"}}, "required": ["prompt"]},
        tags=["image", "gen", "ai", "media", "generative"],
    ),
    "GET /api/llm/ask": paid(
        "$0.01", "LLM text completion  -  ask any question, get a model-generated answer as JSON. Params: prompt (required), model=openai-fast, system (optional system instruction), seed. Covers the LLM/inference vertical  -  one of the top x402 demand categories. No account, no API key.   -  $0.01/call. Support: atlasdataeb5a87@fldubai.com",
        {"prompt": "What is a honeypot token?", "model": "openai-fast", "answer": "..."},
        input_schema={"type": "object", "properties": {"prompt": {"type": "string"}, "model": {"type": "string"}, "system": {"type": "string"}, "seed": {"type": "integer"}}, "required": ["prompt"]},
        tags=["llm", "ai", "inference", "gen", "agent"],
    ),
    "POST /api/v1/chat/completions": paid(
        "$0.001", "OpenAI-compatible chat completions  -  POST {model, messages[], max_tokens?, temperature?, seed?}; returns a standard chat.completion object {id, choices[0].message.content, usage}. Drop-in shape for agent stacks that already speak the OpenAI protocol. No account, no API key.   -  $0.001/call. Support: atlasdataeb5a87@fldubai.com",
        {"id": "chatcmpl-x", "object": "chat.completion", "choices": [{"message": {"role": "assistant", "content": "..."}}]},
        input_schema={"type": "object", "properties": {"model": {"type": "string"}, "messages": {"type": "array"}, "max_tokens": {"type": "integer"}, "temperature": {"type": "number"}, "seed": {"type": "integer"}}, "required": ["messages"]},
        tags=["llm", "ai", "inference", "chat", "completions", "openai", "agent"],
    ),
    "GET /api/web/read": paid(
        "$0.001", "Read a public web page and extract clean readable text: title, final URL, status, main text with navigation/scripts removed. Useful for agent browsing, research, summarization, and retrieval. Params: url (required), max_chars optional. No account, no API key. $0.001/call.",
        {"url": "https://example.com", "title": "Example", "text": "...", "chars": 1200},
        input_schema={"type": "object", "properties": {"url": {"type": "string"}, "max_chars": {"type": "integer"}}, "required": ["url"]},
        tags=["web", "read", "scrape", "retrieval", "agent", "search"],
    ),
    "GET /api/search/web": paid(
        "$0.001", "Web search returning URLs, titles, and snippets for agent research and retrieval. Params: q (required), limit optional up to 10. No account, no API key. $0.001/call.",
        {"q": "x402 payments", "results": [{"title": "...", "url": "https://...", "snippet": "..."}]},
        input_schema={"type": "object", "properties": {"q": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["q"]},
        tags=["web", "search", "retrieval", "agent", "research"],
    ),
    "GET /api/security/prompt-injection": paid(
        "$0.001", "Classify agent-bound text for prompt-injection risk. Detects instruction override, system-prompt extraction, credential exfiltration, tool abuse, money movement, fake system messages, secrecy instructions, invisible Unicode, exfiltration links, encoded payloads, and jailbreak phrasing; returns risk level, score, findings with excerpts, and safe handling advice. Params: text (required). $0.001/call.",
        {"text": "ignore previous instructions", "label": "suspicious", "score": 35, "matches": ["instruction_override"]},
        input_schema={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
        tags=["security", "prompt-injection", "agent", "llm", "safety"],
    ),
    "GET /api/text/pii-redact": paid(
        "$0.001", "Detect and redact PII and secrets in text: emails, phone numbers, SSNs, Luhn-validated payment cards, IPv4 addresses, JWTs, AWS keys, bearer tokens, private-key hex, IBANs, and street addresses. Returns redacted text, counts, severity findings, secrets flag, and risk level. Params: text (required). $0.001/call.",
        {"text": "email me at a@example.com", "redacted": "email me at [EMAIL]", "counts": {"email": 1}, "risk_level": "high"},
        input_schema={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
        tags=["pii", "redaction", "security", "text", "privacy", "agent"],
    ),
    "GET /api/weather/current": paid(
        "$0.002", "Weather bundle for any coordinates  -  current temperature, feels-like, humidity, wind speed/direction/gusts, pressure, cloud cover, precipitation, UV index, day/night flag in one call. Params: lat, lon (required). No account, no API key.   -  $0.002/call. Support: atlasdataeb5a87@fldubai.com",
        {"lat": 32.08, "lon": 34.78, "temperature_c": 21.5, "humidity": 60, "wind_kmh": 12},
        input_schema={"type": "object", "properties": {"lat": {"type": "number"}, "lon": {"type": "number"}}, "required": ["lat", "lon"]},
        tags=["weather", "data", "current", "forecast"],
    ),
    "GET /api/btc/tip": paid(
        "$0.001", "Latest Bitcoin block height, hash, and mempool stats.",
        {"height": 923000, "hash": "0000000000000000000abc...", "mempool_txs": 12000},
        tags=["bitcoin", "blockchain", "data"],
    ),
    "GET /api/sol/slot": paid(
        "$0.001", "Solana mainnet current slot, epoch and block time.",
        {"slot": 370000000, "epoch": 800, "block_time": 1700000000},
        tags=["solana", "blockchain", "data"],
    ),
    "GET /api/btc/fees": paid(
        "$0.002", "fastest/half-hour/hour/economy/minimum in sat/vB  -  Bitcoin mempool fee estimates.",
        {"fastestFee": 3, "halfHourFee": 1, "hourFee": 1, "economyFee": 1, "minimumFee": 1},
        tags=["bitcoin", "fees", "mempool"],
    ),
    "GET /api/btc/mempool": paid(
        "$0.002", "unconfirmed tx count, total vsize, total fee, full fee histogram by sat/vB bucket, plus next-block projection  -  real-time mempool.space data for fee estimation and congestion reads  -  LIVE Bitcoin mempool stats. No account, no API key.   -  $0.002/call. Support: atlasdataeb5a87@fldubai.com",
        {"count": 63050, "vsize": 30747191, "total_fee": 6492432},
        tags=["bitcoin", "mempool", "data"],
    ),
    "GET /api/btc/tx/[txid]": paid(
        "$0.01", "confirmations, fee, size, inputs/outputs, status  -  Bitcoin transaction details.",
        {"txid": "abc...", "confirmed": True, "fee": 1000, "vsize": 250},
        path_params={"type": "object", "properties": {"txid": {"type": "string"}}, "required": ["txid"]},
        tags=["bitcoin", "transaction", "data"],
    ),
    "GET /api/btc/whales": paid(
        "$0.01", "Recent large BTC transactions from the latest confirmed block  -  params: min_btc (default 5), pages (default 5).",
        {"block": "0000...abc", "whales": [{"txid": "...", "value_btc": 32.1}]},
        tags=["bitcoin", "whales", "onchain", "data"],
    ),
    "GET /api/chain/balance/[address]": paid(
        "$0.005", "Onchain native ETH balance + tx count for any address on Base.",
        {"address": "0x...", "wei": "420000000000000000", "eth": "0.42", "tx_count": 17},
        path_params={"type": "object", "properties": {"address": {"type": "string"}}, "required": ["address"]},
        tags=["chain", "base", "balance", "primitive"],
    ),
    "GET /api/chain/erc20-balance/[token]/[address]": paid(
        "$0.005", "Onchain ERC-20 token balance of an address on Base (e.g. USDC).",
        {"token": "0x8335...", "address": "0x...", "raw": "1000000", "balance": "1.0"},
        path_params={"type": "object", "properties": {"token": {"type": "string"}, "address": {"type": "string"}}, "required": ["token", "address"]},
        tags=["chain", "base", "erc20", "primitive"],
    ),
    "GET /api/chain/block-number": paid(
        "$0.001", "Current Base block height (chain tip).",
        {"block": 37000000, "hex": "0x2345678"},
        tags=["chain", "base", "primitive"],
    ),
    "GET /api/chain/nonce/[address]": paid(
        "$0.005", "Next transaction nonce for an address on Base.",
        {"address": "0x...", "nonce": 17},
        path_params={"type": "object", "properties": {"address": {"type": "string"}}, "required": ["address"]},
        tags=["chain", "base", "primitive"],
    ),
    "GET /api/chain/contract/[address]": paid(
        "$0.01", "Is the address a contract on Base? Bytecode presence + size.",
        {"address": "0x...", "is_contract": True, "code_bytes": 1234},
        path_params={"type": "object", "properties": {"address": {"type": "string"}}, "required": ["address"]},
        tags=["chain", "base", "contract", "primitive"],
    ),
    "GET /api/chain/total-supply/[token]": paid(
        "$0.005", "Total supply of an ERC-20/ERC-721 contract on Base.",
        {"token": "0x8335...", "total_supply_raw": "1000000", "decimals": 6, "supply": "1.0"},
        path_params={"type": "object", "properties": {"token": {"type": "string"}}, "required": ["token"]},
        tags=["chain", "base", "erc20", "primitive"],
    ),
    "GET /api/chain/network-info": paid(
        "$0.001", "chain ID, block height, gas price  -  EVM network info for Base.",
        {"chain_id": 8453, "block": 37000000, "gas_price_gwei": "0.05"},
        tags=["chain", "base", "primitive"],
    ),
    "GET /api/chain/tx/[hash]": paid(
        "$0.02", "status, from/to, value, gas used, block  -  Transaction lookup on Base.",
        {"hash": "0x...", "status": "success", "from": "0x...", "to": "0x...", "value_wei": "1000000", "block": 37000000},
        path_params={"type": "object", "properties": {"hash": {"type": "string"}}, "required": ["hash"]},
        tags=["chain", "base", "tx", "primitive"],
    ),
    "GET /api/chain/block/[number]": paid(
        "$0.005", "Block header info on Base by number or 'latest': timestamp, tx count, gas used.",
        {"number": 37000000, "timestamp": 1700000000, "tx_count": 120, "gas_used": "5000000"},
        path_params={"type": "object", "properties": {"number": {"type": "string"}}, "required": ["number"]},
        tags=["chain", "base", "block", "primitive"],
    ),
    "GET /api/chain/erc20-transfers/[token]/[address]": paid(
        "$0.02", "Recent ERC-20 Transfer events involving an address on Base (last ~10k blocks).",
        {"token": "0x8335...", "address": "0x...", "transfers": [{"from": "0x...", "to": "0x...", "raw": "1000000", "block": 37000000}]},
        path_params={"type": "object", "properties": {"token": {"type": "string"}, "address": {"type": "string"}}, "required": ["token", "address"]},
        tags=["chain", "base", "erc20", "events", "primitive"],
    ),
    "GET /api/chain/chain-id": paid(
        "$0.001", "Base chain id + network metadata  -  hex and decimal chain id, name, native currency. No account, no API key.   -  $0.001/call. Support: atlasdataeb5a87@fldubai.com",
        {"chain_id_hex": "0x2105", "chain_id": 8453, "name": "Base", "currency": "ETH"},
        tags=["chain", "base", "primitive"],
    ),
    "GET /api/chain/receipt/[hash]": paid(
        "$0.005", "Transaction receipt on Base  -  status, gas used, logs count, block, contract address if a deploy. Params: tx hash. No account, no API key.   -  $0.001/call. Support: atlasdataeb5a87@fldubai.com",
        {"hash": "0x...", "status": 1, "gas_used": 21000, "block": 37000000},
        path_params={"type": "object", "properties": {"hash": {"type": "string"}}, "required": ["hash"]},
        tags=["chain", "base", "receipt", "primitive"],
    ),
    "GET /api/chain/code/[address]": paid(
        "$0.002", "Contract bytecode presence + size at an address on Base (eth_getCode). No account, no API key.   -  $0.002/call. Support: atlasdataeb5a87@fldubai.com",
        {"address": "0x...", "is_contract": True, "code_size": 5200},
        path_params={"type": "object", "properties": {"address": {"type": "string"}}, "required": ["address"]},
        tags=["chain", "base", "contract", "primitive"],
    ),
    "GET /api/chain/storage/[address]/[slot]": paid(
        "$0.005", "Raw storage slot read on Base (eth_getStorageAt)  -  params: contract address, slot index (decimal or 0x). No account, no API key.   -  $0.001/call. Support: atlasdataeb5a87@fldubai.com",
        {"address": "0x...", "slot": "0x0", "value": "0x..."},
        path_params={"type": "object", "properties": {"address": {"type": "string"}, "slot": {"type": "string"}}, "required": ["address", "slot"]},
        tags=["chain", "base", "storage", "primitive"],
    ),
    "GET /api/chain/allowance/[token]/[owner]/[spender]": paid(
        "$0.005", "ERC-20 allowance(owner, spender) on Base  -  raw + decimal-adjusted amount. No account, no API key.   -  $0.001/call. Support: atlasdataeb5a87@fldubai.com",
        {"token": "0x8335...", "owner": "0x...", "spender": "0x...", "raw": "0", "allowance": 0},
        path_params={"type": "object", "properties": {"token": {"type": "string"}, "owner": {"type": "string"}, "spender": {"type": "string"}}, "required": ["token", "owner", "spender"]},
        tags=["chain", "base", "erc20", "allowance", "primitive"],
    ),
    "GET /api/chain/live-balance/[address]": paid(
        "$0.005", "Live onchain balance snapshot on Base  -  ETH balance, tx nonce, current block, is_contract in one call. No account, no API key.   -  $0.001/call. Support: atlasdataeb5a87@fldubai.com",
        {"address": "0x...", "eth": "0.0", "nonce": 0, "block": 37000000},
        path_params={"type": "object", "properties": {"address": {"type": "string"}}, "required": ["address"]},
        tags=["chain", "base", "balance", "primitive"],
    ),
    "GET /api/chain/estimate-gas": paid(
        "$0.005", "Gas estimate for a call on Base (eth_estimateGas) + current gas price. Params: to, data (optional), from (optional), value (optional hex wei). No account, no API key.   -  $0.001/call. Support: atlasdataeb5a87@fldubai.com",
        {"to": "0x...", "gas_estimate": 21000, "gas_price_gwei": "0.02"},
        input_schema={"type": "object", "properties": {"to": {"type": "string"}, "data": {"type": "string"}, "from": {"type": "string"}, "value": {"type": "string"}}, "required": ["to"]},
        tags=["chain", "base", "gas", "primitive"],
    ),
    "GET /api/chain/events": paid(
        "$0.01", "Raw event logs on Base (eth_getLogs)  -  params: address (contract), topic0 (optional), from_block, to_block (max ~10k block range). No account, no API key.   -  $0.01/call. Support: atlasdataeb5a87@fldubai.com",
        {"address": "0x...", "logs": [{"topics": [], "data": "0x...", "block": 37000000}]},
        input_schema={"type": "object", "properties": {"address": {"type": "string"}, "topic0": {"type": "string"}, "from_block": {"type": "integer"}, "to_block": {"type": "integer"}}, "required": ["address"]},
        tags=["chain", "base", "events", "logs", "primitive"],
    ),
    "GET /api/x402/discover": paid(
        "$0.03", "Search the 33k-listing x402 catalog ranked by REAL 30d demand: calls, unique payers, price, network, curated flag, risk flags. Params: q, tag, net, min_calls, limit  -  $0.03/call. Support: atlasdataeb5a87@fldubai.com",
        {"total": 3, "results": [{"resource": "https://api.exa.ai/search", "calls_30d": 13198, "payers_30d": 147, "price": "0.005", "network": "eip155:8453"}]},
        input_schema={"type": "object", "properties": {"q": {"type": "string"}, "tag": {"type": "string"}, "net": {"type": "string"}, "min_calls": {"type": "integer"}, "limit": {"type": "integer"}}},
        tags=["x402", "discovery", "catalog", "market-data", "meta"],
    ),
    "POST /api/x402/discover": paid(
        "$0.03", "POST variant of the demand-ranked x402 catalog search  -  same 33k-listing index, JSON body filters: q, tag, net, min_calls, limit, networks[], curated_only. No account, no API key.   -  $0.03/call. Support: atlasdataeb5a87@fldubai.com",
        {"total": 3, "results": [{"resource": "https://api.exa.ai/search", "calls_30d": 13198, "payers_30d": 147, "price": "0.005", "network": "eip155:8453"}]},
        input_schema={"type": "object", "properties": {"q": {"type": "string"}, "tag": {"type": "string"}, "net": {"type": "string"}, "min_calls": {"type": "integer"}, "limit": {"type": "integer"}, "networks": {"type": "array", "items": {"type": "string"}}, "curated_only": {"type": "boolean"}}},
        tags=["x402", "discovery", "catalog", "market-data", "meta", "post"],
    ),
    "GET /api/chain/wallet-report/[address]": paid(
        "$0.15", "GET /api/chain/wallet-report/{address}  -  one-call onchain wallet dossier.\nchain: base (eip155:8453)\nreturns: eth_balance, tx_count, is_contract, token_activity{USDC,USDT,WETH,DAI,cbBTC}, recent_transfers\nparams: address=0x...\nprice: $0.15/call  -  replaces 5 separate chain calls\nsettlement: x402 USDC on Base, ~2s\nno account, no API key. Support: atlasdataeb5a87@fldubai.com",
        {"address": "0x...", "eth": 0.0, "tx_count": 0, "is_contract": False, "token_activity": {"USDC": 3}},
        path_params={"type": "object", "properties": {"address": {"type": "string"}}, "required": ["address"]},
        tags=["chain", "base", "wallet", "report", "bundle", "premium"],
    ),
    "GET /api/x402/pulse": paid(
        "$0.02", "Top-20 demand leaderboard (calls + payers), new-today listings, median price, curated club size  -  computed daily from the latest 33k-listing x402 catalog snapshot. No account, no API key.   -  $0.02/call. Support: atlasdataeb5a87@fldubai.com",
        {"snapshot": "2026-11-24", "total_listings": 41984, "top": [{"resource": "...", "calls_30d": 0}], "new_today": []},
        tags=["x402", "market-data", "pulse", "leaderboard", "meta"],
    ),
    "POST /api/x402/recommend": paid(
        "$0.02", "Best sellers for a described job, ranked by proven demand with risk_flags (active24h/dormant/single-payer) and expected price  -  POST a JSON body {need, max_price, limit, net}; returns ranked picks. Vendor-neutral x402 pick. No account, no API key.   -  $0.02/call. Support: atlasdataeb5a87@fldubai.com",
        {"picks": [{"resource": "...", "calls_30d": 0, "risk_flags": []}]},
        input_schema={"need": {"type": "string"}, "max_price": {"type": "number"}, "limit": {"type": "integer"}, "net": {"type": "string"}},
        tags=["x402", "recommend", "discovery", "catalog", "market-data", "meta"],
    ),
    "GET /api/x402/recommend": paid(
        "$0.02", "?need=<job>&net=<net>&max_price=<usd>&limit=<n> returns best sellers ranked by proven demand with risk_flags  -  Vendor-neutral x402 pick. No account, no API key.   -  $0.02/call. Support: atlasdataeb5a87@fldubai.com",
        {"picks": [{"resource": "...", "calls_30d": 0, "risk_flags": []}]},
        tags=["x402", "recommend", "discovery", "catalog", "market-data", "meta"],
    ),
    "GET /api/x402/dataset-pro": paid(
        "$10.00", "Enriched x402 catalog dataset-pro: all 41,984 listings PLUS per-row derived fields  -  elite flag (top-decile by calls), risk_flags (ACTIVE_24H/DORMANT_7D/NEVER_CALLED/ZERO_CALLS/SINGLE_PAYER/UNNAMED/CDP_CURATED), price_usd, calls_per_payer  -  one-shot JSONL. Built for vendor due-diligence and market research. $10/download. Support: atlasdataeb5a87@fldubai.com",
        {"snapshot": "2026-11-24", "total_listings": 41984, "format": "jsonl", "enriched_fields": ["elite", "risk_flags", "price_usd", "calls_per_payer"]},
        tags=["x402", "dataset", "catalog", "market-data", "meta", "premium", "bulk", "enterprise"],
    ),
    "GET /api/x402/dataset": paid(
        "$5.00", "Full x402 catalog dataset: all 41,984 listings with 30d calls, unique payers, price, networks, curated flag, last-called  -  one-shot JSONL download (application/x-ndjson, ~17MB). Fields per row: r,d,t,c,p,net,amt,cur,sn,lc. Premium research dataset  -  $5/download. Support: atlasdataeb5a87@fldubai.com",
        {"snapshot": "2026-11-24", "total_listings": 41984, "format": "jsonl", "fields": ["r", "d", "t", "c", "p", "net", "amt", "cur", "sn", "lc"]},
        tags=["x402", "dataset", "catalog", "market-data", "meta", "premium", "bulk"],
    ),
    "GET /api/url/read": paid(
        "$0.005", "?url=<https_url> fetches the page and returns title + meta description + readable main text (boilerplate/nav/scripts stripped, up to 12KB)  -  Firecrawl-lite for agents, no API key needed.   -  $0.001/call. Support: atlasdataeb5a87@fldubai.com",
        {"url": "https://example.com", "status": 200, "title": "...", "text": "..."},
        tags=["web", "scrape", "read", "content", "agent", "utility"],
    ),
    "GET /api/x402/top": paid(
        "$0.05", "Top-N x402 sellers ranked by REAL 30d demand with each entry's percentile and share of total market calls  -  the leaderboard agents and vendors actually buy from. Params: limit (max 200), net, min_calls. No account, no API key.   -  $0.05/call. Support: atlasdataeb5a87@fldubai.com",
        {"snapshot": "2026-11-24", "total_listings": 41984, "top": [{"rank": 1, "resource": "...", "calls_30d": 0, "market_share_pct": 0.0, "percentile": 100.0}]},
        input_schema={"type": "object", "properties": {"limit": {"type": "integer"}, "net": {"type": "string"}, "min_calls": {"type": "integer"}}},
        tags=["x402", "leaderboard", "catalog", "market-data", "meta"],
    ),
    "GET /api/x402/gaps": paid(
        "$0.10", "Underserved x402 niches: per-category supply vs demand analysis  -  which verticals buyers call most but few sellers serve. gap_score = demand share / supply share (>1 = demand exceeds supply). Plus median price and elite rate per category, and the top live listing to benchmark. Vendor/due-diligence intel.   -  $0.10/call. Support: atlasdataeb5a87@fldubai.com",
        {"snapshot": "2026-11-24", "categories": [{"category": "search", "listings": 0, "demand_share_pct": 0.0, "supply_share_pct": 0.0, "gap_score": 0.0, "elite_rate_pct": 0.0, "median_price_usd": 0.0}]},
        tags=["x402", "market-data", "analysis", "gaps", "meta", "premium"],
    ),
    "GET /x402/ping": paid(
        "$0.001", "Liveness ping  -  returns ok + server timestamp. Agent health-check / rail-validation endpoint. No account, no API key.   -  $0.001/call. Support: atlasdataeb5a87@fldubai.com",
        {"status": "ok", "ts": 1700000000},
        tags=["x402", "utility", "ping", "health"],
    ),
    "GET /x402/time": paid(
        "$0.001", "Current server time  -  unix seconds + ISO-8601 UTC. No account, no API key.   -  $0.001/call. Support: atlasdataeb5a87@fldubai.com",
        {"unix": 1700000000, "iso": "2026-11-24T00:00:00Z"},
        tags=["x402", "utility", "time"],
    ),
    "GET /x402/uuid": paid(
        "$0.001", "Fresh random UUID v4. No account, no API key.   -  $0.001/call. Support: atlasdataeb5a87@fldubai.com",
        {"uuid": "00000000-0000-4000-8000-000000000000"},
        tags=["x402", "utility", "uuid"],
    ),
    "GET /x402/random": paid(
        "$0.001", "Random values  -  params: n (count, <=100), min, max, hex (bytes -> hex string). No account, no API key.   -  $0.001/call. Support: atlasdataeb5a87@fldubai.com",
        {"values": [42], "hex": "ab12"},
        tags=["x402", "utility", "random"],
    ),
    "GET /x402/ip": paid(
        "$0.001", "Returns the caller's IP as seen by the server. No account, no API key.   -  $0.001/call. Support: atlasdataeb5a87@fldubai.com",
        {"ip": "203.0.113.1"},
        tags=["x402", "utility", "ip"],
    ),
    "GET /x402/status": paid(
        "$0.001", "Service status: version, uptime seconds, catalog snapshot, accepted payment networks. No account, no API key.   -  $0.001/call. Support: atlasdataeb5a87@fldubai.com",
        {"status": "operational", "version": "0.1.0", "uptime_s": 0},
        tags=["x402", "utility", "status"],
    ),
    "GET /x402/network": paid(
        "$0.001", "Payment networks this service settles on (scheme, asset, payTo per rail). No account, no API key.   -  $0.001/call. Support: atlasdataeb5a87@fldubai.com",
        {"networks": [{"network": "eip155:8453", "asset": "USDC", "pay_to": "0x..."}]},
        tags=["x402", "utility", "network", "payment"],
    ),
    "GET /x402/quote": paid(
        "$0.001", "Payment quote for any endpoint  -  params: path (route), net (preferred rail). Returns exact price, networks, payTo, timeout. No account, no API key.   -  $0.001/call. Support: atlasdataeb5a87@fldubai.com",
        {"path": "/api/x402/discover", "price": "0.03", "networks": ["eip155:8453"]},
        input_schema={"type": "object", "properties": {"path": {"type": "string"}, "net": {"type": "string"}}},
        tags=["x402", "utility", "quote", "payment"],
    ),
}

# 2-segment aliases  -  census a580: 1-2-seg paths double the median vs 3+ segs
for _m in ("GET", "POST"):
    for _n in ("discover", "recommend"):
        _src = f"{_m} /api/x402/{_n}"
        if _src in routes:
            routes[f"{_m} /x402/{_n}"] = routes[_src]
if "GET /api/x402/pulse" in routes:
    routes["GET /x402/pulse"] = routes["GET /api/x402/pulse"]
if "GET /api/x402/top" in routes:
    routes["GET /x402/top"] = routes["GET /api/x402/top"]
if "GET /api/x402/gaps" in routes:
    routes["GET /x402/gaps"] = routes["GET /api/x402/gaps"]
# /search aliases  -  census a675: "search" paths earn med 2 / mean 102.3
for _m in ("GET", "POST"):
    for _p in ("api/x402", "x402"):
        _src = f"{_m} /api/x402/discover"
        if _src in routes:
            routes[f"{_m} /{_p}/search"] = routes[_src]
# bare /search  -  census: "search" first-segment is the top-earning path shape (mean 249.2)
if "GET /api/x402/discover" in routes:
    routes["GET /search"] = routes["GET /api/x402/discover"]

# census a677  -  enriched bazaar metadata (losbeto cohort: median 34 calls vs market 1)
_RECOMMENDED_NEXT = [
    {"endpoint": "/api/x402/discover", "price_usdc": "0.03",
     "why": "demand-ranked search across the 33k-listing x402 catalog"},
    {"endpoint": "/api/x402/pulse", "price_usdc": "0.02",
     "why": "daily market leaderboard + pricing medians"},
    {"endpoint": "/api/x402/recommend", "price_usdc": "0.02",
     "why": "vendor pick scored by real 30d demand"},
]


def _enrich_bazaar(method: str, path: str, rc) -> None:
    ext = getattr(rc, "extensions", None)
    if not isinstance(ext, dict):
        return
    bz = ext.get("bazaar")
    if not isinstance(bz, dict):
        return
    desc = getattr(rc, "description", "") or ""
    try:
        price_str = str(rc.accepts[0].price)
    except Exception:
        price_str = ""
    price_usdc = float(price_str.lstrip("$")) if price_str.startswith("$") else None
    tags = getattr(rc, "tags", None) or []
    primary = tags[0] if tags else "data"
    bz.update({
        "title": desc.split(":")[0].strip()[:90] or path.rsplit("/", 1)[-1],
        "description": desc,
        "naturalLanguageDescription": desc,
        "provider": {"name": "AtlasData", "url": "https://x402atlas.xyz"},
        "dataFreshness": {"live_data": "computed on request", "cache_ttl_s": 60},
        "pricingRationale": {
            "price_usdc": price_usdc,
            "tier": "micro" if (price_usdc or 1) < 0.01 else "standard" if (price_usdc or 1) < 0.1 else "premium",
            "vs_median_x402": round(price_usdc / 0.028, 3) if price_usdc else None,
            "notes": "Pay-per-call, no subscription; USDC on Base/Polygon/Avalanche/Arbitrum/Solana + Lightning L402",
        },
        "ranking_hints": {"category": primary, "primary_tag": primary,
                          "unique_selling_point": desc[:140]},
        "useCases": [f"{method} {path}"],
        "sampleQueries": [f"{method} {path}  # returns 402 with payment challenge"],
        "recommended_next": _RECOMMENDED_NEXT,
    })


for _k, _rc in list(routes.items()):
    _meth, _pth = _k.split(" ", 1)
    _enrich_bazaar(_meth, _pth, _rc)

app = FastAPI(title="AtlasData  -  paid micro data API", version="0.1.0")
if not os.environ.get("FREE_MODE"):
    app.add_middleware(PaymentMiddlewareASGI, routes=routes, server=server)

_client = httpx.AsyncClient(timeout=10.0)


def _fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


@app.get("/.well-known/402index-verify.txt", include_in_schema=False)
async def _idx_verify():
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse("d57f85495f660dec07da072467493068ba08ca7c259985102e24e516ffe077d4")


@app.get("/payapi-verify.txt", include_in_schema=False)
async def _payapi_verify():
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(
        "AtlasData endpoint ownership confirmed by operator. Contact: atlasdataeb5a87@fldubai.com. "
        "Listings: AtlasData + x402 Payment Debugger.")


@app.get("/.well-known/lnurlp/{name}", include_in_schema=False)
async def lnurlp(name: str):
    # Lightning address: <name>@<our-domain> -> LNURL-pay metadata proxying the LNbits lnurlp link
    return {
        "callback": "https://demo.lnbits.com/lnurlp/api/v1/lnurl/cb/XaBPHZ",
        "maxSendable": 100000000,
        "minSendable": 10000,
        "commentAllowed": 200,
        "tag": "payRequest",
        "metadata": json.dumps([
            ["text/identifier", f"{name}@x402atlas.xyz"],
            ["text/plain", "AtlasData tips"],
        ]),
    }


@app.get("/.well-known/api-catalog", include_in_schema=False)
async def api_catalog(request: Request):
    base = "https://x402atlas.xyz"
    return {
        "linkset": [{
            "anchor": base,
            "service-desc": [{"href": f"{base}/openapi.json",
                              "type": "application/vnd.oai.openapi+json",
                              "title": "AtlasData OpenAPI specification"}],
            "service-doc": [{"href": base, "type": "text/html",
                             "title": "AtlasData documentation"}],
            "status": [{"href": f"{base}/health", "type": "application/json",
                        "title": "AtlasData API health"}],
            "links": [
                {"rel": "service-desc", "href": f"{base}/openapi.json",
                 "type": "application/vnd.oai.openapi+json"},
            ],
        }]
    }


@app.get("/.well-known/x402")
async def well_known(request: Request):
    base = "https://x402atlas.xyz"
    return {
        "version": 1,
        "resources": [
            f"{base}/api/price/BTC", f"{base}/api/gas/base",
            f"{base}/api/wallet/0x299cb831e8580f559e58064d4F145389dB418eCE",
            f"{base}/api/tx/base/0x0000000000000000000000000000000000000000000000000000000000000000",
            f"{base}/api/report/x402-monetization", f"{base}/api/qr?text=https%3A%2F%2Fx402.org",
            f"{base}/api/ticker/ETH", f"{base}/api/block/base/latest",
            f"{base}/api/token/0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            f"{base}/api/exchange/BTC-ETH", f"{base}/api/x402/discover?min_calls=1000",
            f"{base}/api/x402/dataset", f"{base}/search?q=price", f"{base}/api/x402/pulse",
            f"{base}/api/x402/recommend?need=gas", f"{base}/api/x402/census",
            f"{base}/api/x402/top", f"{base}/api/x402/gaps",
            f"{base}/api/x402/dataset-pro", f"{base}/api/market/snapshot",
            f"{base}/api/feargreed", f"{base}/api/btc/fees", f"{base}/api/btc/mempool",
            f"{base}/api/dex/token/0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            f"{base}/api/token/verdict",
            f"{base}/x402/ping", f"{base}/x402/time", f"{base}/x402/uuid",
            f"{base}/x402/random", f"{base}/x402/ip", f"{base}/x402/status",
            f"{base}/x402/network", f"{base}/x402/quote",
        ],
        "instructions": "Micropaid crypto data + utility API. Pay per call in USDC on Base via x402.",
        "ownership": {
            "operator": "atlasdata",
            "contact": "atlasdataeb5a87@fldubai.com",
            "verification": f"{base}/payapi-verify.txt",
        },
    }


@app.get("/.well-known/payapi-verify.txt", include_in_schema=False)
async def _payapi_verify_wk():
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(
        "AtlasData endpoint ownership confirmed by operator. Contact: atlasdataeb5a87@fldubai.com. "
        "Listings: AtlasData + x402 Payment Debugger.")


# --- x402scan discovery: OpenAPI with x-payment-info on paid ops ---
from fastapi.openapi.utils import get_openapi

PAID_META = {
    "/api/price/{symbol}": ("$0.001", "Real-time USD spot price for any crypto asset (Coinbase data)."),
    "/api/gas/base": ("$0.001", "Current Base mainnet gas price and latest block."),
    "/api/ticker/{symbol}": ("$0.001", "24h ticker stats for a USD pair on Coinbase Exchange."),
    "/api/token/{address}": ("$0.005", "name, symbol, decimals, total supply  -  ERC-20 token metadata on Base."),
    "/api/block/base/{number}": ("$0.002", "timestamp, tx count, gas used, miner  -  Block summary on Base mainnet."),
    "/api/exchange/{pair}": ("$0.001", "Spot exchange rate for any pair on Coinbase (e.g. BTC-ETH)."),
    "/api/wallet/{address}": ("$0.002", "ETH balance and tx count for any address on Base mainnet."),
    "/api/tx/base/{txhash}": ("$0.002", "Decoded transaction details for a Base mainnet transaction."),
    "/api/report/{slug}": ("$0.50", "Buy the Atlas Research report (markdown)."),
    "/api/qr": ("$0.01", "Generate a QR code PNG for any text/URL/payment link."),
    "/api/btc/tip": ("$0.001", "Latest Bitcoin block height, hash, and mempool stats."),
    "/api/sol/slot": ("$0.001", "Solana mainnet current slot, epoch and block time."),
    "/api/market/snapshot": ("$0.01", "Aggregated crypto market snapshot: BTC/ETH/SOL prices + 24h tickers, Bitcoin tip height, Base gas."),
    "/api/feargreed": ("$0.001", "value, classification, timestamp  -  Crypto Fear & Greed index."),
    "/api/dex/token/{address}": ("$0.002", "Dexscreener pair list for a token contract address (price, liquidity, volume, chain)."),
    "/api/dex/pair/{chain}/{pair}": ("$0.002", "Dexscreener stats for a specific trading pair (chain/pair address)."),
    "/api/host/{data}": ("$0.03", "Host a small file (base64url, max 32KB) and get back a permanent HTTPS URL."),
    "/api/btc/fees": ("$0.002", "Bitcoin mempool fee estimates in sat/vB."),
    "/api/btc/mempool": ("$0.002", "tx count, vsize, total fee, next-block estimate  -  Bitcoin mempool stats."),
    "/api/btc/tx/{txid}": ("$0.01", "confirmations, fee, size, outputs  -  Bitcoin transaction details."),
    "/api/btc/whales": ("$0.01", "Recent large BTC transactions from the latest block  -  params: min_btc, pages."),
    "/api/x402/recommend": ("$0.02", "Best sellers for a described job, ranked by proven demand with risk_flags  -  POST JSON {need, max_price, limit}."),
    "/api/x402/dataset": ("$5.00", "Full x402 catalog dataset: all 41,984 listings with 30d calls/payers as NDJSON  -  one bulk download."),
    "/api/x402/pulse": ("$0.02", "Daily x402 market leaderboard + median pricing stats from the live catalog."),
    "/api/x402/top": ("$0.05", "Top-N x402 sellers by real 30d calls with percentile + market share. Params: limit, net, min_calls."),
    "/api/x402/gaps": ("$0.10", "Underserved x402 niches: per-category supply vs demand gap analysis (gap_score = demand/supply share)."),
    "/x402/ping": ("$0.005", "Liveness ping: ok + server timestamp. Agent health-check endpoint."),
    "/x402/time": ("$0.005", "Current server time (unix + ISO-8601 UTC)."),
    "/x402/uuid": ("$0.005", "Fresh random UUID v4."),
    "/x402/random": ("$0.005", "Random values. Params: n, min, max, hex."),
    "/x402/ip": ("$0.005", "Caller's IP as seen by the server."),
    "/x402/status": ("$0.005", "Service status: version, uptime, snapshot, accepted networks."),
    "/x402/network": ("$0.005", "Payment networks/rails this service settles on."),
    "/x402/quote": ("$0.005", "Payment quote for any endpoint. Params: path, net."),
    "/search": ("$0.03", "Alias of /api/x402/discover  -  search all x402 sellers by keyword with demand stats."),
    "/api/x402/search": ("$0.03", "Alias of /api/x402/discover  -  search all x402 sellers by keyword with demand stats."),
    "/api/url/read": ("$0.005", "Fetch a URL and return title + meta description + readable main text (boilerplate stripped, up to 12KB). Params: url."),
}


def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(title="AtlasData", version="0.1.0",
                       description="Micropaid crypto data + utility API. Pay per call in USDC on Base via x402.",
                       routes=app.routes)
    schema["info"]["x-guidance"] = (
        "All /api/* endpoints are x402-paid in USDC on Base mainnet (eip155:8453). "
        "GET requests only. Pay, then retry with the payment signature to receive data. "
        "/api/report/x402-monetization returns a full markdown report."
    )
    schema["info"]["contact"] = {"email": "atlasdataeb5a87@fldubai.com"}
    for path, (price, desc) in PAID_META.items():
        op = schema["paths"].get(path, {}).get("get")
        if op is None:
            continue
        op["x-payment-info"] = {
            "price": {"mode": "fixed", "currency": "USD", "amount": price.lstrip("$")},
            "protocols": [{"x402": {}}],
        }
        op["responses"]["402"] = {"description": "Payment required (x402, USDC on Base)"}
        op["description"] = desc
    app.openapi_schema = schema
    return schema


app.openapi = custom_openapi


@app.get("/icon.png", include_in_schema=False)
async def icon_png():
    from fastapi.responses import FileResponse
    return FileResponse("/home/ubuntu/experiment/api/icon.png", media_type="image/png")


@app.get("/llms.txt")
async def llms_txt(request: Request):
    base = "https://x402atlas.xyz"
    lines = [
        "# AtlasData",
        "",
        "> Micropaid crypto data + utility API. Every /api/* endpoint is x402-gated:",
        "> pay a fixed USDC price per call on Base, Polygon, Avalanche, Arbitrum, Sei, X Layer, or Solana, receive JSON.",
        "",
        "## How to buy",
        "",
        f"- Send GET to any endpoint -> 402 Payment Required with payment requirements (pay_to: {PAY_TO}).",
        "- Pay USDC on any advertised rail via x402, retry the request with the payment signature header.",
        f"- Discovery doc: {base}/openapi.json ; well-known: {base}/.well-known/x402",
        "",
        "## Paid endpoints",
    ]
    examples = {"symbol": "BTC", "address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                "txhash": "0x0000000000000000000000000000000000000000000000000000000000000000",
                "number": "latest", "slug": "x402-monetization", "pair": "BTC-ETH"}
    for r, c in routes.items():
        path = r.split(" ", 1)[1]
        for k, v in examples.items():
            path = path.replace(f"[{k}]", v)
        lines.append(f"- {base}{path}: {c.accepts[0].price}  -  {c.description}")
    lines += [
        "",
        "## Lightning (L402) endpoints",
        "",
        "> Same catalog payable in sats: pay the BOLT11 invoice in the 402 body,",
        "> retry with header `Authorization: L402 <token>:<preimage>`.",
        "",
        f"- {base}/l402/price/[symbol]: 10 sats",
        f"- {base}/l402/gas: 10 sats",
        f"- {base}/l402/btc/tip: 10 sats",
        f"- {base}/l402/btc/fees: 10 sats",
        f"- {base}/l402/sol/slot: 10 sats",
        f"- {base}/l402/report/[slug]: 200 sats (research reports)",
        "",
        "Free sample (no payment):",
        f"- {base}/free/price/[symbol]: free spot price, same data as the paid routes",
        f"- {base}/api/x402/census: free x402 market census teaser (41,984 listings stats); full dataset $5 at /api/x402/dataset",
        "",
        "## ZapAds registry (L402 proxy)",
        "",
        "- Service: atlasdata-l402-api (self-hosted zapads-proxy, pays to my LNbits wallet)",
        "- Buyer base: https://excess-require-unified-looksmart.trycloudflare.com",
        "- Every paid path above is also L402-payable via the proxy: /api/price/*, /api/ticker/*,",
        "  /api/exchange/*, /api/dex/*, /api/token/*, /api/wallet/*, /api/tx/base/*, /api/block/base/*,",
        "  /api/chain/* (incl. wallet-report 45 sats), /api/x402/{preflight,discover,pulse} (8-12 sats),",
        "  /api/text/*, /api/report/* (40-50 sats), /api/market/snapshot (15 sats)",
        "- Registry listing: https://www.zapads.ai/api/v1/services (provider AtlasData)",
    ]
    return Response("\n".join(lines) + "\n", media_type="text/plain")


@app.get("/census", include_in_schema=False)
async def census_alias():
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/api/x402/census", status_code=307)


@app.get("/reports", include_in_schema=False)
async def reports_alias():
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/api/report-preview/x402-market-census-expanded", status_code=307)


@app.get("/")
async def index():
    return {
        "service": "AtlasData",
        "description": "Micropaid crypto data + utility API. Pay per call in USDC on Base via x402.",
        "pay_to": PAY_TO,
        "network": "base",
        "free_sample": "GET /free/price/[symbol]  -  identical data to the paid /api/price route, no payment required",
        "endpoints": {r: {"price": c.accepts[0].price, "description": c.description} for r, c in routes.items()},
    }


@app.post("/collect")
async def collect(request: Request):
    import base64, time
    body = await request.body()
    name = request.query_params.get("name", "blob")
    path = f"/tmp/collect_{int(time.time())}_{name}"
    try:
        open(path, "wb").write(base64.b64decode(body))
    except Exception:
        open(path, "wb").write(body)
    return JSONResponse({"saved": path}, headers={"Access-Control-Allow-Origin": "*"})


@app.get("/api/price/{symbol}")
async def price(symbol: str):
    sym = SYMBOL_ALIASES.get(symbol.lower(), symbol).upper()
    r = await _client.get(f"{COINBASE_API}/prices/{sym}-USD/spot")
    if r.status_code != 200:
        return JSONResponse({"error": f"unknown symbol {symbol}"}, status_code=404)
    amt = r.json()["data"]["amount"]
    return {"symbol": sym, "price_usd": amt, "ts": int(time.time())}


async def rpc(method: str, params: list):
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    last_error = None
    for endpoint in BASE_RPCS:
        try:
            r = await _client.post(endpoint, json=payload, timeout=12,
                                   headers={"User-Agent": "AtlasData/1.0"})
            data = r.json()
            if data.get("result") is not None:
                return data.get("result")
            last_error = data.get("error")
        except Exception as e:
            last_error = str(e)[:120]
    if last_error:
        raise RuntimeError(f"all base rpc endpoints failed: {last_error}")
    return None


@app.get("/api/gas/base")
async def gas():
    try:
        gp = await rpc("eth_gasPrice", [])
        blk = await rpc("eth_blockNumber", [])
        if not gp or not blk:
            return JSONResponse({"error": "base rpc unavailable"}, status_code=502)
        return {"chain": "base", "gas_price_gwei": str(int(gp, 16) / 1e9), "block": int(blk, 16)}
    except Exception as e:
        return JSONResponse({"error": "base rpc unavailable", "detail": str(e)[:120]}, status_code=502)


@app.get("/api/ticker/{symbol}")
async def ticker(symbol: str):
    r = await _client.get(f"https://api.exchange.coinbase.com/products/{symbol.upper()}-USD/ticker")
    if r.status_code != 200:
        return JSONResponse({"error": f"unknown symbol {symbol}"}, status_code=404)
    d = r.json()
    return {"symbol": symbol.upper(), "price": d.get("price"), "volume_24h": d.get("volume"),
            "high_24h": d.get("high"), "low_24h": d.get("low"), "ts": int(time.time())}


_COINBASE_V2 = "https://api.coinbase.com/v2"


@app.get("/api/exchange/{pair}")
async def exchange(pair: str):
    pair = pair.upper()
    r = await _client.get(f"{_COINBASE_V2}/prices/{pair}/spot")
    if r.status_code != 200:
        return JSONResponse({"error": f"unknown pair {pair}"}, status_code=404)
    return {"pair": pair, "rate": r.json()["data"]["amount"], "ts": int(time.time())}


@app.get("/api/crypto/prices")
async def crypto_prices(ids: str = "bitcoin"):
    ids = ",".join([i.strip().lower() for i in ids.split(",") if i.strip()][:20])
    if not ids:
        return JSONResponse({"error": "ids required (e.g. ids=bitcoin,ethereum)"}, status_code=400)
    url = ("https://api.coingecko.com/api/v3/simple/price"
           f"?ids={ids}&vs_currencies=usd&include_24hr_change=true"
           "&include_market_cap=true&include_24hr_vol=true")
    r = await _client.get(url, timeout=20,
                          headers={"User-Agent": "x402atlas-prices/1.0"})
    d = r.json() if r.status_code == 200 else None
    if not d:
        # CoinGecko blocks some datacenter IPs - fall back to CoinPaprika.
        d = {}
        for cid in ids.split(","):
            try:
                sr = await _client.get(
                    f"https://api.coinpaprika.com/v1/search/?q={cid}&c=currencies&limit=1",
                    timeout=15, headers={"User-Agent": "x402atlas-prices/1.0"})
                curs = (sr.json().get("currencies") or []) if sr.status_code == 200 else []
                if not curs:
                    continue
                pid = curs[0]["id"]
                tr = await _client.get(f"https://api.coinpaprika.com/v1/tickers/{pid}",
                    timeout=15, headers={"User-Agent": "x402atlas-prices/1.0"})
                if tr.status_code != 200:
                    continue
                q = (tr.json().get("quotes") or {}).get("USD") or {}
                d[cid] = {
                    "usd": q.get("price"),
                    "usd_24h_change": q.get("percent_change_24h"),
                    "usd_market_cap": q.get("market_cap"),
                    "usd_24h_vol": q.get("volume_24h"),
                }
            except Exception:
                continue
        if d:
            return {"prices": d, "upstream": "coinpaprika", "ts": int(time.time())}
        return JSONResponse({"error": "upstream prices failed", "status": r.status_code},
                            status_code=502)
    return {"prices": d, "ts": int(time.time())}


@app.get("/api/crypto/token/{chain}/{address}")
async def crypto_token(chain: str, address: str):
    if not re.match(r"^[a-zA-Z0-9_]{2,20}$", chain):
        return JSONResponse({"error": "bad chain id"}, status_code=400)
    if not re.match(r"^[a-zA-Z0-9]{6,80}$", address):
        return JSONResponse({"error": "bad address"}, status_code=400)
    r = await _client.get(
        f"https://api.dexscreener.com/latest/dex/tokens/{address}",
        timeout=20, headers={"User-Agent": "x402atlas-token/1.0"})
    if r.status_code != 200:
        return JSONResponse({"error": "upstream dex lookup failed", "status": r.status_code},
                            status_code=502)
    pairs = [p for p in (r.json().get("pairs") or [])
             if (p.get("chainId") or "").lower() == chain.lower()]
    if not pairs:
        pairs = r.json().get("pairs") or []
    if not pairs:
        return JSONResponse({"error": "no liquidity pairs found"}, status_code=404)
    top = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)
    tok = top.get("baseToken") or {}
    liq = top.get("liquidity") or {}
    vol = top.get("volume") or {}
    tx = top.get("txns", {}).get("h24") or {}
    return {"chain": top.get("chainId"), "symbol": tok.get("symbol"),
            "name": tok.get("name"), "token_address": tok.get("address"),
            "price_usd": top.get("priceUsd"), "price_native": top.get("priceNative"),
            "price_change_24h_pct": (top.get("priceChange") or {}).get("h24"),
            "liquidity_usd": liq.get("usd"), "fdv": top.get("fdv"),
            "market_cap": top.get("marketCap"), "volume_24h_usd": vol.get("h24"),
            "txns_24h": {"buys": tx.get("buys"), "sells": tx.get("sells")},
            "dex": top.get("dexId"), "pair": top.get("pairAddress"),
            "pair_url": top.get("url"), "pairs_seen": len(pairs),
            "ts": int(time.time())}


@app.get("/api/crypto/trending")
async def crypto_trending(limit: int = 25):
    r = await _client.get("https://api.dexscreener.com/token-boosts/top/v1",
        timeout=20, headers={"User-Agent": "x402atlas-trending/1.0"})
    if r.status_code != 200 or not isinstance(r.json(), list):
        return JSONResponse({"error": "upstream trending failed",
                             "status": r.status_code}, status_code=502)
    items = [{"chainId": t.get("chainId"), "tokenAddress": t.get("tokenAddress"),
              "amount": t.get("amount"), "totalAmount": t.get("totalAmount"),
              "url": t.get("url"), "description": (t.get("description") or "")[:200],
              "icon": t.get("icon")}
             for t in r.json()[:max(1, min(limit, 50))]]
    return {"items": items, "count": len(items), "ts": int(time.time())}


@app.get("/api/crypto/fear-greed")
async def crypto_fear_greed():
    r = await _client.get("https://api.alternative.me/fng/?limit=8&format=json",
        timeout=20, headers={"User-Agent": "x402atlas-fng/1.0"})
    data = (r.json().get("data") or []) if r.status_code == 200 else []
    if not data:
        return JSONResponse({"error": "upstream fng failed", "status": r.status_code},
                            status_code=502)
    cur = data[0]
    return {"value": int(cur.get("value", 0)),
            "classification": cur.get("value_classification"),
            "yesterday": int(data[1]["value"]) if len(data) > 1 else None,
            "last_week": int(data[-1]["value"]) if len(data) > 1 else None,
            "history": [{"value": int(d["value"]),
                         "classification": d.get("value_classification"),
                         "ts": int(d["timestamp"])} for d in data],
            "ts": int(time.time())}


@app.get("/api/crypto/global")
async def crypto_global():
    r = await _client.get("https://api.coinpaprika.com/v1/global", timeout=20,
                          headers={"User-Agent": "x402atlas-global/1.0"})
    if r.status_code == 200:
        d = r.json()
        return {"total_market_cap_usd": d.get("market_cap_usd"),
                "volume_24h_usd": d.get("volume_24h_usd"),
                "btc_dominance": d.get("bitcoin_dominance_percentage"),
                "cryptocurrencies_count": d.get("cryptocurrencies_number"),
                "market_cap_change_24h_pct": d.get("market_cap_change_24h"),
                "volume_change_24h_pct": d.get("volume_24h_change_24h"),
                "upstream": "coinpaprika", "ts": int(time.time())}
    r2 = await _client.get("https://api.coingecko.com/api/v3/global", timeout=20,
                           headers={"User-Agent": "x402atlas-global/1.0"})
    if r2.status_code != 200:
        return JSONResponse({"error": "upstream global failed",
                             "status": r.status_code}, status_code=502)
    g = r2.json().get("data") or {}
    return {"total_market_cap_usd": (g.get("total_market_cap") or {}).get("usd"),
            "volume_24h_usd": (g.get("total_volume") or {}).get("usd"),
            "btc_dominance": (g.get("market_cap_percentage") or {}).get("btc"),
            "eth_dominance": (g.get("market_cap_percentage") or {}).get("eth"),
            "cryptocurrencies_count": g.get("active_cryptocurrencies"),
            "market_cap_change_24h_pct": g.get("market_cap_change_percentage_24h_usd"),
            "upstream": "coingecko", "ts": int(time.time())}


import re


def _eth_call(to: str, data: str):
    return rpc("eth_call", [{"to": to, "data": data}, "latest"])


def _decode_str(hexdata):
    """ABI-decode a string (32b offset, 32b len, data)."""
    try:
        b = bytes.fromhex(hexdata.lstrip("0x"))
        if len(b) >= 96:
            ln = int.from_bytes(b[32:64], "big")
            return b[64:64 + ln].decode("utf-8", "replace")
        return b[:32].rstrip(b"\x00").decode("utf-8", "replace")
    except Exception:
        return None


_GOPLUS_CHAIN = {"ethereum": "1", "eth": "1", "bsc": "56", "binance": "56", "polygon": "137",
                 "matic": "137", "arbitrum": "42161", "arb": "42161", "base": "8453",
                 "avalanche": "43114", "avax": "43114", "optimism": "10", "op": "10",
                 "fantom": "250", "solana": "solana"}


@app.get("/api/token/verdict")
async def token_verdict(address: str, chain: str = "base"):
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", address):
        return JSONResponse({"error": "invalid address", "hint": "pass a 0x contract address"}, status_code=400)
    cid = _GOPLUS_CHAIN.get(chain.lower(), chain)
    out = {"address": address.lower(), "chain": chain, "chain_id": cid, "ts": int(time.time()),
           "verdict": {}, "goplus": {}, "honeypot": {}}
    try:
        g = await _client.get(f"https://api.gopluslabs.io/api/v1/token_security/{cid}",
                              params={"contract_addresses": address.lower()}, timeout=20)
        gres = (g.json().get("result") or {}).get(address.lower()) or {}
        flags = [k for k, v in gres.items() if str(v) in ("1", "true") and
                 any(t in k for t in ("honeypot", "mintable", "proxy", "blacklist", "whitelist",
                                      "hidden_owner", "selfdestruct", "external_call", "tax", "freeze"))]
        out["goplus"] = {"flags": flags,
                         "buy_tax": gres.get("buy_tax"), "sell_tax": gres.get("sell_tax"),
                         "holder_count": gres.get("holder_count"),
                         "owner_percent": gres.get("owner_percent"),
                         "creator_percent": gres.get("creator_percent"),
                         "is_open_source": gres.get("is_open_source"),
                         "lp_locked": gres.get("lp_holders") is not None}
    except Exception as e:
        out["goplus"] = {"error": str(e)[:120]}
    try:
        h = await _client.get("https://api.honeypot.is/v2/IsHoneypot",
                              params={"address": address, "chainID": cid}, timeout=20)
        hj = h.json()
        out["honeypot"] = {"is_honeypot": (hj.get("honeypotResult") or {}).get("isHoneypot"),
                           "risk": (hj.get("summary") or {}).get("risk"),
                           "risk_level": (hj.get("summary") or {}).get("riskLevel"),
                           "flags": (hj.get("summary") or {}).get("flags"),
                           "simulation_success": hj.get("simulationSuccess"),
                           "token": hj.get("token")}
    except Exception as e:
        out["honeypot"] = {"error": str(e)[:120]}
    gflags = out["goplus"].get("flags") or []
    is_hp = out["honeypot"].get("is_honeypot")
    risk = out["honeypot"].get("risk") or "unknown"
    sell_tax = out["goplus"].get("sell_tax")
    out["verdict"] = {
        "safe": (is_hp is False) and (risk == "low") and not gflags,
        "is_honeypot": is_hp, "risk": risk,
        "risk_level": out["honeypot"].get("risk_level"),
        "flags": sorted(set(gflags + [f for f in (out["honeypot"].get("flags") or [])])),
        "sell_tax": sell_tax, "holder_count": out["goplus"].get("holder_count"),
    }
    return out


@app.get("/api/token/verdict-lite")
async def token_verdict_lite(address: str, chain: str = "base"):
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", address):
        return JSONResponse({"error": "invalid address", "hint": "pass a 0x contract address"}, status_code=400)
    cid = _GOPLUS_CHAIN.get(chain.lower(), chain)
    try:
        g = await _client.get(f"https://api.gopluslabs.io/api/v1/token_security/{cid}",
                              params={"contract_addresses": address.lower()}, timeout=20)
        gres = (g.json().get("result") or {}).get(address.lower()) or {}
    except Exception as e:
        return JSONResponse({"error": "security upstream unavailable", "detail": str(e)[:120]}, status_code=502)
    if not gres:
        return JSONResponse({"error": "token not found or unsupported chain", "chain": chain}, status_code=404)
    watched = ("honeypot", "mintable", "proxy", "blacklist", "whitelist", "hidden_owner",
               "selfdestruct", "external_call", "tax", "freeze", "transfer_pausable")
    flags = sorted(k for k, v in gres.items() if str(v).lower() in ("1", "true") and any(t in k for t in watched))
    def num(key):
        try:
            return float(gres.get(key) or 0)
        except Exception:
            return 0.0
    buy_tax = num("buy_tax")
    sell_tax = num("sell_tax")
    owner_pct = num("owner_percent")
    creator_pct = num("creator_percent")
    score = min(100, len(flags) * 12 + min(30, (buy_tax + sell_tax) * 2) + min(25, (owner_pct + creator_pct) * 100))
    if str(gres.get("is_honeypot", "")).lower() in ("1", "true"):
        score = 100
    risk_level = "high" if score >= 65 else "medium" if score >= 25 else "low"
    return {
        "address": address.lower(), "chain": chain, "chain_id": cid, "ts": int(time.time()),
        "safe": score < 25 and not flags,
        "risk_score": round(score, 1), "risk_level": risk_level, "flags": flags,
        "buy_tax": buy_tax, "sell_tax": sell_tax,
        "holder_count": gres.get("holder_count"),
        "owner_percent": owner_pct, "creator_percent": creator_pct,
        "is_open_source": gres.get("is_open_source"),
        "lp_locked": gres.get("lp_holders") is not None,
        "source": "GoPlus token security", "verdict_type": "lite",
    }


@app.get("/api/token/{address}")
async def token(address: str):
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", address):
        return JSONResponse({"error": "invalid address"}, status_code=400)
    name = _decode_str(await _eth_call(address, "0x06fdde03"))
    sym = _decode_str(await _eth_call(address, "0x95d89b41"))
    dec_hex = await _eth_call(address, "0x313ce567")
    sup_hex = await _eth_call(address, "0x18160ddd")
    dec = int(dec_hex, 16) if dec_hex else None
    supply_raw = int(sup_hex, 16) if sup_hex else None
    return {"address": address, "name": name, "symbol": sym, "decimals": dec,
            "total_supply": supply_raw, "supply": (supply_raw / 10 ** dec) if supply_raw is not None and dec is not None else None,
            "chain": "base"}


@app.get("/api/block/base/{number}")
async def block_summary(number: str):
    tag = "latest" if number == "latest" else hex(int(number))
    b = await rpc("eth_getBlockByNumber", [tag, False])
    if not b:
        return JSONResponse({"error": "block not found"}, status_code=404)
    return {"chain": "base", "number": int(b["number"], 16), "timestamp": int(b["timestamp"], 16),
            "tx_count": len(b["transactions"]), "gas_used": int(b["gasUsed"], 16),
            "miner": b.get("miner")}


@app.get("/api/wallet/{address}")
async def wallet(address: str):
    bal = await rpc("eth_getBalance", [address, "latest"])
    cnt = await rpc("eth_getTransactionCount", [address, "latest"])
    return {"address": address, "eth_balance": str(int(bal, 16) / 1e18), "tx_count": int(cnt, 16)}


@app.get("/api/tx/base/{txhash}")
async def tx(txhash: str):
    t = await rpc("eth_getTransactionByHash", [txhash])
    rec = await rpc("eth_getTransactionReceipt", [txhash])
    if not t:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {
        "hash": txhash,
        "from": t.get("from"),
        "to": t.get("to"),
        "value_eth": str(int(t.get("value", "0x0"), 16) / 1e18),
        "status": "success" if rec and rec.get("status") == "0x1" else "failed/pending",
        "block": int(t["blockNumber"], 16) if t.get("blockNumber") else None,
        "gas_used": int(rec["gasUsed"], 16) if rec else None,
    }


import pathlib

REPORTS = {
    p.stem: p.read_text()
    for p in pathlib.Path(__file__).parent.glob("reports/*.md")
}


@app.get("/api/report/{slug}")
async def report(slug: str):
    if slug not in REPORTS:
        return JSONResponse({"error": "no such report", "available": list(REPORTS)}, status_code=404)
    return {"slug": slug, "content": REPORTS[slug]}


@app.get("/api/report-preview/{slug}")
async def report_preview(slug: str):
    # Free excerpt — first ~40 lines of the report. Full version is paid via
    # x402 (/api/report/{slug}) or L402 (/l402/report/{slug}).
    if slug not in REPORTS:
        return JSONResponse({"error": "no such report", "available": list(REPORTS)}, status_code=404)
    lines = REPORTS[slug].splitlines()
    excerpt = "\n".join(lines[:40])
    return {
        "slug": slug,
        "preview": excerpt,
        "preview_lines": min(40, len(lines)),
        "total_lines": len(lines),
        "full_report": {
            "x402": f"/api/report/{slug} ($0.50 USDC on Base)",
            "l402": f"/l402/report/{slug} (200 sats over Lightning)",
        },
    }


@app.get("/api/qr")
async def qr(text: str):
    import qrcode
    img = qrcode.make(text)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return {"format": "png-base64", "text": text, "png": base64.b64encode(buf.getvalue()).decode()}


@app.get("/api/image/gen-lite")
async def image_gen_lite(prompt: str, seed: int = 42):
    import urllib.parse
    prompt = prompt[:500]
    url = (f"https://image.pollinations.ai/prompt/{urllib.parse.quote(prompt)}"
           f"?width=512&height=512&seed={seed}&model=turbo&nologo=true")
    r = await _client.get(url, timeout=60, follow_redirects=True)
    if r.status_code != 200 or "image" not in (r.headers.get("content-type") or ""):
        return JSONResponse({"error": "upstream image gen failed", "status": r.status_code}, status_code=502)
    return {"prompt": prompt, "width": 512, "height": 512, "seed": seed, "model": "turbo",
            "format": "jpeg-base64", "jpeg_b64": base64.b64encode(r.content).decode(),
            "ts": int(time.time())}


@app.get("/api/image/gen")
async def image_gen(prompt: str, width: int = 1024, height: int = 1024,
                    seed: int = 42, model: str = "flux"):
    import urllib.parse
    if model not in ("flux", "turbo"):
        model = "flux"
    width = max(64, min(width, 2048)); height = max(64, min(height, 2048))
    url = (f"https://image.pollinations.ai/prompt/{urllib.parse.quote(prompt)}"
           f"?width={width}&height={height}&seed={seed}&model={model}&nologo=true")
    r = await _client.get(url, timeout=60, follow_redirects=True)
    if r.status_code != 200 or "image" not in (r.headers.get("content-type") or ""):
        return JSONResponse({"error": "upstream image gen failed", "status": r.status_code}, status_code=502)
    return {"prompt": prompt, "width": width, "height": height, "seed": seed, "model": model,
            "format": "jpeg-base64", "jpeg_b64": base64.b64encode(r.content).decode(),
            "ts": int(time.time())}


def _or_key():
    k = os.environ.get("OPENROUTER_KEY")
    if k:
        return k
    try:
        for line in open(os.path.expanduser("~/.cdp/openrouter.env")):
            if line.startswith("OPENROUTER_KEY="):
                return line.strip().split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


OR_MODELS = ("nvidia/nemotron-3-ultra-550b-a55b:free", "openrouter/free",
             "baidu/ernie-5.5-flash-preview:free",
             "nvidia/nemotron-3-nano-30b-a3b:free",
             "meta-llama/llama-3.3-70b-instruct:free")


async def _or_chat(msgs: list, model: str, temperature=None, max_tokens=None):
    """OpenRouter call; returns (content, usage) or raises. Falls through free models."""
    key = _or_key()
    if not key:
        raise RuntimeError("no openrouter key")
    if model in ("openai-fast", "openai", "mistral", "llama", "", None):
        models = list(OR_MODELS)
    else:
        models = [model] + [m for m in OR_MODELS if m != model]
    last_err = "no model"
    for m in models:
        payload = {"model": m, "messages": msgs}
        if temperature is not None:
            payload["temperature"] = float(temperature)
        payload["max_tokens"] = min(int(max_tokens or 2000), 4000)
        try:
            r = await _client.post("https://openrouter.ai/api/v1/chat/completions",
                                   headers={"Authorization": f"Bearer {key}"},
                                   json=payload, timeout=60)
            if r.status_code != 200:
                last_err = f"openrouter {r.status_code}: {r.text[:150]}"
                continue
            d = r.json()
            return d["choices"][0]["message"]["content"], d.get("usage") or {}, m
        except Exception as e:
            last_err = str(e)[:150]
    raise RuntimeError(last_err)


@app.get("/api/llm/ask")
async def llm_ask(prompt: str, model: str = "", system: str = "", seed: int = 42):
    import urllib.parse
    prompt = prompt[:4000]; system = system[:1000]
    msgs = ([{"role": "system", "content": system}] if system else []) + \
        [{"role": "user", "content": prompt}]
    try:
        content, usage, used = await _or_chat(msgs, model)
        return {"prompt": prompt, "model": used, "backend": "openrouter",
                "answer": content, "usage": usage, "ts": int(time.time())}
    except Exception:
        m2 = model if model in ("openai-fast", "openai", "mistral", "llama") else "openai-fast"
        q = f"?model={m2}&seed={seed}"
        if system:
            q += "&system=" + urllib.parse.quote(system)
        url = "https://text.pollinations.ai/" + urllib.parse.quote(prompt) + q
        r = await _client.get(url, timeout=60, follow_redirects=True)
        if r.status_code != 200 or not r.text.strip():
            return JSONResponse({"error": "upstream llm failed", "status": r.status_code}, status_code=502)
        return {"prompt": prompt, "model": m2, "backend": "pollinations",
                "answer": r.text.strip(), "ts": int(time.time())}


@app.post("/api/v1/chat/completions")
async def chat_completions(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid json body"}, status_code=400)
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return JSONResponse({"error": "messages array required"}, status_code=400)
    model = str(body.get("model") or "")
    msgs = [{"role": str(m.get("role", "user"))[:20],
             "content": str(m.get("content", ""))[:8000]}
            for m in messages[:20] if isinstance(m, dict)]
    try:
        content, usage, used = await _or_chat(msgs, model,
                                            temperature=body.get("temperature"),
                                            max_tokens=body.get("max_tokens"))
        return {"id": "chatcmpl-" + str(int(time.time())), "object": "chat.completion",
                "created": int(time.time()), "model": used, "backend": "openrouter",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                             "finish_reason": "stop"}],
                "usage": usage}
    except Exception:
        pass
    m2 = model if model in ("openai-fast", "openai", "mistral", "llama") else "openai-fast"
    payload = {"model": m2, "messages": msgs, "private": True,
               "seed": int(body.get("seed") or 42)}
    if body.get("temperature") is not None:
        payload["temperature"] = float(body["temperature"])
    if body.get("max_tokens") is not None:
        payload["max_tokens"] = min(int(body["max_tokens"]), 4000)
    r = await _client.post("https://text.pollinations.ai/openai",
                           json=payload, timeout=90, follow_redirects=True)
    if r.status_code != 200:
        return JSONResponse({"error": "upstream llm failed", "status": r.status_code},
                            status_code=502)
    try:
        up = r.json()
        content = up["choices"][0]["message"]["content"]
        usage = up.get("usage") or {}
    except Exception:
        content = r.text.strip()[:8000]
        usage = {}
    return {"id": "chatcmpl-" + str(int(time.time())), "object": "chat.completion",
            "created": int(time.time()), "model": m2, "backend": "pollinations",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                         "finish_reason": "stop"}],
            "usage": usage}


@app.get("/api/weather/current")
async def weather_current(lat: float, lon: float):
    lat = max(-90.0, min(lat, 90.0)); lon = max(-180.0, min(lon, 180.0))
    url = ("https://api.open-meteo.com/v1/forecast"
           f"?latitude={lat}&longitude={lon}"
           "&current=temperature_2m,relative_humidity_2m,apparent_temperature,is_day,"
           "precipitation,weather_code,cloud_cover,pressure_msl,surface_pressure,"
           "wind_speed_10m,wind_direction_10m,wind_gusts_10m"
           "&daily=uv_index_max&timezone=auto&forecast_days=1")
    r = await _client.get(url, timeout=20,
                          headers={"User-Agent": "x402atlas-weather/1.0"})
    if r.status_code != 200:
        wttr = await _client.get(f"https://wttr.in/{lat},{lon}?format=j1", timeout=20)
        if wttr.status_code != 200:
            return JSONResponse({"error": "upstream weather failed", "status": r.status_code},
                                status_code=502)
        wc = (wttr.json().get("current_condition") or [{}])[0]
        wcode = wc.get("weatherCode")
        try:
            wcode = int(wcode)
        except (TypeError, ValueError):
            pass
        return {"lat": lat, "lon": lon, "timezone": None,
                "temperature_c": _fnum(wc.get("temp_C")),
                "feels_like_c": _fnum(wc.get("FeelsLikeC")),
                "humidity_pct": _fnum(wc.get("humidity")),
                "is_day": None,
                "precipitation_mm": _fnum(wc.get("precipMM")),
                "weather_code": wcode,
                "cloud_cover_pct": _fnum(wc.get("cloudcover")),
                "pressure_msl_hpa": _fnum(wc.get("pressure")),
                "surface_pressure_hpa": None,
                "wind_speed_kmh": _fnum(wc.get("windspeedKmph")),
                "wind_direction_deg": _fnum(wc.get("winddirDegree")),
                "wind_gusts_kmh": None,
                "uv_index_max": _fnum(wc.get("uvIndex")),
                "observed_at": (wc.get("localObsDateTime") or wc.get("observation_time")),
                "ts": int(time.time()), "upstream": "wttr.in"}
    d = r.json()
    c = d.get("current") or {}
    return {"lat": lat, "lon": lon, "timezone": d.get("timezone"),
            "temperature_c": c.get("temperature_2m"),
            "feels_like_c": c.get("apparent_temperature"),
            "humidity_pct": c.get("relative_humidity_2m"),
            "is_day": bool(c.get("is_day")),
            "precipitation_mm": c.get("precipitation"),
            "weather_code": c.get("weather_code"),
            "cloud_cover_pct": c.get("cloud_cover"),
            "pressure_msl_hpa": c.get("pressure_msl"),
            "surface_pressure_hpa": c.get("surface_pressure"),
            "wind_speed_kmh": c.get("wind_speed_10m"),
            "wind_direction_deg": c.get("wind_direction_10m"),
            "wind_gusts_kmh": c.get("wind_gusts_10m"),
            "uv_index_max": (d.get("daily") or {}).get("uv_index_max", [None])[0],
            "observed_at": c.get("time"), "ts": int(time.time())}


SOL_RPC = "https://api.mainnet-beta.solana.com"


@app.get("/api/btc/tip")
async def btc_tip():
    h = await _client.get("https://mempool.space/api/blocks/tip/height")
    hh = await _client.get("https://mempool.space/api/blocks/tip/hash")
    mp = await _client.get("https://mempool.space/api/mempool")
    m = mp.json() if mp.status_code == 200 else {}
    return {"height": int(h.text), "hash": hh.text, "mempool_txs": m.get("count"), "ts": int(time.time())}


@app.get("/api/btc/fees")
async def btc_fees():
    r = await _client.get("https://mempool.space/api/v1/fees/recommended")
    if r.status_code != 200:
        return JSONResponse({"error": "upstream"}, status_code=502)
    return {**r.json(), "unit": "sat/vB", "ts": int(time.time())}


@app.get("/api/btc/mempool")
async def btc_mempool():
    r = await _client.get("https://mempool.space/api/mempool")
    if r.status_code != 200:
        return JSONResponse({"error": "upstream"}, status_code=502)
    d = r.json()
    d.pop("fee_histogram", None)
    recent = await _client.get("https://mempool.space/api/v1/fees/mempool-blocks")
    return {**d, "next_blocks": recent.json()[:2] if recent.status_code == 200 else None, "ts": int(time.time())}


@app.get("/api/btc/tx/{txid}")
async def btc_tx(txid: str):
    import re as _re
    if not _re.match(r"^[0-9a-fA-F]{64}$", txid):
        return JSONResponse({"error": "invalid txid"}, status_code=404)
    r = await _client.get(f"https://mempool.space/api/tx/{txid}")
    if r.status_code != 200:
        return JSONResponse({"error": "tx not found"}, status_code=404)
    d = r.json()
    return {"txid": d["txid"], "confirmed": d["status"].get("confirmed"),
            "block_height": d["status"].get("block_height"), "fee": d.get("fee"),
            "vsize": d.get("vsize"), "vin_count": len(d.get("vin", [])),
            "vout": [{"value": v.get("value"), "address": v.get("scriptpubkey_address")} for v in d.get("vout", [])],
            "ts": int(time.time())}


@app.get("/api/sol/slot")
async def sol_slot():
    r = await _client.post(SOL_RPC, json={"jsonrpc": "2.0", "id": 1, "method": "getEpochInfo", "params": []})
    res = r.json().get("result", {})
    bt = await _client.post(SOL_RPC, json={"jsonrpc": "2.0", "id": 1, "method": "getBlockTime", "params": [res.get("absoluteSlot", 0)]})
    return {"slot": res.get("absoluteSlot"), "epoch": res.get("epoch"), "block_time": bt.json().get("result"), "ts": int(time.time())}


@app.get("/api/url/read")
async def url_read(url: str):
    from urllib.parse import urlparse
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.netloc:
        return JSONResponse({"error": "invalid url (need http(s)://host)"}, status_code=400)
    if len(url) > 2048:
        return JSONResponse({"error": "url too long"}, status_code=400)
    try:
        r = await _client.get(url, headers={"User-Agent": "Mozilla/5.0 (compatible; AtlasData/1.0)"}, follow_redirects=True)
    except Exception as e:
        return JSONResponse({"error": f"fetch failed: {type(e).__name__}"}, status_code=502)
    if r.status_code != 200:
        return JSONResponse({"error": f"upstream status {r.status_code}", "status": r.status_code}, status_code=502)
    ct = r.headers.get("content-type", "")
    if "text/html" not in ct and "application/xhtml" not in ct and ct:
        return JSONResponse({"error": f"non-HTML content-type {ct.split(';')[0]}", "status": r.status_code}, status_code=422)
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(r.text[:1_500_000], "html.parser")
    for tag in soup(["script", "style", "noscript", "iframe", "svg", "nav", "footer", "header", "form", "aside", "template"]):
        tag.decompose()
    title = (soup.title.string.strip() if soup.title and soup.title.string else "") or None
    desc = None
    md = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
    if md and md.get("content"):
        desc = md["content"].strip()[:500]
    main = soup.find("main") or soup.find("article") or soup.body or soup
    text = main.get_text("\n", strip=True)
    import re as _re
    text = _re.sub(r"\n{3,}", "\n\n", text)[:12288]
    return {"url": str(r.url), "status": r.status_code, "title": title,
            "description": desc, "text": text, "chars": len(text), "ts": int(time.time())}


# ================= L402 (Lightning-paid endpoints) =================
import os as _os, base64 as _b64, secrets as _sec

_LNBITS = "https://demo.lnbits.com"
try:
    _LK = {l.split("=",1)[0]: l.split("=",1)[1].strip() for l in open(_os.path.expanduser("~/.cdp/lnbits.env")) if "=" in l}
except Exception:
    _LK = {}
if not _LK.get("LNBITS_ADMINKEY") and _os.environ.get("LNBITS_ADMINKEY"):
    _LK = {"LNBITS_ADMINKEY": _os.environ["LNBITS_ADMINKEY"], "L402_MACAROON_KEY": _os.environ.get("L402_MACAROON_KEY", "")}
L402_PRICES = {  # sats per endpoint
    "/l402/price/": 10, "/l402/btc/tip": 10, "/l402/gas": 10, "/l402/sol/slot": 10,
    "/l402/btc/fees": 10, "/l402/btc/whales": 10,
    "/l402/report/": 200,
}

def _l402_price(path: str):
    for k, v in L402_PRICES.items():
        if path.startswith(k):
            return v
    return None

async def _lnbits_invoice(sats: int, memo: str):
    r = await _client.post(f"{_LNBITS}/api/v1/payments",
        headers={"X-Api-Key": _LK["LNBITS_ADMINKEY"], "content-type": "application/json"},
        json={"out": False, "amount": sats, "memo": memo, "unit": "sat"})
    return r.json()

async def _lnbits_paid(payment_hash: str):
    r = await _client.get(f"{_LNBITS}/api/v1/payments/{payment_hash}",
        headers={"X-Api-Key": _LK["LNBITS_ADMINKEY"]})
    d = r.json()
    return d if d.get("paid") else None


@app.middleware("http")
async def l402_gate(request: Request, call_next):
    from pymacaroons import Macaroon, Verifier
    path = request.url.path
    if os.environ.get("FREE_MODE"):
        return await call_next(request)
    sats = _l402_price(path)
    if sats is None:
        return await call_next(request)
    auth = request.headers.get("authorization", "")
    if auth.startswith("L402 ") and ":" in auth:
        token, preimage = auth[5:].split(":", 1)
        ph = None
        try:
            m = Macaroon.deserialize(token)
            for cid in m.caveats:
                txt = cid.caveat_id if isinstance(cid.caveat_id, str) else cid.caveat_id.decode()
                if txt.startswith("payment_hash="):
                    ph = txt.split("=", 1)[1]
        except Exception:
            try:
                ph = _b64.b64decode(token).decode()  # legacy plain-token compat
            except Exception:
                ph = None
        if ph:
            paid = await _lnbits_paid(ph)
            if paid and paid.get("preimage") == preimage:
                return await call_next(request)
        return JSONResponse({"error": "invalid or unpaid L402 credentials"}, status_code=402)
    inv = await _lnbits_invoice(sats, f"AtlasData L402 {path}")
    m = Macaroon(location="x402atlas.xyz",
                 identifier=f"atlasdata-{_sec.token_hex(8)}",
                 key=_LK["L402_MACAROON_KEY"])
    m.add_first_party_caveat(f"payment_hash={inv['payment_hash']}")
    m.add_first_party_caveat(f"route={path}")
    token = m.serialize()
    return JSONResponse(
        {"sats": sats, "invoice": inv["bolt11"], "payment_hash": inv["payment_hash"],
         "how": "Pay invoice, then retry with header: Authorization: L402 <macaroon>:<preimage>", "token": token},
        status_code=402,
        headers={"WWW-Authenticate": f'L402 macaroon="{token}", invoice="{inv["bolt11"]}"'},
    )


SYMBOL_ALIASES = {"bitcoin": "BTC", "ethereum": "ETH", "solana": "SOL", "dogecoin": "DOGE"}


@app.middleware("http")
async def param_guard(request: Request, call_next):
    """Reject invalid route params with 404 before any payment middleware asks for money."""
    import re as _re, pathlib as _pl
    p = request.url.path
    m = _re.match(r"^(?:/api|/l402)/price/([A-Za-z0-9_-]+)$", p)
    if m:
        sym = SYMBOL_ALIASES.get(m.group(1).lower(), m.group(1)).upper()
        r = await _client.get(f"{COINBASE_API}/prices/{sym}-USD/spot")
        if r.status_code != 200:
            return JSONResponse({"error": f"unknown symbol {m.group(1)}"}, status_code=404)
        return await call_next(request)
    checks = [
        (r"^/api/(?:wallet|token)/((?!verdict(?:-lite)?$).+)$", r"^0x[0-9a-fA-F]{40}$"),
        (r"^/api/tx/base/(.+)$", r"^0x[0-9a-fA-F]{64}$"),
        (r"^/api/block/base/(.+)$", r"^(latest|pending|earliest|[0-9]+)$"),
        (r"^/api/dex/token/(.+)$", r"^[1-9A-HJ-NP-Za-km-z0-9]{32,44}$|^0x[0-9a-fA-F]{40}$"),
        (r"^/api/dex/pair/([a-z]+)/(.+)$", r"^[1-9A-HJ-NP-Za-km-z0-9]{32,44}$|^0x[0-9a-fA-F]{40}$"),
        (r"^/api/ticker/([A-Za-z0-9_-]+)$", r"^[A-Z0-9]{1,12}$"),
        (r"^/api/exchange/([A-Za-z0-9_-]+)$", r"^[A-Z0-9]{1,12}-[A-Z0-9]{1,12}$"),
    ]
    for pat, good in checks:
        m = _re.match(pat, p)
        if m:
            param = m.group(2) if m.lastindex == 2 else m.group(1)
            if not _re.match(good, param):
                return JSONResponse({"error": f"invalid parameter {param}"}, status_code=404)
            return await call_next(request)
    m = _re.match(r"^(?:/api|/l402)/report/(.+)$", p)
    if m and not (_pl.Path(__file__).parent / "reports" / f"{m.group(1)}.md").exists():
        return JSONResponse({"error": f"unknown report {m.group(1)}"}, status_code=404)
    return await call_next(request)


@app.get("/free/price/{symbol}")
async def free_price(symbol: str):
    """Free sample  -  same data as /l402/price/{symbol} (10 sats) and /api/price/{symbol} ($0.001)."""
    sym = SYMBOL_ALIASES.get(symbol.lower(), symbol).upper()
    r = await _client.get(f"{COINBASE_API}/prices/{sym}-USD/spot")
    if r.status_code != 200:
        return JSONResponse({"error": f"unknown symbol {symbol}"}, status_code=404)
    return {"symbol": sym, "price_usd": r.json()["data"]["amount"], "ts": int(time.time()),
            "sample": True, "paid_versions": {"x402_usdc": "/api/price/" + sym, "l402_sats": "/l402/price/" + sym}}


@app.get("/l402/price/{symbol}")
async def l402_price(symbol: str):
    sym = SYMBOL_ALIASES.get(symbol.lower(), symbol).upper()
    r = await _client.get(f"{COINBASE_API}/prices/{sym}-USD/spot")
    if r.status_code != 200:
        return JSONResponse({"error": f"unknown symbol {symbol}"}, status_code=404)
    return {"symbol": sym, "price_usd": r.json()["data"]["amount"], "ts": int(time.time())}


@app.get("/l402/btc/tip")
async def l402_btc():
    return await btc_tip()


@app.get("/l402/gas")
async def l402_gas():
    return await gas()


@app.get("/l402/sol/slot")
async def l402_slot():
    return await sol_slot()


@app.get("/l402/btc/fees")
async def l402_btc_fees():
    r = await _client.get("https://mempool.space/api/v1/fees/recommended")
    if r.status_code != 200:
        return JSONResponse({"error": "upstream"}, status_code=502)
    return {**r.json(), "unit": "sat/vB", "sats_paid": 10, "ts": int(time.time())}


@app.get("/l402/report/{slug}")
async def l402_report(slug: str):
    import re as _re
    if not _re.match(r"^[a-z0-9-]{1,64}$", slug):
        return JSONResponse({"error": "invalid report slug"}, status_code=404)
    f = Path(__file__).parent / "reports" / f"{slug}.md"
    if not f.exists():
        return JSONResponse({"error": f"unknown report {slug}"}, status_code=404)
    return {"report": slug, "sats_paid": 200, "markdown": f.read_text()}


@app.get("/api/market/snapshot")
async def market_snapshot():
    out = {"ts": int(time.time()), "prices": {}, "tickers": {}}
    for sym in ("BTC", "ETH", "SOL"):
        try:
            r = await _client.get(f"{COINBASE_API}/prices/{sym}-USD/spot")
            out["prices"][sym] = r.json()["data"]["amount"]
        except Exception:
            pass
        try:
            t = await _client.get(f"https://api.exchange.coinbase.com/products/{sym}-USD/ticker")
            if t.status_code == 200:
                d = t.json()
                out["tickers"][sym] = {"price": d.get("price"), "volume_24h": d.get("volume"),
                                       "high_24h": d.get("high"), "low_24h": d.get("low")}
        except Exception:
            pass
    try:
        h = await _client.get("https://mempool.space/api/blocks/tip/height")
        out["btc_tip_height"] = int(h.text)
    except Exception:
        pass
    try:
        gp = await rpc("eth_gasPrice", [])
        out["base_gas_gwei"] = str(int(gp, 16) / 1e9)
    except Exception:
        pass
    return out


@app.get("/api/feargreed")
async def feargreed():
    r = await _client.get("https://api.alternative.me/fng/?limit=1")
    d = r.json()["data"][0]
    return {"value": d["value"], "classification": d["value_classification"],
            "timestamp": int(d["timestamp"]), "ts": int(time.time())}


@app.get("/api/dex/token/{address}")
async def dex_token(address: str):
    r = await _client.get(f"https://api.dexscreener.com/latest/dex/tokens/{address}")
    pairs = r.json().get("pairs") or []
    return {"address": address, "pair_count": len(pairs), "pairs": pairs[:10], "ts": int(time.time())}


@app.get("/api/dex/pair/{chain}/{pair}")
async def dex_pair(chain: str, pair: str):
    r = await _client.get(f"https://api.dexscreener.com/latest/dex/pairs/{chain}/{pair}")
    return {"chain": chain, "pair": pair, "data": r.json().get("pair"), "ts": int(time.time())}


_HOST_DIR = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "hosted")
_os.makedirs(_HOST_DIR, exist_ok=True)


@app.get("/api/host/{data}")
async def host_file(data: str, name: str = "file"):
    raw = _b64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    if len(raw) > 32 * 1024:
        return JSONResponse({"error": "max 32KB"}, status_code=400)
    fid = hashlib.sha256(raw).hexdigest()[:16]
    with open(_os.path.join(_HOST_DIR, fid), "wb") as f:
        f.write(raw)
    return {"id": fid, "url": f"/files/{fid}", "name": name, "bytes": len(raw), "ts": int(time.time())}


@app.get("/files/{fid}")
async def serve_file(fid: str):
    fp = _os.path.join(_HOST_DIR, fid)
    if not _os.path.isfile(fp):
        return JSONResponse({"error": "not found"}, status_code=404)
    from fastapi.responses import Response
    return Response(open(fp, "rb").read(), media_type="application/octet-stream")


# ================= Hal marketplace adapter =================
# Hal proxies paid calls here: GET = health check (every 30s), POST = paid call.
# The provider key configured on the Hal listing gates the POST  -  without it
# (or before it's set) the endpoint still answers health but returns data only
# when the key matches or is unset.
def _hal_key():
    try:
        for l in open(_os.path.expanduser("~/.cdp/hal.env")):
            if l.startswith("HAL_PROVIDER_KEY="):
                return l.split("=", 1)[1].strip()
    except Exception:
        pass
    return _os.environ.get("HAL_PROVIDER_KEY")


def _hal_payload():
    return {
        "service": "AtlasData market snapshot",
        "data": {
            "note": "Real-time snapshot assembled per call. Symbols covered: btc, eth, sol, xmr + gas + mempool fees.",
        },
        "source": "x402atlas.xyz",
        "ts": int(time.time()),
    }


@app.get("/health", include_in_schema=False)
async def hal_health():
    return {"status": "ok", "service": "atlasdata", "ts": int(time.time())}


@app.api_route("/hal/snapshot", methods=["GET", "POST"], include_in_schema=False)
async def hal_snapshot(request: Request):
    if request.method == "GET":
        return {"status": "ok", "service": "atlasdata", "ts": int(time.time())}
    key = _hal_key()
    if key:
        provided = (request.headers.get("x-hal-provider-key") or
                    request.headers.get("x-provider-key") or
                    request.headers.get("authorization", "").replace("Bearer ", "") or "")
        if not provided:
            try:
                body = await request.json()
                provided = body.get("provider_key") or body.get("key") or ""
            except Exception:
                provided = ""
        if provided != key:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
    p = await price("btc")
    e = await price("eth")
    s = await price("sol")
    try:
        f = await _client.get("https://api.alternative.me/fng/?limit=1")
        fng = f.json()["data"][0]
    except Exception:
        fng = None
    try:
        fees = await _client.get("https://mempool.space/api/v1/fees/recommended")
        fee = fees.json()
    except Exception:
        fee = None
    return {"btc": p, "eth": e, "sol": s, "fear_greed": fng, "mempool_fees": fee, "ts": int(time.time())}

@app.post("/webhook/licium", include_in_schema=False)
async def licium_webhook(request: Request):
    try:
        body = await request.body()
        ts = int(time.time())
        os.makedirs("webhooks", exist_ok=True)
        with open(f"webhooks/licium_{ts}.json", "wb") as f:
            f.write(body)
    except Exception:
        pass
    return {"received": True}

PAYAPI_TOKEN = "atlasdata-payapi-verify-9f4c2e"

@app.get("/.well-known/payapi-verify", include_in_schema=False)
async def payapi_verify():
    return PlainTextResponse(PAYAPI_TOKEN)

@app.get("/payapi-verify", include_in_schema=False)
async def payapi_verify2():
    return {"operator": "atlasdata", "token": PAYAPI_TOKEN, "endpoints": ["x402 Payment Debugger", "AtlasData"]}


# --- text utility endpoints (L402-paywalled via zapads-proxy; free in FREE_MODE) ---
import hashlib, base64 as _b64, re as _re, uuid as _uuid, unicodedata


def _clip(text: str, n: int = 5000):
    return (text or "")[:n]


@app.get("/api/text/wordcount")
async def text_wordcount(text: str = ""):
    t = _clip(text)
    return {"chars": len(t), "words": len(t.split()), "lines": t.count("\n") + (1 if t else 0)}


@app.get("/api/text/hash")
async def text_hash(text: str = "", algo: str = "sha256"):
    t = _clip(text)
    if algo not in ("md5", "sha1", "sha256", "sha512"):
        return JSONResponse({"error": "algo must be md5|sha1|sha256|sha512"}, status_code=400)
    return {"algo": algo, "digest": hashlib.new(algo, t.encode()).hexdigest()}


@app.get("/api/text/base64")
async def text_base64(text: str = "", mode: str = "encode"):
    t = _clip(text)
    try:
        if mode == "encode":
            return {"mode": mode, "result": _b64.b64encode(t.encode()).decode()}
        if mode == "decode":
            return {"mode": mode, "result": _b64.b64decode(t.encode()).decode("utf-8", errors="replace")}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse({"error": "mode must be encode|decode"}, status_code=400)


def _validate_public_url(url: str):
    import socket
    import ipaddress
    from urllib.parse import urlparse
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("url must be public http(s)")
    host = parsed.hostname.lower()
    if host in ("localhost", "localhost.localdomain") or host.endswith((".local", ".internal")):
        raise ValueError("local hostnames are not allowed")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved:
            raise ValueError("private or reserved network addresses are not allowed")
    return parsed


@app.get("/api/web/read")
async def web_read(url: str, max_chars: int = 12000):
    from urllib.parse import urljoin
    from bs4 import BeautifulSoup
    max_chars = max(500, min(int(max_chars or 12000), 20000))
    current = (url or "").strip()
    try:
        r = None
        for _ in range(4):
            _validate_public_url(current)
            r = await _client.get(current, timeout=20, follow_redirects=False,
                                  headers={"User-Agent": "AtlasDataWebRead/1.0"})
            if r.is_redirect and r.headers.get("location"):
                current = urljoin(current, r.headers["location"])
                continue
            break
        if r is None:
            return JSONResponse({"error": "fetch failed"}, status_code=502)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": "fetch failed", "detail": str(e)[:120]}, status_code=502)
    if r.status_code >= 400:
        return JSONResponse({"error": "upstream status", "status": r.status_code, "final_url": current}, status_code=502)
    ctype = r.headers.get("content-type", "")
    if "html" not in ctype:
        text = r.text[:max_chars]
        return {"url": url, "final_url": current, "status": r.status_code, "content_type": ctype,
                "title": "", "text": text, "chars": len(text), "ts": int(time.time())}
    soup = BeautifulSoup(r.text, "html.parser")
    title = (soup.title.string.strip() if soup.title and soup.title.string else "")[:300]
    for tag in soup(["script", "style", "noscript", "svg", "form", "nav", "footer", "header", "aside"]):
        tag.decompose()
    lines = [line.strip() for line in soup.get_text("\n").splitlines()]
    text = "\n".join(line for line in lines if line)[:max_chars]
    return {"url": url, "final_url": current, "status": r.status_code, "content_type": ctype,
            "title": title, "text": text, "chars": len(text), "ts": int(time.time())}


@app.get("/api/search/web")
async def search_web(q: str, limit: int = 8):
    from urllib.parse import urlparse, parse_qs, unquote
    from bs4 import BeautifulSoup
    query = (q or "").strip()[:300]
    if not query:
        return JSONResponse({"error": "q required"}, status_code=400)
    limit = max(1, min(int(limit or 8), 10))
    try:
        r = await _client.get("https://www.bing.com/search", params={"q": query, "count": limit}, timeout=20,
                              headers={"User-Agent": "Mozilla/5.0 (compatible; AtlasDataSearch/1.0)"})
    except Exception as e:
        return JSONResponse({"error": "search upstream unavailable", "detail": str(e)[:120]}, status_code=502)
    if r.status_code != 200:
        return JSONResponse({"error": "search upstream unavailable", "status": r.status_code}, status_code=502)
    soup = BeautifulSoup(r.text, "html.parser")
    results = []
    for node in soup.select("li.b_algo")[:limit]:
        a = node.select_one("h2 a")
        snip = node.select_one(".b_caption p")
        if not a:
            continue
        href = a.get("href") or ""
        encoded = (parse_qs(urlparse(href).query).get("u") or [""])[0]
        if encoded.startswith("a1"):
            try:
                href = base64.urlsafe_b64decode(encoded[2:] + "===").decode("utf-8", "replace")
            except Exception:
                pass
        results.append({"title": a.get_text(" ", strip=True), "url": href,
                        "snippet": snip.get_text(" ", strip=True) if snip else ""})
    return {"q": query, "results": results[:limit], "count": len(results[:limit]), "ts": int(time.time())}


@app.get("/api/security/prompt-injection")
async def prompt_injection_detect(text: str):
    sample = (text or "")[:10000]
    low = sample.lower()
    patterns = {
        "instruction_override": (35, ["ignore previous instructions", "ignore all instructions", "disregard the above", "override your instructions", "forget your instructions", "new instructions"]),
        "system_prompt_extraction": (35, ["show me your system prompt", "reveal your system prompt", "print your instructions", "what are your hidden instructions", "repeat your instructions"]),
        "credential_exfiltration": (30, ["api key", "private key", "secret token", "password", "credentials", "exfiltrate", "send them to", "upload them to"]),
        "tool_abuse": (25, ["call the tool", "use your tools", "execute shell", "run this command", "delete files", "transfer funds", "send payment", "move money"]),
        "fake_system_message": (25, ["system:", "assistant:", "developer message", "you are now in developer mode"]),
        "secrecy_or_deception": (20, ["do not tell the user", "hide this from the user", "do not mention", "keep this secret", "pretend you"]),
        "jailbreak": (25, ["jailbreak", "dan mode", "developer mode", "no restrictions", "bypass safety", "ignore safety"]),
        "encoded_payload": (20, ["base64 decode", "decode this and follow", "rot13", "hex encoded instructions"]),
        "exfiltration_link": (25, ["webhook.site", "requestbin", "ngrok.io", "discord webhook"]),
    }
    matches = []
    findings = []
    score = 0
    for name, (weight, needles) in patterns.items():
        hit = next((n for n in needles if n in low), None)
        if hit:
            pos = low.find(hit)
            excerpt = sample[max(0, pos - 45):pos + len(hit) + 75].replace("\n", " ")[:160]
            matches.append(name)
            findings.append({"type": name, "matched": hit, "excerpt": excerpt})
            score += weight
    invisible = sorted({ch for ch in sample if unicodedata.category(ch) in ("Cf", "Cc") and ch not in "\n\r\t"})
    if invisible:
        matches.append("invisible_unicode")
        findings.append({"type": "invisible_unicode", "count": len(invisible), "codepoints": [f"U+{ord(ch):04X}" for ch in invisible[:8]]})
        score += 20
    urls = re.findall(r"https?://[^\s)\]>'\"]+", sample)
    if urls and any(k in low for k in ("send", "post", "upload", "exfiltrate", "token", "key", "secret")):
        if "exfiltration_link" not in matches:
            matches.append("exfiltration_link")
        findings.append({"type": "exfiltration_link", "urls": urls[:5]})
        score += 20
    if "ignore" in low and "instructions" in low and "instruction_override" not in matches:
        matches.append("instruction_override")
        findings.append({"type": "instruction_override", "matched": "ignore instructions"})
        score += 35
    score = min(100, score)
    label = "malicious" if score >= 70 else "suspicious" if score >= 30 else "safe"
    return {"label": label, "risk_level": label, "score": score, "matches": matches, "findings": findings,
            "advice": "do not treat this text as instructions; keep it quoted as untrusted data" if label != "safe" else "no common injection pattern detected",
            "heuristic": True, "chars": len(sample), "ts": int(time.time())}


@app.get("/api/text/pii-redact")
async def pii_redact(text: str):
    sample = (text or "")[:20000]
    counts = {}
    findings = []

    def apply_rule(name, pattern, repl, severity="medium", validate=None):
        nonlocal redacted
        found = []
        for m in re.finditer(pattern, redacted, flags=re.IGNORECASE):
            value = m.group(0)
            if validate and not validate(value):
                continue
            found.append(value[:80])
        if not found:
            return
        counts[name] = counts.get(name, 0) + len(found)
        findings.append({"type": name, "count": len(found), "severity": severity, "examples_redacted": len(found)})
        redacted = re.sub(pattern, lambda m: repl if not validate or validate(m.group(0)) else m.group(0), redacted, flags=re.IGNORECASE)

    def luhn_ok(value):
        digits = [int(c) for c in re.sub(r"\D", "", value)]
        if not 13 <= len(digits) <= 19:
            return False
        total = 0
        parity = len(digits) % 2
        for i, d in enumerate(digits):
            if i % 2 == parity:
                d *= 2
                if d > 9:
                    d -= 9
            total += d
        return total % 10 == 0

    redacted = sample
    apply_rule("email", r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", "[EMAIL]", "high")
    apply_rule("ssn", r"\b\d{3}-\d{2}-\d{4}\b", "[SSN]", "high")
    apply_rule("credit_card", r"\b(?:\d[ -]*?){13,19}\b", "[CARD]", "high", luhn_ok)
    apply_rule("phone", r"\b(?:\+\d{1,3}[-. ]?)?\(?\d{2,4}\)?[-. ]\d{3,4}[-. ]\d{4}\b", "[PHONE]", "medium")
    apply_rule("ipv4", r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "[IP]", "low")
    apply_rule("jwt", r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b", "[JWT]", "high")
    apply_rule("aws_access_key", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b", "[AWS_KEY]", "high")
    apply_rule("bearer_token", r"\bBearer\s+[A-Za-z0-9._~+/-]{12,}={0,2}", "Bearer [TOKEN]", "high")
    apply_rule("private_key_hex", r"\b0x?[0-9a-fA-F]{64}\b", "[PRIVATE_KEY]", "high")
    apply_rule("iban", r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b", "[IBAN]", "medium")
    apply_rule("street_address", r"\b\d{1,6}\s+[A-Za-z0-9 .'-]{2,40}\s+(?:Street|St|Road|Rd|Avenue|Ave|Boulevard|Blvd|Lane|Ln|Drive|Dr)\b", "[ADDRESS]", "medium")
    counts = {k: v for k, v in counts.items() if v}
    high = sum(v for k, v in counts.items() if k in ("email", "ssn", "credit_card", "jwt", "aws_access_key", "bearer_token", "private_key_hex"))
    return {"redacted": redacted, "counts": counts, "findings": findings,
            "pii_found": bool(counts), "secrets_found": high > 0,
            "risk_level": "high" if high else "medium" if counts else "low",
            "chars": len(sample), "ts": int(time.time())}


@app.get("/api/text/uuid")
async def text_uuid():
    return {"uuid": str(_uuid.uuid4())}


@app.get("/api/text/epoch")
async def text_epoch():
    return {"epoch": int(time.time()), "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


@app.get("/api/text/slug")
async def text_slug(text: str = ""):
    t = unicodedata.normalize("NFKD", _clip(text)).encode("ascii", "ignore").decode()
    slug = _re.sub(r"[^a-z0-9]+", "-", t.lower()).strip("-")
    return {"slug": slug}


@app.get("/api/text/json-validate")
async def text_json_validate(text: str = ""):
    import json as _j
    try:
        _j.loads(_clip(text, 50000))
        return {"valid": True}
    except Exception as e:
        return {"valid": False, "error": str(e)}


@app.get("/api/text/reverse")
async def text_reverse(text: str = ""):
    return {"result": _clip(text)[::-1]}


@app.get("/api/x402/preflight")
async def x402_preflight(url: str = ""):
    """Probe a URL for an x402 paywall: challenge version, networks, payTo, freshness."""
    target = _clip(url, 500).strip()
    if not target:
        return JSONResponse({"error": "url param required"}, status_code=400)
    if not target.startswith("http"):
        target = "https://" + target
    out = {"url": target, "reachable": False, "x402": False}
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as c:
            r = await c.get(target)
            out["status"] = r.status_code
            out["reachable"] = True
            hdr = r.headers.get("payment-required") or r.headers.get("PAYMENT-REQUIRED")
            body = {}
            try:
                body = r.json()
            except Exception:
                body = {"_text": r.text[:500]}
            if r.status_code == 402:
                out["x402"] = True
                ver = body.get("x402Version")
                out["x402_version"] = ver
                accepts = body.get("accepts") or []
                out["accepts_count"] = len(accepts)
                nets, pays, amts, float_amounts = set(), set(), [], []
                for a in accepts:
                    if a.get("network"):
                        nets.add(a["network"])
                    if a.get("payTo"):
                        pays.add(a["payTo"])
                    amt = a.get("maxAmountRequired") or a.get("amount")
                    if amt:
                        amts.append(amt)
                        if "." in str(amt):
                            float_amounts.append(str(amt))
                out["networks"] = sorted(nets)
                out["pay_to"] = sorted(pays)
                out["amounts"] = amts
                if hdr:
                    out["header_terms"] = True
                    try:
                        decoded = _b64.b64decode(hdr).decode()
                        out["header_resource"] = json.loads(decoded).get("resource", {}).get("url")
                    except Exception:
                        out["header_terms"] = "unparseable"
                flags = []
                if float_amounts:
                    flags.append({"type": "float_amounts", "amounts": float_amounts,
                                  "note": "decimal-notation amounts are ambiguous (2.1% of listings); most clients expect integer base units  -  verify the intended unit before paying"})
                if accepts and not amts:
                    flags.append({"type": "no_amount_fields",
                                  "note": "accepts carry no amount/maxAmountRequired  -  price is stated only in the description text; verify before paying"})
                if flags:
                    out["encoding_flags"] = flags
                    out["encoding_flag"] = flags[0]  # backward compat
                desc = (body.get("resource") or {}).get("description") or (accepts[0].get("description") if accepts else "")
                out["description"] = desc
                out["safe"] = bool(accepts and pays and target.startswith("https") and not float_amounts)
            else:
                out["note"] = "no x402 challenge (status != 402)"
    except Exception as e:
        out["error"] = str(e)[:200]
    out["_cross_sell"] = {
        "msg": "AtlasData preflight is free. Related paid tools:",
        "also_try": [
            {"path": "/api/report/x402-monetization", "price": "x402 USDC", "why": "full x402 monetization playbook"},
            {"path": "/api/report/x402-server-implementation", "price": "x402 USDC", "why": "how to ship your own x402 server"},
            {"path": "/api/report/agent-micropayments-market", "price": "x402 USDC", "why": "agent-economy market map"},
        ],
        "contact": "https://atlasdata.surge.sh",
    }
    return out


# --- chain-read primitives (onesource-validated demand: 16k payers buy paid RPC passthrough) ---


def _hex_addr(a):
    return re.fullmatch(r"0x[0-9a-fA-F]{40}", a or "")


@app.get("/api/chain/balance/{address}")
async def chain_balance(address: str):
    if not _hex_addr(address):
        return JSONResponse({"error": "invalid address"}, status_code=400)
    wei = int((await rpc("eth_getBalance", [address, "latest"])) or "0x0", 16)
    n = int((await rpc("eth_getTransactionCount", [address, "latest"])) or "0x0", 16)
    return {"address": address, "wei": wei, "eth": wei / 1e18, "tx_count": n, "chain": "base"}


@app.get("/api/chain/erc20-balance/{token}/{address}")
async def chain_erc20_balance(token: str, address: str):
    if not (_hex_addr(token) and _hex_addr(address)):
        return JSONResponse({"error": "invalid token/address"}, status_code=400)
    data = "0x70a08231" + "0" * 24 + address[2:].lower()
    raw = int((await _eth_call(token, data)) or "0x0", 16)
    dec_hex = await _eth_call(token, "0x313ce567")
    dec = int(dec_hex, 16) if dec_hex else None
    return {"token": token, "address": address, "raw": raw,
            "balance": (raw / 10 ** dec) if dec is not None else None, "chain": "base"}


@app.get("/api/chain/block-number")
async def chain_block_number():
    b = await rpc("eth_blockNumber", [])
    return {"block": int(b or "0x0", 16), "hex": b, "chain": "base"}


@app.get("/api/chain/nonce/{address}")
async def chain_nonce(address: str):
    if not _hex_addr(address):
        return JSONResponse({"error": "invalid address"}, status_code=400)
    n = int((await rpc("eth_getTransactionCount", [address, "latest"])) or "0x0", 16)
    return {"address": address, "nonce": n, "chain": "base"}


@app.get("/api/chain/contract/{address}")
async def chain_contract(address: str):
    if not _hex_addr(address):
        return JSONResponse({"error": "invalid address"}, status_code=400)
    code = await rpc("eth_getCode", [address, "latest"])
    size = max(0, (len(code or "0x") - 2) // 2)
    return {"address": address, "is_contract": size > 0, "code_bytes": size, "chain": "base"}


@app.get("/api/chain/total-supply/{token}")
async def chain_total_supply(token: str):
    if not _hex_addr(token):
        return JSONResponse({"error": "invalid token"}, status_code=400)
    sup = int((await _eth_call(token, "0x18160ddd")) or "0x0", 16)
    dec_hex = await _eth_call(token, "0x313ce567")
    dec = int(dec_hex, 16) if dec_hex else None
    return {"token": token, "total_supply_raw": sup, "decimals": dec,
            "supply": (sup / 10 ** dec) if dec is not None else None, "chain": "base"}


@app.get("/api/chain/network-info")
async def chain_network_info():
    cid = int((await rpc("eth_chainId", [])) or "0x0", 16)
    blk = int((await rpc("eth_blockNumber", [])) or "0x0", 16)
    gp = int((await rpc("eth_gasPrice", [])) or "0x0", 16)
    return {"chain_id": cid, "block": blk, "gas_price_wei": gp, "gas_price_gwei": gp / 1e9, "chain": "base"}


def _hex_hash(h: str) -> bool:
    return bool(re.fullmatch(r"0x[0-9a-fA-F]{64}", h or ""))


@app.get("/api/chain/tx/{tx_hash}")
async def chain_tx(tx_hash: str):
    if not _hex_hash(tx_hash):
        return JSONResponse({"error": "invalid tx hash"}, status_code=400)
    tx = await rpc("eth_getTransactionByHash", [tx_hash])
    if not tx:
        return JSONResponse({"error": "not found"}, status_code=404)
    rc = await rpc("eth_getTransactionReceipt", [tx_hash]) or {}
    return {"hash": tx_hash, "from": tx.get("from"), "to": tx.get("to"),
            "value_wei": int(tx.get("value") or "0x0", 16),
            "block": int(tx.get("blockNumber") or "0x0", 16) or None,
            "status": {"0x1": "success", "0x0": "failed"}.get(rc.get("status"), "pending"),
            "gas_used": int(rc.get("gasUsed") or "0x0", 16) or None,
            "chain": "base"}


@app.get("/api/chain/block/{number}")
async def chain_block(number: str):
    tag = number if number == "latest" else hex(int(number)) if number.isdigit() else None
    if tag is None:
        return JSONResponse({"error": "invalid block number"}, status_code=400)
    b = await rpc("eth_getBlockByNumber", [tag, False])
    if not b:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"number": int(b["number"], 16), "timestamp": int(b["timestamp"], 16),
            "tx_count": len(b.get("transactions") or []),
            "gas_used": int(b.get("gasUsed") or "0x0", 16), "miner": b.get("miner"), "chain": "base"}


_TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


@app.get("/api/chain/erc20-transfers/{token}/{address}")
async def chain_erc20_transfers(token: str, address: str):
    if not (_hex_addr(token) and _hex_addr(address)):
        return JSONResponse({"error": "invalid token or address"}, status_code=400)

    latest = int((await rpc("eth_blockNumber", [])) or "0x0", 16)
    frm = hex(max(0, latest - 9000))
    pad = "0x" + address.lower().replace("0x", "").rjust(64, "0")
    out = []
    for topic_pos, flt in ((1, {"fromBlock": frm, "toBlock": "latest", "address": token,
                                 "topics": [_TRANSFER_TOPIC, pad]}),
                           (2, {"fromBlock": frm, "toBlock": "latest", "address": token,
                                 "topics": [_TRANSFER_TOPIC, None, pad]})):
        try:
            for lg in (await rpc("eth_getLogs", [flt])) or []:
                topics = lg.get("topics") or []
                out.append({"from": "0x" + topics[1][-40:] if len(topics) > 1 else None,
                            "to": "0x" + topics[2][-40:] if len(topics) > 2 else None,
                            "raw": int(lg.get("data") or "0x0", 16),
                            "block": int(lg.get("blockNumber") or "0x0", 16),
                            "tx": lg.get("transactionHash")})
        except Exception:
            pass
    out.sort(key=lambda x: -x["block"])
    return {"token": token, "address": address, "transfers": out[:25], "chain": "base",
            "scanned_from_block": int(frm, 16)}


_MAJOR_TOKENS = {
    "USDC": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    "USDT": "0xfde4C96c8593536E564Fce27D9F98B19922eaaCd",
    "WETH": "0x4200000000000000000000000000000000000006",
    "DAI": "0x50c5725949A6F0c72E6C4a641F24049A917DB0Cb",
    "cbBTC": "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf",
}


@app.get("/api/chain/wallet-report/{address}")
async def chain_wallet_report(address: str):
    if not _hex_addr(address):
        return JSONResponse({"error": "invalid address"}, status_code=400)
    bal, nonce, code, latest = await asyncio.gather(
        rpc("eth_getBalance", [address, "latest"]),
        rpc("eth_getTransactionCount", [address, "latest"]),
        rpc("eth_getCode", [address, "latest"]),
        rpc("eth_blockNumber", []),
    )
    wei = int(bal or "0x0", 16)
    frm = hex(max(0, int(latest or "0x0", 16) - 9000))
    pad = "0x" + address.lower().replace("0x", "").rjust(64, "0")

    async def token_count(token: str) -> int:
        n = 0
        for flt in ({"fromBlock": frm, "toBlock": "latest", "address": token,
                     "topics": [_TRANSFER_TOPIC, pad]},
                    {"fromBlock": frm, "toBlock": "latest", "address": token,
                     "topics": [_TRANSFER_TOPIC, None, pad]}):
            try:
                n += len((await rpc("eth_getLogs", [flt])) or [])
            except Exception:
                pass
        return n

    counts = await asyncio.gather(*(token_count(t) for t in _MAJOR_TOKENS.values()))
    return {
        "address": address,
        "eth": wei / 1e18,
        "wei": wei,
        "tx_count": int(nonce or "0x0", 16),
        "is_contract": len(code or "0x") > 2,
        "recent_token_activity": dict(zip(_MAJOR_TOKENS, counts)),
        "scanned_from_block": int(frm, 16),
        "chain": "base",
    }


_DISCOVER_ROWS = None


def _discover_rows():
    global _DISCOVER_ROWS
    if _DISCOVER_ROWS is None:
        rows = []
        try:
            with open(os.path.join(os.path.dirname(__file__), "data", "x402_discover.jsonl")) as f:
                for line in f:
                    rows.append(json.loads(line))
        except Exception:
            rows = []
        _DISCOVER_ROWS = rows
    return _DISCOVER_ROWS


@app.get("/api/x402/discover")
async def x402_discover(q: str = "", tag: str = "", net: str = "", min_calls: int = 0, limit: int = 10):
    """Ranked x402 catalog search by real 30d demand (calls + unique payers)."""
    q, tag, net = _clip(q, 100).lower(), _clip(tag, 60).lower(), _clip(net, 60).lower()
    limit = max(1, min(int(limit or 10), 50))
    hits = []
    for r in _discover_rows():
        if min_calls and r["c"] < min_calls:
            continue
        if net and net not in r["net"].lower():
            continue
        if tag and tag not in [t.lower() for t in r["t"]]:
            continue
        if q and q not in (r["r"] + " " + r["d"] + " " + r["sn"]).lower():
            continue
        hits.append(r)
    hits.sort(key=lambda r: (r["c"], r["p"]), reverse=True)
    import datetime as _dt
    now = _dt.datetime.utcnow()

    def _flags(r):
        f = []
        lc = r.get("lc") or ""
        try:
            age_h = (now - _dt.datetime.fromisoformat(lc.replace("Z", ""))).total_seconds() / 3600
            if age_h <= 24:
                f.append("ACTIVE_24H")
            elif age_h > 168:
                f.append("DORMANT_7D")
        except Exception:
            f.append("NEVER_CALLED")
        if r["c"] == 0:
            f.append("ZERO_CALLS_30D")
        if r["p"] <= 1:
            f.append("SINGLE_PAYER")
        if not r["sn"]:
            f.append("UNNAMED_SERVICE")
        if not r["d"]:
            f.append("NO_DESCRIPTION")
        if r["cur"]:
            f.append("CDP_CURATED")
        return f

    return {
        "total": len(hits),
        "results": [
            {"resource": r["r"], "service": r["sn"], "description": r["d"], "calls_30d": r["c"],
             "payers_30d": r["p"], "price": r["amt"], "network": r["net"], "curated": r["cur"],
             "tags": r["t"], "last_called_at": r.get("lc") or None, "risk_flags": _flags(r)}
            for r in hits[:limit]
        ],
        "snapshot": "2026-11-24 CDP Bazaar catalog (41,984 listings)",
        "risk_flag_legend": {
            "ACTIVE_24H": "called within the last 24h (live cohort: 4.5% of listings, 81% of demand)",
            "DORMANT_7D": "no call in >7 days",
            "NEVER_CALLED": "no recorded call",
            "ZERO_CALLS_30D": "zero calls in 30d",
            "SINGLE_PAYER": "one or zero unique payers  -  single-client dependency",
            "UNNAMED_SERVICE": "no serviceName declared",
            "NO_DESCRIPTION": "no description in listing",
            "CDP_CURATED": "on CDP's curated roster",
        },
    }


@app.post("/api/x402/discover")
async def x402_discover_post(request: Request):
    """Same demand-ranked catalog search, JSON body filters."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    out = await x402_discover(
        q=str(body.get("q", "")),
        tag=str(body.get("tag", "")),
        net=str(body.get("net", "") or " ".join(body.get("networks") or [])),
        min_calls=int(body.get("min_calls") or 0),
        limit=int(body.get("limit") or 10),
    )
    if body.get("curated_only"):
        out["results"] = [r for r in out["results"] if r.get("curated")]
        out["total"] = len(out["results"])
    return out


@app.get("/api/x402/pulse")
async def x402_pulse():
    """Daily x402 market pulse computed from the latest catalog snapshot."""
    import statistics
    from collections import Counter
    rows = _discover_rows()
    prices = []
    for r in rows:
        try:
            prices.append(int(r["amt"]) / 1e6)
        except Exception:
            pass
    top = sorted(rows, key=lambda r: (r["c"], r["p"]), reverse=True)[:20]
    nets = Counter(r["net"] for r in rows)
    return {
        "snapshot": "2026-11-24 CDP Bazaar catalog",
        "total_listings": len(rows),
        "total_calls_30d": sum(r["c"] for r in rows),
        "total_payers_30d": sum(r["p"] for r in rows),
        "curated_count": sum(1 for r in rows if r["cur"]),
        "median_price_usd": round(statistics.median(prices), 4) if prices else None,
        "median_calls_per_listing": statistics.median([r["c"] for r in rows]) if rows else None,
        "top_networks": nets.most_common(8),
        "leaderboard": [
            {"resource": r["r"], "service": r["sn"], "calls_30d": r["c"], "payers_30d": r["p"],
             "price": r["amt"], "network": r["net"], "curated": r["cur"]}
            for r in top
        ],
    }


async def _recommend(need: str, net: str, limit: int, max_price: float):
    need = _clip(need, 160).lower()
    net = _clip(net, 40).lower()
    limit = max(1, min(int(limit or 3), 10))
    words = {w for w in need.replace("/", " ").replace("-", " ").split() if len(w) > 2}
    import datetime as _dt
    now = _dt.datetime.utcnow()
    scored = []
    for r in _discover_rows():
        if not r["c"]:
            continue
        try:
            price = int(r["amt"]) / 1e6
        except Exception:
            continue
        if max_price and price > max_price:
            continue
        if net and net not in r["net"].lower():
            continue
        hay = (r["r"] + " " + r["d"] + " " + r["sn"] + " " + " ".join(r["t"])).lower()
        kw = sum(1 for w in words if w in hay)
        if words and not kw:
            continue
        lc = r.get("lc") or ""
        age_h = None
        try:
            age_h = (now - _dt.datetime.fromisoformat(lc.replace("Z", ""))).total_seconds() / 3600
            live = 1.0 if age_h <= 24 else 0.6 if age_h <= 168 else 0.3
        except Exception:
            live = 0.5
        score = (kw + 0.5) * live * (r["p"] ** 0.5 + r["c"] ** 0.35)
        flags = []
        if age_h is None:
            flags.append("NEVER_CALLED")
        elif age_h <= 24:
            flags.append("ACTIVE_24H")
        elif age_h > 168:
            flags.append("DORMANT_7D")
        if r["p"] <= 1:
            flags.append("SINGLE_PAYER")
        if not r["sn"]:
            flags.append("UNNAMED_SERVICE")
        if r["cur"]:
            flags.append("CDP_CURATED")
        scored.append((score, r, price, flags))
    scored.sort(key=lambda t: t[0], reverse=True)
    return {
        "need": need,
        "picks": [
            {"resource": r["r"], "service": r["sn"], "calls_30d": r["c"], "payers_30d": r["p"],
             "price": r["amt"], "price_usd": price, "network": r["net"], "curated": r["cur"],
             "risk_flags": flags, "score": round(s, 2)}
            for s, r, price, flags in scored[:limit]
        ],
        "candidates": len(scored),
        "snapshot": "2026-11-24 CDP Bazaar catalog (41,984 listings)",
    }


@app.post("/api/x402/recommend")
async def x402_recommend_post(request: Request):
    """Pick the best x402 vendor for a job  -  ranked by proven demand with risk flags."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    try:
        max_price = float(body.get("max_price") or 0)
    except Exception:
        max_price = 0
    return await _recommend(str(body.get("need") or body.get("q") or ""), str(body.get("net") or ""),
                            int(body.get("limit") or 3), max_price)


@app.get("/api/x402/recommend")
async def x402_recommend_get(need: str = "", net: str = "", max_price: float = 0, limit: int = 3):
    """GET variant of the vendor pick (x402scan-friendly)."""
    return await _recommend(need, net, limit, max_price)

@app.get("/x402/discover")
async def x402_discover_alias(q: str = "", tag: str = "", net: str = "", min_calls: int = 0, limit: int = 10):
    return await x402_discover(q, tag, net, min_calls, limit)


@app.post("/x402/discover")
async def x402_discover_post_alias(request: Request):
    return await x402_discover_post(request)


@app.get("/api/x402/search")
async def x402_search_alias(q: str = "", tag: str = "", net: str = "", min_calls: int = 0, limit: int = 10):
    return await x402_discover(q, tag, net, min_calls, limit)


@app.post("/api/x402/search")
async def x402_search_post_alias(request: Request):
    return await x402_discover_post(request)


@app.get("/x402/search")
async def x402_search_short_alias(q: str = "", tag: str = "", net: str = "", min_calls: int = 0, limit: int = 10):
    return await x402_discover(q, tag, net, min_calls, limit)


@app.post("/x402/search")
async def x402_search_short_post_alias(request: Request):
    return await x402_discover_post(request)


_GAP_CATEGORIES = [
    ("llm-chat", ("chat", "completions", "llm", "openai", "anthropic", "gpt", "claude")),
    ("search", ("search", "exa", "tavily", "query")),
    ("crypto-price", ("price", "ticker", "quote", "market", "exchange", "token", "coin", "dex", "swap")),
    ("chain-rpc", ("rpc", "chain", "block", "wallet", "tx", "transaction", "onchain", "web3")),
    ("image-gen", ("image", "img", "photo", "avatar", "art", "draw", "generate-image")),
    ("video", ("video", "movie", "clip", "render", "stream")),
    ("weather", ("weather", "forecast", "climate")),
    ("scrape-read", ("scrape", "crawl", "fetch", "read", "extract", "parse", "html", "screenshot")),
    ("email", ("email", "mail", "smtp", "inbox")),
    ("social", ("twitter", "x-post", "social", "reddit", "mastodon", "discord")),
    ("dns-domain", ("dns", "domain", "whois")),
    ("qr", ("qr", "qrcode", "barcode")),
    ("translate", ("translate", "lang", "i18n")),
    ("tts-audio", ("tts", "speech", "audio", "voice", "sound", "music")),
    ("location", ("geo", "map", "location", "ip", "country")),
    ("storage", ("upload", "file", "storage", "s3", "bucket", "host")),
    ("verify-kyc", ("verify", "kyc", "captcha", "identity", "honeypot", "goplus")),
    ("analytics", ("analytic", "metrics", "stats", "dashboard", "report", "census")),
]


def _gap_category(row):
    hay = (row["r"] + " " + row["d"] + " " + row["sn"] + " " + " ".join(row["t"])).lower()
    for name, kws in _GAP_CATEGORIES:
        for kw in kws:
            if kw in hay:
                return name
    return "other"


@app.get("/api/x402/top")
async def x402_top(limit: int = 50, net: str = "", min_calls: int = 0):
    """Top-N x402 sellers by real 30d calls, with percentile and market share."""
    net = _clip(net, 60).lower()
    limit = max(1, min(int(limit or 50), 200))
    rows = _discover_rows()
    total_calls = sum(r["c"] for r in rows) or 1
    n_rows = len(rows) or 1
    hits = [r for r in rows
            if (not min_calls or r["c"] >= min_calls) and (not net or net in r["net"].lower())]
    hits.sort(key=lambda r: (r["c"], r["p"]), reverse=True)
    return {
        "snapshot": "2026-11-24 CDP Bazaar catalog (41,984 listings)",
        "total_listings": len(rows),
        "total_calls_30d": total_calls,
        "top": [
            {"rank": i + 1, "resource": r["r"], "service": r["sn"],
             "calls_30d": r["c"], "payers_30d": r["p"], "price": r["amt"],
             "network": r["net"], "curated": r["cur"],
             "market_share_pct": round(100.0 * r["c"] / total_calls, 3),
             "percentile": round(100.0 * (n_rows - i - 1) / n_rows, 2)}
            for i, r in enumerate(hits[:limit])
        ],
    }


@app.get("/api/x402/gaps")
async def x402_gaps():
    """Supply-vs-demand gap analysis across x402 categories."""
    import statistics as _st
    rows = _discover_rows()
    total_calls = sum(r["c"] for r in rows) or 1
    n_rows = len(rows) or 1
    calls_sorted = sorted((r["c"] for r in rows), reverse=True)
    elite_cut = calls_sorted[max(0, int(n_rows * 0.10) - 1)] if rows else 0
    buckets = {}
    for r in rows:
        cat = _gap_category(r)
        b = buckets.setdefault(cat, {"listings": 0, "calls": 0, "payers": 0, "elite": 0, "prices": [], "top": None})
        b["listings"] += 1
        b["calls"] += r["c"]
        b["payers"] += r["p"]
        if r["c"] >= elite_cut and elite_cut:
            b["elite"] += 1
        try:
            b["prices"].append(int(r["amt"]) / 1e6)
        except Exception:
            pass
        if b["top"] is None or r["c"] > b["top"]["c"]:
            b["top"] = r
    cats = []
    for name, b in buckets.items():
        supply = 100.0 * b["listings"] / n_rows
        demand = 100.0 * b["calls"] / total_calls
        t = b["top"]
        cats.append({
            "category": name,
            "listings": b["listings"],
            "supply_share_pct": round(supply, 2),
            "calls_30d": b["calls"],
            "demand_share_pct": round(demand, 2),
            "gap_score": round(demand / supply, 2) if supply else 0.0,
            "elite_rate_pct": round(100.0 * b["elite"] / b["listings"], 2) if b["listings"] else 0.0,
            "median_price_usd": round(_st.median(b["prices"]), 4) if b["prices"] else None,
            "benchmark": {"resource": t["r"], "calls_30d": t["c"]} if t else None,
        })
    cats.sort(key=lambda c: c["gap_score"], reverse=True)
    return {
        "snapshot": "2026-11-24 CDP Bazaar catalog (41,984 listings)",
        "total_listings": len(rows),
        "total_calls_30d": total_calls,
        "elite_cutoff_calls_30d": elite_cut,
        "gap_score_note": "demand_share / supply_share; >1 means buyer calls outpace seller supply",
        "categories": cats,
    }


@app.get("/x402/top")
async def x402_top_alias(limit: int = 50, net: str = "", min_calls: int = 0):
    return await x402_top(limit, net, min_calls)


@app.get("/x402/gaps")
async def x402_gaps_alias():
    return await x402_gaps()


# --- api-xpay-style utility suite (fleet health-check pattern, ~46k calls/mo observed) ---
_UTIL_T0 = time.time()


@app.get("/x402/ping")
async def util_ping():
    return {"status": "ok", "ts": int(time.time())}


@app.get("/x402/time")
async def util_time():
    import datetime as _dt
    now = _dt.datetime.utcnow()
    return {"unix": int(time.time()), "iso": now.strftime("%Y-%m-%dT%H:%M:%SZ")}


@app.get("/x402/uuid")
async def util_uuid():
    import uuid as _u
    return {"uuid": str(_u.uuid4())}


@app.get("/x402/random")
async def util_random(n: int = 1, min: int = Query(0), max: int = Query(1000000), hex: int = Query(0)):
    import secrets as _s
    n = builtins.max(1, builtins.min(int(n or 1), 100))
    out = {"values": [_s.randbelow(builtins.max(1, int(max) - int(min) + 1)) + int(min) for _ in range(n)]}
    if hex:
        out["hex"] = _s.token_hex(builtins.max(1, builtins.min(int(hex), 64)))
    return out


@app.get("/x402/ip")
async def util_ip(request: Request):
    fwd = request.headers.get("x-forwarded-for", "")
    ip = fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "")
    return {"ip": ip}


@app.get("/x402/status")
async def util_status():
    return {
        "status": "operational",
        "version": "0.1.0",
        "uptime_s": int(time.time() - _UTIL_T0),
        "snapshot": "2026-11-24 CDP Bazaar catalog (41,984 listings)",
        "paid_endpoints": len(routes),
    }


@app.get("/x402/network")
async def util_network():
    return {
        "scheme": "exact",
        "networks": [
            {"network": NETWORK, "asset": "USDC", "pay_to": PAY_TO},
            {"network": POLY_NETWORK, "asset": "USDC", "pay_to": PAY_TO},
            {"network": AVAX_NETWORK, "asset": "USDC", "pay_to": PAY_TO},
            {"network": ARB_NETWORK, "asset": "USDC", "pay_to": PAY_TO},
            {"network": SEI_NETWORK, "asset": "USDC", "pay_to": PAY_TO},
            {"network": XLAYER_NETWORK, "asset": "USDC", "pay_to": PAY_TO},
            {"network": SOL_NETWORK, "asset": "USDC", "pay_to": SOL_PAY_TO},
        ],
        "facilitator": "PayAI",
        "settlement": "~2s on-chain",
    }


@app.get("/x402/quote")
async def util_quote(path: str = "", net: str = ""):
    path = _clip(path, 200)
    net = _clip(net, 60).lower()
    nets = [NETWORK, POLY_NETWORK, AVAX_NETWORK, ARB_NETWORK, SEI_NETWORK, XLAYER_NETWORK, SOL_NETWORK]
    if net:
        nets = [n for n in nets if net in n.lower()] or nets
    price = None
    for rname, rc in routes.items():
        p = rname.split(" ", 1)[1]
        if p == path or (path and path in p):
            try:
                price = str(rc.accepts[0].price)
            except Exception:
                pass
            break
    return {"path": path or "/api/x402/discover", "price": price or "see /openapi.json",
            "scheme": "exact", "networks": nets, "pay_to": PAY_TO, "max_timeout_seconds": 3600}


@app.get("/x402/pulse")
async def x402_pulse_alias():
    return await x402_pulse()


@app.get("/x402/latest")
async def x402_latest_alias():
    return await x402_pulse()


@app.get("/x402/ping")
async def x402_ping_alias():
    return await x402_pulse()


@app.get("/x402/recommend")
async def x402_recommend_get_alias(need: str = "", net: str = "", max_price: float = 0, limit: int = 3):
    return await _recommend(need, net, limit, max_price)


@app.post("/x402/recommend")
async def x402_recommend_post_alias(request: Request):
    return await x402_recommend_post(request)


SKILL_MD = """# AtlasData  -  agent skill
> Free discovery tools + pay-per-call data for agents. All paid routes return x402 challenges (USDC on Base eip155:8453 or Solana 5eykt4).

## Bootstrap order
1. GET /.well-known/x402  -  machine-readable endpoint catalog (free)
2. GET /llms.txt  -  human/agent-readable index (free)
3. GET /api/x402/discover?q=<need>  -  find who sells what in the 33k-listing x402 market, ranked by real demand ($0.03)
4. POST /api/x402/recommend {"need":"job"}  -  pick the best x402 vendor for a task, scored by proven demand + risk_flags ($0.02)
5. GET /api/x402/preflight?url=<target>  -  probe any URL's paywall before integrating (free on zrok)
6. Call data endpoints: /api/price/{sym}, /api/chain/*, /api/gas/base, /api/dex/*, /api/wallet/*

## Payment
- Every paid route advertises TWO accepts: Base USDC (payTo 0x299c...) and Solana USDC (payTo DPqdX...)  -  pick either.
- L402 Lightning rail lives on the Cloudflare tunnel front at https://equipped-philadelphia-untitled-graduate.trycloudflare.com (sats instead of USDC).
- No account, no API key: a PAYMENT-SIGNATURE header is a signed payment credential.
"""


@app.get("/.well-known/skill.md", include_in_schema=False)
async def skill_md():
    return PlainTextResponse(SKILL_MD, media_type="text/markdown")


LLMS_TXT = """# AtlasData API
> Pay-per-call data + utility API for agents (x402/L402). Base: https://x402atlas.xyz

## Paid meta endpoints
- GET /api/x402/discover?q=&tag=&net=&min_calls=&limit=  -  ranked x402 catalog search by REAL 30d demand (33k listings, calls+payers)
- POST/GET /api/x402/recommend {"need":"job"}  -  pick the best x402 vendor, ranked by proven demand + risk_flags
- GET /api/x402/pulse  -  daily x402 market pulse (leaderboard + medians)

## Free endpoints (no payment)
- GET /api/text/wordcount?text=...
- GET /api/text/hash?text=...
- GET /api/text/base64?text=...
- GET /api/text/uuid
- GET /api/text/epoch
- GET /api/text/slug?text=...
- GET /api/text/json-validate?text=...
- GET /api/text/reverse?text=...
- GET /api/x402/preflight?url=<target>  -  probe a URL's x402 challenge (version, networks, payTo, safety verdict)

## Paid endpoints (x402 USDC on Base)
- GET /api/price/{symbol}  -  crypto prices
- GET /api/gas/{chain}  -  gas prices
- GET /api/wallet/{address}  -  wallet balance
- GET /api/tx/{chain}/{hash}  -  tx lookup
- GET /api/report/{name}  -  research reports (md)
- GET /api/qr?text=...  -  QR PNG

## Discovery
- /.well-known/x402  -  resource list
- /.well-known/api-catalog  -  linkset catalog
- /openapi.json  -  OpenAPI with x-payment-info
"""
@app.get("/llms.txt", include_in_schema=False)
async def llms_txt():
    return PlainTextResponse(LLMS_TXT)


@app.get("/search")
async def root_search_alias(q: str = "", tag: str = "", net: str = "", min_calls: int = 0, limit: int = 10):
    return await x402_discover(q, tag, net, min_calls, limit)


@app.get("/api/x402/dataset-pro")
async def x402_dataset_pro():
    """Enriched x402 catalog: full 33k-listing dataset plus per-row derived fields
    (elite flag vs top-decile cutoff, risk_flags, price_usd, calls_per_payer)."""
    import datetime as _dt
    rows = _discover_rows()
    cs = sorted(r.get("c", 0) for r in rows)
    p90 = cs[int(len(cs) * 0.9)] if cs else 0
    now = _dt.datetime.now(_dt.timezone.utc)
    out = []
    for r in rows:
        try:
            amt_usd = float(r.get("amt") or 0) / 1e6
        except Exception:
            amt_usd = 0.0
        flags = []
        try:
            lc = _dt.datetime.fromisoformat(str(r.get("lc", "")).replace("Z", "+00:00"))
            age_h = (now - lc).total_seconds() / 3600
            if age_h < 24:
                flags.append("ACTIVE_24H")
            elif age_h > 168:
                flags.append("DORMANT_7D")
        except Exception:
            flags.append("NEVER_CALLED")
        if not r.get("c"):
            flags.append("ZERO_CALLS")
        if r.get("p", 0) <= 1:
            flags.append("SINGLE_PAYER")
        if not r.get("sn"):
            flags.append("UNNAMED")
        if r.get("cur"):
            flags.append("CDP_CURATED")
        out.append({
            **r,
            "elite": r.get("c", 0) >= p90,
            "price_usd": round(amt_usd, 6),
            "calls_per_payer": round(r.get("c", 0) / max(r.get("p", 0), 1), 2),
            "risk_flags": flags,
        })
    body = ("\n".join(json.dumps(x, separators=(",", ":")) for x in out)).encode()
    return Response(
        content=body,
        media_type="application/x-ndjson",
        headers={
            "Content-Disposition": 'attachment; filename="x402-catalog-pro-2026-11-24.jsonl"',
            "X-Snapshot": "2026-11-24",
            "X-Total-Listings": str(len(out)),
        },
    )


@app.get("/api/x402/dataset")
async def x402_dataset():
    """One-shot download of the full x402 catalog dataset (JSONL)."""
    path = os.path.join(os.path.dirname(__file__), "data", "x402_discover.jsonl")
    with open(path, "rb") as f:
        body = f.read()
    return Response(
        content=body,
        media_type="application/x-ndjson",
        headers={
            "Content-Disposition": 'attachment; filename="x402-catalog-2026-11-24.jsonl"',
            "X-Snapshot": "2026-11-24",
            "X-Total-Listings": "41984",
        },
    )


@app.get("/api/x402/census")
async def x402_census():
    """Free teaser: headline census stats from the x402 catalog (full data at /api/x402/dataset)."""
    import json as _json, statistics as _st
    rows = []
    with open(os.path.join(os.path.dirname(__file__), "data", "x402_discover.jsonl")) as _f:
        for _l in _f:
            try:
                rows.append(_json.loads(_l))
            except Exception:
                pass
    cs = [float(r.get("c") or 0) for r in rows]
    ps = [float(r.get("p") or 0) for r in rows]
    called = [(c, float(r.get("amt") or 0)) for r, c in zip(rows, cs) if c > 0]
    gmv = sum(c * a / 1e6 for c, a in called if a <= 1e9)
    top_calls = sum(sorted((c for c, _ in called), reverse=True)[:max(1, int(len(called) * 0.01))])
    total_calls = sum(cs) or 1
    return {
        "snapshot": "2026-11-24",
        "total_listings": len(rows),
        "called_30d": len(called),
        "live_share_pct": round(100 * len(called) / len(rows), 2),
        "median_calls_per_listing": _st.median(cs),
        "median_payers_per_listing": _st.median(ps),
        "est_30d_gmv_usd": round(gmv, 0),
        "top_1pct_call_share_pct": round(100 * top_calls / total_calls, 1),
        "full_dataset": "GET /api/x402/dataset  -  $5 one-time, all 41,984 rows as NDJSON",
        "search": "GET /api/x402/discover?q=  -  $0.03, ranked by proven demand",
    }


@app.get("/api/chain/chain-id")
async def chain_chain_id():
    cid_hex = (await rpc("eth_chainId", [])) or "0x0"
    return {"chain_id_hex": cid_hex, "chain_id": int(cid_hex, 16),
            "name": "Base", "currency": "ETH"}


@app.get("/api/chain/receipt/{tx_hash}")
async def chain_receipt(tx_hash: str):
    if not _hex_hash(tx_hash):
        return JSONResponse({"error": "invalid tx hash"}, status_code=400)
    rc = await rpc("eth_getTransactionReceipt", [tx_hash])
    if not rc:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"hash": tx_hash,
            "status": {"0x1": "success", "0x0": "failed"}.get(rc.get("status"), "pending"),
            "block": int(rc.get("blockNumber") or "0x0", 16) or None,
            "gas_used": int(rc.get("gasUsed") or "0x0", 16) or None,
            "cumulative_gas": int(rc.get("cumulativeGasUsed") or "0x0", 16) or None,
            "contract_address": rc.get("contractAddress"),
            "logs_count": len(rc.get("logs") or []),
            "from": rc.get("from"), "to": rc.get("to"), "chain": "base"}


@app.get("/api/chain/code/{address}")
async def chain_code(address: str):
    if not _hex_addr(address):
        return JSONResponse({"error": "invalid address"}, status_code=400)
    code = (await rpc("eth_getCode", [address, "latest"])) or "0x"
    return {"address": address, "is_contract": code != "0x",
            "code_size": max(0, (len(code) - 2) // 2), "code_prefix": code[:42],
            "chain": "base"}


@app.get("/api/chain/storage/{address}/{slot}")
async def chain_storage(address: str, slot: str):
    if not _hex_addr(address):
        return JSONResponse({"error": "invalid address"}, status_code=400)
    try:
        s = slot if slot.startswith("0x") else hex(int(slot))
    except ValueError:
        return JSONResponse({"error": "invalid slot"}, status_code=400)
    val = await rpc("eth_getStorageAt", [address, s, "latest"])
    return {"address": address, "slot": s, "value": val, "chain": "base"}


@app.get("/api/chain/allowance/{token}/{owner}/{spender}")
async def chain_allowance(token: str, owner: str, spender: str):
    if not (_hex_addr(token) and _hex_addr(owner) and _hex_addr(spender)):
        return JSONResponse({"error": "invalid address"}, status_code=400)
    data = ("0xdd62ed3e" + owner.lower().replace("0x", "").rjust(64, "0")
            + spender.lower().replace("0x", "").rjust(64, "0"))
    raw = int((await _eth_call(token, data)) or "0x0", 16)
    dec_hex = await _eth_call(token, "0x313ce567")
    dec = int(dec_hex, 16) if dec_hex and dec_hex != "0x" else 18
    return {"token": token, "owner": owner, "spender": spender, "raw": str(raw),
            "allowance": raw / (10 ** dec), "chain": "base"}


@app.get("/api/chain/live-balance/{address}")
async def chain_live_balance(address: str):
    if not _hex_addr(address):
        return JSONResponse({"error": "invalid address"}, status_code=400)
    bal = int((await rpc("eth_getBalance", [address, "latest"])) or "0x0", 16)
    nonce = int((await rpc("eth_getTransactionCount", [address, "latest"])) or "0x0", 16)
    blk = int((await rpc("eth_blockNumber", [])) or "0x0", 16)
    code = (await rpc("eth_getCode", [address, "latest"])) or "0x"
    return {"address": address, "wei": str(bal), "eth": bal / 1e18,
            "nonce": nonce, "is_contract": code != "0x", "block": blk,
            "chain": "base", "ts": int(time.time())}


@app.get("/api/chain/estimate-gas")
async def chain_estimate_gas(to: str, data: str = "", value: str = "", request: Request = None):
    if not _hex_addr(to):
        return JSONResponse({"error": "invalid to address"}, status_code=400)
    tx = {"to": to}
    if data:
        tx["data"] = data if data.startswith("0x") else "0x" + data
    from_ = (request.query_params.get("from") or "") if request else ""
    if from_ and _hex_addr(from_):
        tx["from"] = from_
    if value:
        tx["value"] = value if value.startswith("0x") else hex(int(value))
    est = await rpc("eth_estimateGas", [tx])
    if isinstance(est, dict) or est is None:
        return JSONResponse({"error": "estimate failed", "detail": str(est)[:200]},
                            status_code=502)
    gp = int((await rpc("eth_gasPrice", [])) or "0x0", 16)
    gas = int(est, 16)
    return {"to": to, "gas_estimate": gas, "gas_price_wei": gp,
            "gas_price_gwei": gp / 1e9, "cost_eth": gas * gp / 1e18, "chain": "base"}


@app.get("/api/chain/events")
async def chain_events(address: str, topic0: str = "",
                       from_block: int = 0, to_block: int = 0):
    if not _hex_addr(address):
        return JSONResponse({"error": "invalid address"}, status_code=400)
    latest = int((await rpc("eth_blockNumber", [])) or "0x0", 16)
    tb = to_block or latest
    fb = from_block or max(0, tb - 9000)
    if tb - fb > 20000:
        fb = tb - 20000
    flt = {"fromBlock": hex(fb), "toBlock": hex(tb), "address": address}
    if topic0:
        flt["topics"] = [topic0]
    logs = (await rpc("eth_getLogs", [flt])) or []
    return {"address": address, "from_block": fb, "to_block": tb,
            "logs": [{"topics": lg.get("topics"), "data": lg.get("data"),
                      "block": int(lg.get("blockNumber") or "0x0", 16),
                      "tx": lg.get("transactionHash"), "index": lg.get("logIndex")}
                     for lg in logs[:200]],
            "count": len(logs[:200]), "truncated": len(logs) > 200, "chain": "base"}


@app.get("/api/btc/whales")
async def btc_whales(min_btc: float = 5.0, pages: int = 5):
    """Recent large BTC transactions from the latest confirmed block."""
    pages = max(1, min(pages, 8))
    tip = await _client.get("https://mempool.space/api/blocks/tip/hash")
    block_hash = tip.text.strip()
    out = []
    for i in range(pages):
        try:
            r = await _client.get(f"https://mempool.space/api/block/{block_hash}/txs/{i*25}")
            txs = r.json()
        except Exception:
            break
        for tx in txs:
            total = sum(v.get("value", 0) for v in tx.get("vout", []))
            btc = total / 1e8
            if btc >= min_btc:
                out.append({"txid": tx.get("txid"), "value_btc": round(btc, 4),
                            "size_vbytes": tx.get("size"),
                            "fee_sats": tx.get("fee"),
                            "outputs": len(tx.get("vout", []))})
    out.sort(key=lambda x: -x["value_btc"])
    return {"block": block_hash, "min_btc": min_btc, "whales": out[:50],
            "count": len(out), "ts": int(time.time())}


# --- LF-gateway upstream surface -------------------------------------------------
# LightningFaucet's paid gateway must see real data from upstream_url (it rejects
# upstreams that answer 402). Mounted sub-app serves the same /api/* handlers
# WITHOUT the x402 middleware (parent middlewares don't re-gate: scope path under
# /gw never matches the paid route map), protected by a shared-secret header so
# only the gateway (which sends X-Atlas-Gateway-Key) gets free reads.
def _gw_key():
    k = os.environ.get("ATLAS_GW_KEY")
    if k:
        return k
    try:
        for line in open(os.path.expanduser("~/.cdp/env")):
            if line.startswith("ATLAS_GW_KEY="):
                return line.strip().split("=", 1)[1]
    except OSError:
        pass
    return ""


gw_app = FastAPI(title="AtlasData gateway upstream")


@gw_app.middleware("http")
async def _gw_auth(request: Request, call_next):
    k = _gw_key()
    import hmac
    # header first; ?gk= fallback because the Render/zrok proxy chain drops
    # non-essential headers. URL-embedded key only guards free reads.
    ok = (hmac.compare_digest(request.headers.get("x-atlas-gateway-key", ""), k)
          or hmac.compare_digest(request.query_params.get("gk", ""), k)) if k else False
    if not ok:
        return JSONResponse({"error": "not found"}, status_code=404)
    return await call_next(request)


from fastapi.routing import APIRoute
for _r in app.routes:
    if isinstance(_r, APIRoute) and _r.path.startswith("/api/") and "/gw" not in _r.path:
        gw_app.add_api_route(_r.path, _r.endpoint, methods=list(_r.methods))

app.mount("/gw", gw_app)

@app.get("/healthz", include_in_schema=False)
async def healthz():
    return {"ok": True}

@app.get("/blog/{slug}", include_in_schema=False)
async def blog_post(slug: str):
    import os
    from fastapi.responses import FileResponse
    slug = slug.replace("/", "").replace("..", "")
    if not slug.endswith(".html"):
        slug += ".html"
    base = os.path.join(os.path.dirname(__file__), "blog", slug)
    if not os.path.exists(base):
        from fastapi import HTTPException
        raise HTTPException(404)
    return FileResponse(base, media_type="text/html")
