#!/usr/bin/env node

/**
 * Render a Markdown file to a single long-page PNG image using marknative.
 *
 * Usage:
 *   node scripts/render_markdown.mjs <input.md> [output.png]
 *
 * If output path is omitted, writes <stem>-preview.png next to the source.
 * Prints the output file path to stdout on success.
 */

import { readFileSync, writeFileSync, existsSync, unlinkSync } from "node:fs";
import { resolve, dirname, join, basename, extname } from "node:path";
import { renderMarkdown } from "marknative";

const [inputPath, outputPath] = process.argv.slice(2);

if (!inputPath) {
  console.error("Usage: node scripts/render_markdown.mjs <input.md> [output.png]");
  process.exit(1);
}

const md = readFileSync(resolve(inputPath), "utf-8");
const out =
  outputPath ||
  join(dirname(resolve(inputPath)), basename(inputPath, extname(inputPath)) + "-preview.png");

// Clean up old paginated files from previous runs
for (let i = 1; i <= 50; i++) {
  const oldFile = join(dirname(resolve(out)), `${basename(resolve(out), extname(resolve(out)))}-${String(i).padStart(2, "0")}.png`);
  if (existsSync(oldFile)) unlinkSync(oldFile);
}
if (existsSync(resolve(out))) unlinkSync(resolve(out));

const pages = await renderMarkdown(md, {
  format: "png",
  singlePage: true,
  scale: 1,
  theme: { page: { width: 1600 } },
});

const first = pages[0];
if (!first || first.format !== "png") {
  console.error("marknative returned no output");
  process.exit(1);
}

writeFileSync(resolve(out), first.data);
console.log(resolve(out));
