import { existsSync } from "node:fs";
import { mkdir, readdir, readFile, writeFile } from "node:fs/promises";
import { join, resolve } from "node:path";
import { SidecarClient } from "../../src/mcp-client.js";

const [rawPackageRoot, rawAgentDir] = process.argv.slice(2);
if (!rawPackageRoot || !rawAgentDir) {
  throw new Error("usage: package-smoke.ts PACKAGE_ROOT AGENT_DIR");
}

const packageRoot = resolve(rawPackageRoot);
const agentDir = resolve(rawAgentDir);
const projectChainsPath = join(agentDir, "project", ".pi", "pi-codemcp", "chains");
const traceId = "release-smoke";
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

const client = new SidecarClient({ packageRoot, agentDir, projectChainsPath });
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

  const saved = await client.call("save_chain", {
    trace_id: traceId,
    scope: "project",
    name: "release_echo",
    description: "Exercise project-scoped persistence from the packed release.",
    code: 'return {"value": input["value"]}',
    input_schema: {
      type: "object",
      properties: { value: { type: "integer" } },
      required: ["value"],
      additionalProperties: false,
    },
    output_schema: {
      type: "object",
      properties: { value: { type: "integer" } },
      required: ["value"],
      additionalProperties: false,
    },
  });
  if (saved.created !== true) {
    throw new Error(`project chain was not created: ${JSON.stringify(saved)}`);
  }
  const projectManifest = join(projectChainsPath, "release_echo.json");
  if (!existsSync(projectManifest)) {
    throw new Error(`project chain was not written to ${projectManifest}`);
  }
  if (existsSync(join(agentDir, "pi-codemcp", "chains", "release_echo.json"))) {
    throw new Error("project chain leaked into the global chain store");
  }
  const manifest = JSON.parse(await readFile(projectManifest, "utf8"));
  if (manifest.name !== "release_echo") {
    throw new Error(`unexpected project chain manifest: ${JSON.stringify(manifest)}`);
  }
  const executed = await client.call("execute_chain", {
    trace_id: traceId,
    name: "release_echo",
    arguments: { value: 7 },
  });
  if (executed.ok !== true || JSON.stringify(executed.result) !== JSON.stringify({ value: 7 })) {
    throw new Error(`project chain execution failed: ${JSON.stringify(executed)}`);
  }
  const disabled = await client.call("set_chain_enabled", {
    trace_id: traceId,
    name: "release_echo",
    scope: "project",
    enabled: false,
  });
  if (disabled.status !== "disabled") {
    throw new Error(`chain was not disabled immediately: ${JSON.stringify(disabled)}`);
  }
  const blocked = await client.call("execute_chain", {
    trace_id: traceId,
    name: "release_echo",
    arguments: { value: 8 },
  });
  if (blocked.ok !== false || blocked.failure_stage !== "preflight") {
    throw new Error(`disabled project chain still executed: ${JSON.stringify(blocked)}`);
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
