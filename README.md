# Conway Address Intel — examples

Copy-paste integrations for [Conway Address Intel](https://conway-address-intel-production.up.railway.app) — a pay-per-call address intelligence service for **Base**.

- **Free** `GET /overview/{address}` — contract vs wallet, holds-funds, activity bucket. No wallet, no key.
- **Paid** `GET /report/{address}` — $0.02 USDC via [x402](https://x402.org): exact balances, tx count, **first-seen date**, ERC-20 probe, observations.

**Honest scope:** live public onchain data only. Not a security audit, not scam detection, not financial advice.

| Endpoint | URL |
|---|---|
| Production (Base mainnet) | `https://conway-address-intel-production.up.railway.app` |
| Sandbox (Base Sepolia, free faucet USDC) | `https://conway-sandbox-production.up.railway.app` |
| Machine manifest | `/.well-known/x402` |

## Examples
- **[`badge/`](badge/)** — drop-in address-safety badge. 2 lines of HTML, free, no wallet. The fastest way to add "is this a fresh/contract/empty address?" to any UI.
- **[`node/`](node/)** — full paid `/report` flow in Node with `x402-fetch` (~10 lines).
- **[`dashboard/`](dashboard/)** — enrich a table of addresses: free badge on render, paid detail on click.

Try it free right now (no wallet):
```bash
curl https://conway-address-intel-production.up.railway.app/overview/0x4200000000000000000000000000000000000006
```
