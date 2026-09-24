/**
 * Emit dist/artifact.html — the same bundle, wrapped for a host that supplies
 * its own <!doctype>/<html>/<head>/<body>. Generated from dist/index.html so the
 * fingerprinted asset names can never drift out of sync.
 */
import { readFileSync, writeFileSync } from "fs";

const html = readFileSync("dist/index.html", "utf8");
const css = html.match(/href="\.\/(assets\/[^"]+\.css)"/)?.[1];
const js = html.match(/src="\.\/(assets\/[^"]+\.js)"/)?.[1];
const title = html.match(/<title>([^<]*)<\/title>/)?.[1] ?? "Sentinel-FX";
if (!css || !js) throw new Error("could not locate the built assets in dist/index.html");

writeFileSync("dist/artifact.html", `<title>${title}</title>
<meta name="color-scheme" content="light dark" />
<link rel="stylesheet" href="${css}" />
<style>
  /* First frame: a resting skeleton, so a thumbnail or a slow connection never
     shows an empty page. React replaces it on mount. */
  #root:empty::after {
    content: "Sentinel-FX";
    display: flex; align-items: center; justify-content: center;
    min-height: 60vh; font: 600 18px/1.4 system-ui, sans-serif;
    letter-spacing: -.02em; color: #737373;
  }
</style>
<div id="root"></div>
<script type="module" crossorigin src="${js}"></script>
`);
console.log(`artifact.html -> ${css} + ${js}`);
