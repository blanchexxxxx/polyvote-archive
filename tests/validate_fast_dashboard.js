const fs = require("fs");

const html = fs.readFileSync("fast_dashboard.html", "utf8");
const script = html.match(/<script>([\s\S]*?)<\/script>/);
if (!script) throw new Error("inline script missing");
new Function(script[1]);

const ids = [...html.matchAll(/id="([^"]+)"/g)].map((match) => match[1]);
const duplicates = ids.filter((id, index) => ids.indexOf(id) !== index);
if (duplicates.length) throw new Error(`duplicate ids: ${duplicates.join(", ")}`);

const offlineCss = html.match(/\.offline\{([^}]*)\}/);
if (!offlineCss) throw new Error("stale-data alert styles missing");
if (/inset\s*:\s*0/.test(offlineCss[1])) {
  throw new Error("stale-data alert must not cover the viewport");
}
if (!/pointer-events\s*:\s*none/.test(offlineCss[1])) {
  throw new Error("stale-data alert must not block dashboard interaction");
}
if (!html.includes('id="alertTitle"') || !html.includes('id="alertText"')) {
  throw new Error("freshness alert title/text targets missing");
}
if (!script[1].includes("b.online===false")) {
  throw new Error("engine stale alert must use backend per-bot freshness");
}
if (!script[1].includes("s.generated_at")) {
  throw new Error("dashboard delay alert must use aggregate generation time");
}
if (!script[1].includes("DASHBOARD DELAY") || !script[1].includes("ENGINE STALE")) {
  throw new Error("engine and dashboard freshness alerts must be distinct");
}
if (script[1].includes("age>3.5")) {
  throw new Error("receipt-time age must not masquerade as engine staleness");
}

console.log("frontend syntax and ids OK");
