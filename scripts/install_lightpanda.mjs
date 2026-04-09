import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(__dirname, "..");
const installDir = path.resolve(repoRoot, ".lightpanda");
const binaryPath = process.env.LIGHTPANDA_BINARY || path.join(installDir, "lightpanda");

function resolveAssetName() {
  if (process.platform !== "linux") {
    throw new Error(`当前安装脚本仅支持 linux，实际平台: ${process.platform}`);
  }

  if (process.arch === "x64") {
    return "lightpanda-x86_64-linux";
  }

  if (process.arch === "arm64") {
    return "lightpanda-aarch64-linux";
  }

  throw new Error(`当前安装脚本不支持架构: ${process.arch}`);
}

async function download(url, destination) {
  const response = await fetch(url);
  if (!response.ok) {
    throw new Error(`下载失败: ${response.status} ${response.statusText} ${url}`);
  }

  await fs.promises.mkdir(path.dirname(destination), { recursive: true });
  const buffer = Buffer.from(await response.arrayBuffer());
  await fs.promises.writeFile(destination, buffer, { mode: 0o755 });
  await fs.promises.chmod(destination, 0o755);
}

async function main() {
  if (fs.existsSync(binaryPath)) {
    console.log(`Lightpanda 已存在: ${binaryPath}`);
    return;
  }

  const assetName = resolveAssetName();
  const version = process.env.LIGHTPANDA_VERSION || "latest";
  const baseUrl =
    version === "latest"
      ? `https://github.com/lightpanda-io/browser/releases/latest/download/${assetName}`
      : `https://github.com/lightpanda-io/browser/releases/download/${version}/${assetName}`;

  console.log(`下载 Lightpanda: ${baseUrl}`);
  await download(baseUrl, binaryPath);
  console.log(`Lightpanda 已安装到: ${binaryPath}`);
}

main().catch((error) => {
  console.error(error instanceof Error ? error.message : String(error));
  process.exit(1);
});
