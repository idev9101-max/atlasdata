# AtlasData mirror for Hugging Face Spaces — emits its own 402 challenge,
# proxies signed requests to the zrok backend for settlement + content.
import json, re, base64, os, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qsl

UPSTREAM = "https://atlasdata.shares.zrok.io"
PAY_TO = "0x299cb831e8580f559e58064d4F145389dB418eCE"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

ROUTES = [
    (r"^/api/price/([A-Za-z0-9-]+)$", ["symbol"], "5000", "Real-time USD spot price for any crypto asset (Coinbase data)."),
    (r"^/api/ticker/([A-Za-z0-9-]+)$", ["symbol"], "10000", "24h ticker: price, volume, change for any crypto asset."),
    (r"^/api/gas/base$", [], "5000", "Current Base mainnet gas price and latest block."),
    (r"^/api/exchange/([A-Za-z0-9-]+)$", ["pair"], "10000", "Exchange rates and currency conversion for a pair like BTC-ETH."),
    (r"^/api/token/(0x[0-9a-fA-F]{40})$", ["address"], "10000", "ERC-20 token metadata on Base for a contract address."),
    (r"^/api/wallet/(0x[0-9a-fA-F]{40})$", ["address"], "10000", "ETH balance and tx count for any address on Base mainnet."),
    (r"^/api/tx/base/(0x[0-9a-fA-F]{64})$", ["txhash"], "15000", "Decoded transaction details for a Base mainnet transaction."),
    (r"^/api/block/base/latest$", [], "10000", "Latest Base block with timestamp and gas metrics."),
    (r"^/api/btc/fees$", [], "10000", "Live Bitcoin mempool fee estimates (fast/half-hour/hour) plus mempool stats."),
    (r"^/api/btc/mempool$", [], "15000", "Current Bitcoin mempool size, fee histogram and recent block stats."),
    (r"^/api/btc/tip$", [], "10000", "Bitcoin tip height, hash, median fee and unconfirmed tx count."),
    (r"^/api/sol/slot$", [], "10000", "Current Solana slot, block height and recent performance samples."),
    (r"^/api/feargreed$", [], "10000", "Crypto Fear & Greed index: value, classification, timestamp, history."),
    (r"^/api/market/snapshot$", [], "25000", "Multi-source crypto market snapshot: BTC+ETH price, gas, mempool, fear/greed."),
    (r"^/api/dex/token/(0x[0-9a-fA-F]{40})$", ["address"], "15000", "DEX pairs, liquidity and price for a token address (DexScreener)."),
    (r"^/api/x402/discover$", [], "30000", "Search 33k+ x402 services: keyword, network, curated filters, risk flags."),
    (r"^/api/x402/pulse$", [], "20000", "Daily x402 market leaderboard + price/call medians from 33k listings."),
    (r"^/api/x402/recommend$", [], "20000", "Vendor pick for a need: scored on proven demand with risk_flags."),
    (r"^/api/chain/wallet-report/(0x[0-9a-fA-F]{40}|[1-9A-HJ-NP-Za-km-z]{32,44})$", ["address"], "150000", "Multi-chain wallet risk report: balances, tx counts, holdings, scoring."),
]
ROUTES = [(re.compile(p), names, atomic, desc) for p, names, atomic, desc in ROUTES]


def b64(o):
    return base64.b64encode(json.dumps(o, separators=(",", ":")).encode()).decode()


def challenge(url, params, atomic, desc):
    path_props = {p: {"type": "string"} for p in params}
    input_schema = {
        "type": "object",
        "properties": {
            "type": {"type": "string", "const": "http"},
            "method": {"type": "string", "enum": ["GET"]},
            **({"pathParams": {"type": "object", "properties": path_props, "required": params}} if params else {}),
        },
        "required": ["type", "method"],
        "additionalProperties": False,
    }
    return {
        "x402Version": 2,
        "error": "Payment required",
        "resource": {"url": url, "description": desc, "mimeType": "application/json"},
        "accepts": [{
            "scheme": "exact", "network": "eip155:8453", "amount": atomic, "asset": USDC,
            "payTo": PAY_TO, "maxTimeoutSeconds": 300,
            "extra": {"name": "USD Coin", "version": "2"},
        }],
        "extensions": {
            "bazaar": {
                "info": {
                    "input": {"type": "http", "method": "GET", **({"pathParams": path_props} if params else {})},
                    "output": {"type": "json", "example": {"status": "ok"}},
                },
                "schema": {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "type": "object",
                    "properties": {
                        "input": input_schema,
                        "output": {"type": "object", "properties": {"type": {"type": "string"}, "example": {"type": "object"}}, "required": ["type"]},
                    },
                },
            }
        },
    }


class H(BaseHTTPRequestHandler):
    def _send(self, code, body=b"", ctype="application/json", extra=None):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        path = u.path
        for rx, params, atomic, desc in ROUTES:
            m = rx.match(path)
            if not m:
                continue
            sig = self.headers.get("payment-signature")
            origin = "https://" + (self.headers.get("host") or "atlasdatadev-atlas.hf.space")
            if not sig:
                ch = challenge(origin + self.path, params, atomic, desc)
                return self._send(402, json.dumps({"error": "payment required"}), extra={"payment-required": b64(ch)})
            try:
                req = urllib.request.Request(UPSTREAM + path + ("?" + u.query if u.query else ""),
                                             headers={"payment-signature": sig})
                r = urllib.request.urlopen(req, timeout=30)
                body = r.read()
                extra = {}
                pr = r.headers.get("payment-response")
                if pr:
                    extra["payment-response"] = pr
                return self._send(r.status, body, r.headers.get("content-type") or "application/json", extra)
            except urllib.error.HTTPError as e:
                body = e.read()
                extra = {}
                pr = e.headers.get("payment-response") if e.headers else None
                if pr:
                    extra["payment-response"] = pr
                return self._send(e.code, body, "application/json", extra)
            except Exception:
                return self._send(502, json.dumps({"error": "upstream unreachable"}))
        if path in ("/", "/api"):
            idx = {"service": "AtlasData", "upstream": UPSTREAM,
                   "endpoints": [{"pattern": rx.pattern, "price": a, "desc": d} for rx, _, a, d in ROUTES]}
            return self._send(200, json.dumps(idx))
        if path == "/healthz":
            return self._send(200, "ok", "text/plain")
        return self._send(404, json.dumps({"error": "not found", "endpoints": len(ROUTES)}))

    def do_POST(self):
        return self.do_GET()

    def log_message(self, *a):
        pass


PORT = int(os.environ.get("PORT", "7860"))
ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
