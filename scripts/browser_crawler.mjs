import fs from "node:fs";
import path from "node:path";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import puppeteer from "puppeteer-core";
import { chromium } from "playwright-core";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(__dirname, "..");
const defaultBinary = process.env.LIGHTPANDA_BINARY || path.resolve(repoRoot, ".lightpanda", "lightpanda");

function parseArgs(argv) {
  const args = {
    config: process.env.DIGEST_BROWSER_TARGETS_FILE || path.resolve(repoRoot, "config", "browser_targets.json"),
    output: "",
    host: process.env.LIGHTPANDA_HOST || "127.0.0.1",
    port: Number.parseInt(process.env.LIGHTPANDA_PORT || "9222", 10),
    binary: defaultBinary,
  };

  for (let index = 0; index < argv.length; index += 1) {
    const token = argv[index];
    const next = argv[index + 1];
    if (token === "--config" && next) {
      args.config = path.resolve(next);
      index += 1;
    } else if (token === "--output" && next) {
      args.output = path.resolve(next);
      index += 1;
    } else if (token === "--host" && next) {
      args.host = next;
      index += 1;
    } else if (token === "--port" && next) {
      args.port = Number.parseInt(next, 10);
      index += 1;
    } else if (token === "--binary" && next) {
      args.binary = path.resolve(next);
      index += 1;
    }
  }

  return args;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function toAbsoluteUrl(url, baseUrl) {
  if (!url) {
    return "";
  }

  try {
    return new URL(url, baseUrl).toString();
  } catch {
    return url;
  }
}

function normalizeItems(items, target) {
  if (!Array.isArray(items)) {
    return [];
  }

  return items
    .map((item) => {
      const url = toAbsoluteUrl(item.url || "", target.url);
      const title = String(item.title || "").trim();
      if (!url || !title) {
        return null;
      }

      const metadata = typeof item.metadata === "object" && item.metadata ? item.metadata : {};
      return {
        source: item.source || target.name,
        title,
        url,
        published_at: String(item.published_at || ""),
        raw_summary: String(item.raw_summary || item.summary || ""),
        category_hint: String(item.category_hint || target.category_hint || ""),
        signals: Array.isArray(item.signals)
          ? item.signals.map((entry) => String(entry).trim()).filter(Boolean)
          : [],
        metadata,
      };
    })
    .filter(Boolean)
    .slice(0, target.limit || 20);
}

function compileFunction(script, fallbackName) {
  if (!script || !String(script).trim()) {
    throw new Error(`${fallbackName} 不能为空`);
  }
  return new Function(`return (${script});`)();
}

async function executeSteps(page, target) {
  const steps = Array.isArray(target.steps) && target.steps.length > 0 ? target.steps : [
    {
      action: "goto",
      url: target.url,
      waitUntil: target.waitUntil || "networkidle0",
    },
  ];

  for (const step of steps) {
    const action = step.action || "goto";
    if (action === "goto") {
      await page.goto(step.url || target.url, {
        waitUntil: step.waitUntil || target.waitUntil || "networkidle0",
        timeout: step.timeoutMs || target.timeoutMs || 45_000,
      });
      continue;
    }

    if (action === "waitForSelector") {
      await page.waitForSelector(step.selector, {
        timeout: step.timeoutMs || target.timeoutMs || 45_000,
      });
      continue;
    }

    if (action === "click") {
      const times = step.times || 1;
      for (let index = 0; index < times; index += 1) {
        await page.click(step.selector, {
          delay: step.delayMs || 50,
        });
        if (step.afterWaitMs) {
          await sleep(step.afterWaitMs);
        }
      }
      continue;
    }

    if (action === "type") {
      await page.type(step.selector, step.text || "", {
        delay: step.delayMs || 30,
      });
      continue;
    }

    if (action === "waitForTimeout") {
      await sleep(step.ms || 1_000);
      continue;
    }

    if (action === "evaluate") {
      const fn = compileFunction(step.script, `${target.name} evaluate step`);
      await page.evaluate(fn, step.args || {});
      continue;
    }

    if (action === "scrollToBottom") {
      const times = step.times || 3;
      for (let index = 0; index < times; index += 1) {
        await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
        await sleep(step.afterWaitMs || 1_000);
      }
      continue;
    }

    throw new Error(`不支持的 step.action: ${action}`);
  }
}

function pickField(root, field) {
  if (!field) {
    return "";
  }

  if (typeof field === "string") {
    const node = root.querySelector(field);
    return node ? node.textContent.trim() : "";
  }

  const selector = field.selector || null;
  const attr = field.attr || "textContent";
  const node = selector ? root.querySelector(selector) : root;
  if (!node) {
    return "";
  }

  if (attr === "textContent") {
    return (node.textContent || "").trim();
  }

  return (node.getAttribute(attr) || "").trim();
}

async function extractItems(page, target) {
  if (!target.extract) {
    throw new Error(`${target.name} 缺少 extract 配置`);
  }

  if (target.extract.mode === "script") {
    const fn = compileFunction(target.extract.script, `${target.name} extract.script`);
    const result = await page.evaluate(fn, target.extract.args || {});
    return normalizeItems(result, target);
  }

  if (target.extract.mode === "selector") {
    const itemSelector = target.extract.itemSelector;
    const fields = target.extract.fields || {};
    const result = await page.$$eval(
      itemSelector,
      (nodes, fieldConfig) =>
        nodes.map((root) => {
          const pick = (field) => {
            if (!field) {
              return "";
            }

            if (typeof field === "string") {
              const node = root.querySelector(field);
              return node ? (node.textContent || "").trim() : "";
            }

            const selector = field.selector || null;
            const attr = field.attr || "textContent";
            const node = selector ? root.querySelector(selector) : root;
            if (!node) {
              return "";
            }
            if (attr === "textContent") {
              return (node.textContent || "").trim();
            }
            return (node.getAttribute(attr) || "").trim();
          };

          return {
            title: pick(fieldConfig.title),
            url: pick(fieldConfig.url),
            raw_summary: pick(fieldConfig.raw_summary),
            published_at: pick(fieldConfig.published_at),
          };
        }),
      fields,
    );
    return normalizeItems(result, target);
  }

  throw new Error(`${target.name} extract.mode 仅支持 script 或 selector`);
}

async function withPuppeteer(wsEndpoint, target) {
  const browser = await puppeteer.connect({
    browserWSEndpoint: wsEndpoint,
    defaultViewport: target.viewport || { width: 1440, height: 1024 },
  });
  try {
    const page = await browser.newPage();
    if (target.userAgent) {
      await page.setUserAgent(target.userAgent);
    }
    if (target.headers) {
      await page.setExtraHTTPHeaders(target.headers);
    }
    await executeSteps(page, target);
    return await extractItems(page, target);
  } finally {
    await browser.disconnect();
  }
}

async function withPlaywright(wsEndpoint, target) {
  const browser = await chromium.connectOverCDP(wsEndpoint);
  let context = browser.contexts()[0];
  if (!context) {
    context = await browser.newContext({
      viewport: target.viewport || { width: 1440, height: 1024 },
      userAgent: target.userAgent || undefined,
      extraHTTPHeaders: target.headers || undefined,
    });
  }

  const page = await context.newPage();
  try {
    await executeSteps(page, target);
    return await extractItems(page, target);
  } finally {
    await browser.close();
  }
}

async function waitForBrowser(target) {
  const wsEndpoint = `ws://${target.host}:${target.port}`;
  const deadline = Date.now() + 30_000;
  let lastError = null;

  while (Date.now() < deadline) {
    try {
      const browser = await puppeteer.connect({ browserWSEndpoint: wsEndpoint });
      await browser.disconnect();
      return wsEndpoint;
    } catch (error) {
      lastError = error;
      await sleep(500);
    }
  }

  throw lastError || new Error("等待 Lightpanda 启动超时");
}

function startLightpanda({ binary, host, port }) {
  if (!fs.existsSync(binary)) {
    throw new Error(`找不到 Lightpanda 二进制文件: ${binary}`);
  }

  const child = spawn(binary, ["serve", "--host", host, "--port", String(port)], {
    stdio: ["ignore", "pipe", "pipe"],
  });
  child.stdout.on("data", (chunk) => process.stderr.write(`[lightpanda] ${chunk}`));
  child.stderr.on("data", (chunk) => process.stderr.write(`[lightpanda] ${chunk}`));
  return child;
}

async function crawlTarget(wsEndpoint, target) {
  if ((target.engine || "puppeteer") === "playwright") {
    return withPlaywright(wsEndpoint, target);
  }
  return withPuppeteer(wsEndpoint, target);
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (!fs.existsSync(args.config)) {
    throw new Error(`找不到浏览器爬虫配置: ${args.config}`);
  }

  const config = JSON.parse(fs.readFileSync(args.config, "utf8"));
  const targets = Array.isArray(config.targets) ? config.targets.filter((item) => item.enabled !== false) : [];
  if (targets.length === 0) {
    if (args.output) {
      fs.writeFileSync(args.output, "[]\n");
    }
    return;
  }

  const child = startLightpanda(args);
  let wsEndpoint = "";
  try {
    wsEndpoint = process.env.LIGHTPANDA_WS_ENDPOINT || (await waitForBrowser(args));
    const allItems = [];
    for (const target of targets) {
      const items = await crawlTarget(wsEndpoint, target);
      allItems.push(...items);
    }

    const output = JSON.stringify(allItems, null, 2);
    if (args.output) {
      fs.writeFileSync(args.output, `${output}\n`);
    } else {
      process.stdout.write(`${output}\n`);
    }
  } finally {
    if (child && !child.killed) {
      child.kill("SIGTERM");
    }
  }
}

main().catch((error) => {
  console.error(error instanceof Error ? error.stack || error.message : String(error));
  process.exit(1);
});
