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

console.log("frontend syntax and ids OK");
