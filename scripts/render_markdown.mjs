#!/usr/bin/env node

/**
 * Render a Markdown file to paginated PNG images using marknative.
 *
 * Usage:
 *   node scripts/render_markdown.mjs <input.md> [output_prefix]
 *
 * Outputs: <output_prefix>-01.png, <output_prefix>-02.png, ...
 * Prints each output file path on its own line.
 */

import { readFileSync, writeFileSync, existsSync, unlinkSync } from "node:fs";
import { resolve, dirname, join, basename, extname } from "node:path";
import { renderMarkdown } from "marknative";

const [inputPath, outputPrefix] = process.argv.slice(2);

if (!inputPath) {
  console.error("Usage: node scripts/render_markdown.mjs <input.md> [output_prefix]");
  process.exit(1);
}

const md = readFileSync(resolve(inputPath), "utf-8");
const prefix =
  outputPrefix ||
  join(dirname(resolve(inputPath)), basename(inputPath, extname(inputPath)));

// Clean up old files from previous runs
for (let i = 1; i <= 50; i++) {
  const oldFile = `${prefix}-${String(i).padStart(2, "0")}.png`;
  if (existsSync(oldFile)) unlinkSync(oldFile);
}
if (existsSync(`${prefix}.png`)) unlinkSync(`${prefix}.png`);

const pages = await renderMarkdown(md, {
  format: "png",
  singlePage: false,
  scale: 2,
  theme: "default",
});

if (!pages.length) {
  console.error("marknative returned no output");
  process.exit(1);
}

for (const [i, page] of pages.entries()) {
  const suffix = String(i + 1).padStart(2, "0");
  const out = `${prefix}-${suffix}.png`;
  writeFileSync(resolve(out), page.data);
  console.log(resolve(out));
}
