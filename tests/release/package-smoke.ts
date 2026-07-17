import { existsSync } from "node:fs";
import { mkdir, readdir, writeFile } from "node:fs/promises";
import { join, resolve } from "node:path";
import { SidecarClient } from "../../src/mcp-client.js";

const [rawPackageRoot, rawAgentDir] = process.argv.slice(2);
if (!rawPackageRoot || !rawAgentDir) {
  throw new Error("usage: package-smoke.ts PACKAGE_ROOT AGENT_DIR");
}

const packageRoot = resolve(rawPackageRoot);
const agentDir = resolve(rawAgentDir);
await mkdir(agentDir, { recursive: true });
await writeFile(
  join(agentDir, "mcp.json"),
  JSON.stringify({
    mcpServers: {
      placeholder: {
        command: "this-command-must-not-start-during-status",
      },
    },
  }),
  "utf8",
);

const client = new SidecarClient({ packageRoot, agentDir });
try {
  const status = await client.call("status", {});
  if (status.connected !== true || status.tool_count !== 0) {
    throw new Error(`unexpected sidecar status: ${JSON.stringify(status)}`);
  }
  if (status.config_path !== join(agentDir, "mcp.json")) {
    throw new Error(`sidecar used the wrong config path: ${String(status.config_path)}`);
  }
  const upstreams = status.upstreams;
  if (!Array.isArray(upstreams) || upstreams.length !== 1 || upstreams[0]?.name !== "placeholder") {
    throw new Error(`unexpected upstream status: ${JSON.stringify(upstreams)}`);
  }
} finally {
  await client.close();
}

const runtimeRoot = join(agentDir, "pi-codemcp", "runtime");
const venvRoot = join(runtimeRoot, "venv");
const uvVersions = await readdir(join(runtimeRoot, "uv"));
if (uvVersions.length !== 1) {
  throw new Error(`expected one managed uv version, found: ${uvVersions.join(", ")}`);
}
const bundledUv = join(
  runtimeRoot,
  "uv",
  uvVersions[0] ?? "missing",
  process.platform === "win32" ? "uv.exe" : "uv",
);
const pytest = join(venvRoot, process.platform === "win32" ? "Scripts" : "bin", "pytest");

if (!existsSync(join(venvRoot, "pyvenv.cfg"))) {
  throw new Error("bundled uv did not create the isolated runtime environment");
}
if (!existsSync(bundledUv)) {
  throw new Error("bundled uv was not installed under the Pi agent directory");
}
if (existsSync(pytest)) {
  throw new Error("development dependencies leaked into the release runtime environment");
}
if (existsSync(join(packageRoot, "sidecar", ".venv"))) {
  throw new Error("release runtime wrote a virtual environment inside the installed package");
}

console.log("release package smoke test passed");
