// Full paid /report flow: 402 -> sign USDC authorization -> retry -> report.
// Needs a wallet holding USDC on Base. Zero real money? Point CONWAY_URL at the
// sandbox and fund the wallet from faucet.circle.com (Base Sepolia).
//   npm i x402-fetch viem
//   PRIVATE_KEY=0x... node report.js 0xADDRESS
import { wrapFetchWithPayment } from "x402-fetch";
import { privateKeyToAccount } from "viem/accounts";

const BASE = process.env.CONWAY_URL ?? "https://conway-address-intel-production.up.railway.app";
const address = process.argv[2] ?? "0x4200000000000000000000000000000000000006";
const account = privateKeyToAccount(process.env.PRIVATE_KEY);

const payFetch = wrapFetchWithPayment(fetch, account, 100000n); // hard cap 0.10 USDC
const res = await payFetch(`${BASE}/report/${address}`);
console.log(await res.text());
const settle = res.headers.get("x-payment-response");
if (settle) console.log("\nsettlement:", Buffer.from(settle, "base64").toString());
