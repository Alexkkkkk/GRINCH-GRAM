---
name: TonConnect manifest must be HTTPS on Replit
description: The /tonconnect-manifest.json route must force https for non-local hosts, because the Replit proxy omits X-Forwarded-Proto and TonKeeper refuses an http manifest.
---

# TonConnect / TonKeeper manifest must be served over HTTPS

**Rule:** build the `tonconnect-manifest.json` `url` from `request.host` and force the `https` scheme for any
non-local host; keep `http` only for `127.0.0.1` / `localhost` dev.

**Why:** TonKeeper (and other TonConnect wallets) fetch the manifest on the user's phone and reject it unless it is
HTTPS with a `url` matching the dapp origin. On Replit the proxy terminates TLS but does **not** reliably set
`X-Forwarded-Proto`, so `ProxyFix(x_proto=1)` is ineffective and `request.host_url` returns `http://…` even on the
public `.replit.dev`/`.replit.app` domain — which silently breaks the wallet connect handshake.

**How to apply:** any route that emits an absolute URL a wallet/3rd-party will validate (manifests, OAuth redirect
URIs, webhooks) must not trust `request.scheme`/`host_url` on Replit — derive https explicitly for non-local hosts.
