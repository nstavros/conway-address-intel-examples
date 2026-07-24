// Free /overview — no wallet, no key. Good for CI, previews, badges.
//   node overview.js 0xADDRESS
const BASE = process.env.CONWAY_URL ?? "https://conway-address-intel-production.up.railway.app";
const address = process.argv[2] ?? "0x4200000000000000000000000000000000000006";
const res = await fetch(`${BASE}/overview/${address}`);
console.log(JSON.stringify(await res.json(), null, 2));
