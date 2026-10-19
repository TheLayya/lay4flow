// Load the same local script assets as the page, in document order.
const fs = require('node:fs');
const path = require('node:path');

function sources(html) {
  const root = path.resolve(__dirname, '../../static/js');
  return [...html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/gi)].map(([, attrs, inline]) => {
    const src = attrs.match(/\bsrc=["']([^"']+)["']/i)?.[1];
    if (!src) return inline;
    // The test harness supplies the existing Tailwind configuration object.
    if (src === 'https://cdn.tailwindcss.com') return '';
    if (!src.startsWith('/static/js/')) throw new Error(`Unexpected script asset: ${src}`);
    const file = path.resolve(root, src.split('?')[0].slice('/static/js/'.length));
    if (!file.startsWith(root + path.sep)) throw new Error(`Script escapes static/js: ${src}`);
    return fs.readFileSync(file, 'utf8');
  }).filter(code => code.trim());
}

module.exports = html => sources(html).join('\n');
module.exports.sources = sources;
