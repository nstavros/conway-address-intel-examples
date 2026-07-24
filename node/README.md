# Node example

```bash
npm install
# Free, no wallet:
node overview.js 0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913
# Paid ($0.02 USDC on Base), needs a funded wallet:
PRIVATE_KEY=0xYOURKEY node report.js 0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913
```
The buyer needs **no ETH for gas** — the x402 facilitator submits the transfer. Failed reports are never charged.
