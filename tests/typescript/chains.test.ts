import { expect, test } from "bun:test";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { ExtensionAPI, Theme } from "@earendil-works/pi-coding-agent";
import type { Component } from "@earendil-works/pi-tui";
import { SavedChainManager } from "../../src/chains.js";
import type { CodeMcpLifecycle } from "../../src/lifecycle.js";
import { DEFAULT_CODEMCP_SETTINGS } from "../../src/settings.js";

interface RegisteredTool {
  name: string;
  description: string;
  parameters: unknown;
  execute(
    id: string,
    params: Record<string, unknown>,
    signal: AbortSignal | undefined,
  ): Promise<{ content: Array<{ type: string; text: string }>; details?: unknown }>;
  renderCall?: (
    args: Record<string, unknown>,
    theme: Theme,
    context: { expanded: boolean },
  ) => Component;
  renderResult?: (
    result: { content: Array<{ type: string; text: string }>; details?: unknown },
    state: { expanded: boolean; isPartial: boolean },
    theme: Theme,
  ) => Component;
}

const plainTheme = {
  fg: (_color: string, text: string) => text,
  bold: (text: string) => text,
} as unknown as Theme;

function manifest(name: string, enabled = true): Record<string, unknown> {
  return {
    version: 1,
    id: `id-${name}`,
    name,
    description: `Run ${name}`,
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
    enabled,
    dependencies: [],
    schema_fingerprint: `schema-${name}`,
    created_at: 1,
    updated_at: 1,
    validated_at: 1,
  };
}

function view(name: string, enabled = true): Record<string, unknown> {
  return {
    chain: manifest(name, enabled),
    status: enabled ? "ready" : "disabled",
    stale_dependencies: [],
    called_by: [],
  };
}

test("saved manifests defer runtime actions and new saves activate immediately", async () => {
  const temporary = await mkdtemp(join(tmpdir(), "pi-codemcp-chains-"));
  const chainsPath = join(temporary, "pi-codemcp", "chains");
  await mkdir(chainsPath, { recursive: true });
  await writeFile(join(chainsPath, "echo_value.json"), JSON.stringify(manifest("echo_value")));

  const tools: RegisteredTool[] = [];
  let active = ["codemcp_search"];
  let runtimeReady = false;
  const requests: Array<{ name: string; args: Record<string, unknown> }> = [];
  const pi = {
    registerTool(tool: RegisteredTool) {
      const previous = tools.findIndex((candidate) => candidate.name === tool.name);
      if (previous >= 0) tools[previous] = tool;
      else tools.push(tool);
    },
    getAllTools() {
      if (!runtimeReady) throw new Error("getAllTools called during extension loading");
      return tools;
    },
    getActiveTools() {
      if (!runtimeReady) throw new Error("getActiveTools called during extension loading");
      return active;
    },
    setActiveTools(names: string[]) {
      if (!runtimeReady) throw new Error("setActiveTools called during extension loading");
      active = names;
    },
  } as unknown as ExtensionAPI;
  const lifecycle = {
    chainsPath,
    loadSettings() {
      return DEFAULT_CODEMCP_SETTINGS;
    },
    async request(name: string, args: Record<string, unknown>) {
      requests.push({ name, args });
      if (name === "execute_chain") {
        return { ok: true, result: { value: 4 }, calls_made: 1, chain_calls: 2 };
      }
      if (name === "save_chain") {
        return { chain: view(String(args.name)), created: true };
      }
      throw new Error(`Unexpected request: ${name}`);
    },
  } as unknown as CodeMcpLifecycle;

  try {
    const manager = new SavedChainManager(pi, lifecycle);
    expect(manager.startupErrors).toEqual([]);
    expect(tools).toEqual([]);
    runtimeReady = true;
    manager.activatePersisted();
    expect(tools.map((tool) => tool.name)).toEqual(["mcp_chain_echo_value"]);
    expect(active).toEqual(["codemcp_search", "mcp_chain_echo_value"]);

    const native = tools[0];
    expect(native?.parameters).toMatchObject({
      type: "object",
      properties: { value: { type: "integer" } },
    });
    const result = await native?.execute("call-1", { value: 4 }, undefined);
    expect(result?.content[0]?.text).toContain('"value": 4');
    const renderedCall = native?.renderCall?.({ value: 4 }, plainTheme, { expanded: false });
    expect(renderedCall?.render(120).join("\n")).toContain("MCP Chain echo_value · 1 argument");
    const renderedResult = result
      ? native?.renderResult?.(result, { expanded: false, isPartial: false }, plainTheme)
      : undefined;
    const compact = renderedResult?.render(120).join("\n") ?? "";
    expect(compact).toContain("✓ Output · 1 MCP call · 2 chain calls");
    expect(compact).toContain("value: 4");
    expect(compact).toContain("arguments and full output");
    expect(compact).not.toContain('"ok"');
    expect(requests[0]).toEqual({
      name: "execute_chain",
      args: { name: "echo_value", arguments: { value: 4 } },
    });

    const saved = await manager.save({
      name: "new_chain",
      description: "Run new_chain",
      code: 'return {"value": input["value"]}',
      inputSchema: {
        type: "object",
        properties: { value: { type: "integer" } },
      },
      outputSchema: {
        type: "object",
        properties: { value: { type: "integer" } },
      },
    });
    expect(saved.chain.name).toBe("new_chain");
    expect(tools.map((tool) => tool.name)).toEqual(["mcp_chain_echo_value", "mcp_chain_new_chain"]);
    expect(active).toContain("mcp_chain_new_chain");
  } finally {
    await rm(temporary, { recursive: true, force: true });
  }
});
