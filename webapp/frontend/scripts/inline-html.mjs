import { readFile, readdir, writeFile } from 'node:fs/promises';
import { gzipSync } from 'node:zlib';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const frontendDir = path.dirname(fileURLToPath(import.meta.url));
const distDir = path.resolve(frontendDir, '../../dist');
const indexPath = path.join(distDir, 'index.html');
let html = await readFile(indexPath, 'utf8');

const scriptTag = [...html.matchAll(/<script\b[^>]*\bsrc="([^"]+\.js)"[^>]*><\/script>/gi)]
  .find((match) => match[1].startsWith('/dashboard/assets/'));
const styleTag = html.match(/<link\b[^>]*\brel="stylesheet"[^>]*\bhref="([^"]+\.css)"[^>]*>/i);
if (!scriptTag || !styleTag) throw new Error('Expected one built JavaScript bundle and one stylesheet in Vite output.');

const jsName = path.basename(scriptTag[1]);
const cssName = path.basename(styleTag[1]);
const jsPath = path.join(distDir, 'assets', jsName);
const cssPath = path.join(distDir, 'assets', cssName);
let js = await readFile(jsPath, 'utf8');
const css = await readFile(cssPath, 'utf8');

const assetFiles = await readdir(path.join(distDir, 'assets'));
const previewName = assetFiles.find((name) => /^alpine-preview-.*\.(png|jpe?g|webp)$/i.test(name));
if (!previewName) throw new Error('Frame Studio preview artwork was not found in Vite assets.');
const previewBytes = await readFile(path.join(distDir, 'assets', previewName));
const mime = previewName.toLowerCase().endsWith('.png') ? 'image/png'
  : previewName.toLowerCase().endsWith('.webp') ? 'image/webp' : 'image/jpeg';
const previewDataUrl = `data:${mime};base64,${previewBytes.toString('base64')}`;
const assetUrl = `/dashboard/assets/${previewName}`;
if (!js.includes(assetUrl)) throw new Error(`Built application does not reference expected artwork URL ${assetUrl}.`);

html = html.replace(scriptTag[0], () => `<script type="module">\n${js.replace(/<\/script/gi, '<\\/script')}\n</script>`);
html = html.replace(styleTag[0], () => `<style>\n${css}\n</style>`);
html = html.replace('</head>', '  <!-- Frame Studio JS/CSS are embedded; preview artwork remains a separate lazy asset. -->\n  </head>');
await writeFile(indexPath, html, 'utf8');
const gzipPath = path.join(distDir, 'index.html.gz');
const gzipBytes = gzipSync(Buffer.from(html, 'utf8'), { level: 9 });
await writeFile(gzipPath, gzipBytes);
const standalonePath = path.resolve(frontendDir, '../../../FrameStudioMiniApp.html');
const standaloneHtml = html.replaceAll(assetUrl, previewDataUrl);
await writeFile(standalonePath, standaloneHtml, 'utf8');
console.log(`Inlined ${jsName} and ${cssName} into ${indexPath}; artwork is served lazily from ${previewName}. Standalone copy: ${standalonePath} (${Buffer.byteLength(standaloneHtml)} bytes; server HTML gzip ${gzipBytes.length} bytes).`);
