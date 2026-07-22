import { expect, test } from "bun:test";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import type { SavedChainView } from "../../src/chains.js";
import { DEFAULT_CODEMCP_SETTINGS } from "../../src/settings.js";
import { registerCodeMcpTools } from "../../src/tools.js";

interface RegisteredTool {
  name: string;
  execute(
    toolCallId: string,
    params: Record<string, unknown>,
    signal: AbortSignal | undefined,
    onUpdate: undefined,
  ): Promise<{
    content: Array<{ type: string; text: string }>;
    details?: Record<string, unknown>;
  }>;
}

const view: SavedChainView = {
  scope: "project",
  status: "ready",
  staleDependencies: [],
  calledBy: [],
  chain: {
    version: 1,
    id: "chain-id",
    name: "daily",
    description: "Daily chain",
    code: "return {}",
    inputSchema: { type: "object", properties: {} },
    outputSchema: { type: "object", properties: {} },
    enabled: true,
    dependencies: [],
    schemaFingerprint: "fingerprint",
    createdAt: 1,
    updatedAt: 1,
    validatedAt: 1,
  },
};

function captureTools(options: {
  projectAvailable: boolean;
  executeResult?: Record<string, unknown>;
  searchResult?: Record<string, unknown>;
}) {
  const tools = new Map<string, RegisteredTool>();
  const pi = {
    registerTool(tool: RegisteredTool) {
      tools.set(tool.name, tool);
    },
  } as unknown as ExtensionAPI;
  const lifecycle = {
    projectChainsPath: options.projectAvailable ? "/project/chains" : undefined,
    loadSettings: () => ({ ...DEFAULT_CODEMCP_SETTINGS, disabledTools: {} }),
    async request(name: string) {
      if (name === "execute" && options.executeResult) return options.executeResult;
      if (name === "search" && options.searchResult) return options.searchResult;
      throw new Error("unexpected sidecar request");
    },
  };
  const calls: string[] = [];
  const chains = {
    async list() {
      calls.push("list");
      return [view];
    },
    async applyEnabled(changes: Array<{ enabled: boolean }>) {
      calls.push(changes[0]?.enabled ? "enable" : "disable");
      return { chains: [view], status: {} };
    },
    async revalidate() {
      calls.push("revalidate");
      return view;
    },
    async delete() {
      calls.push("delete");
      return [];
    },
    async save() {
      calls.push("save");
      return view;
    },
  };
  registerCodeMcpTools(pi, lifecycle as never, chains as never);
  return { tools, calls };
}

test("execute sends only compact result while keeping metadata in details", async () => {
  const { tools } = captureTools({
    projectAvailable: true,
    executeResult: {
      ok: true,
      result: { value: 2 },
      calls_made: 1,
      chain_calls: 0,
      timings: { typecheck_ms: 4, execution_ms: 8, serialization_ms: 1 },
    },
  });
  const execute = tools.get("codemcp_execute");
  const result = await execute?.execute("id", { code: "return 2" }, undefined, undefined);
  expect(result?.content[0]?.text).toBe('{"value":2}');
  expect(result?.content[0]?.text).not.toContain("calls_made");
  expect(result?.details).toMatchObject({
    ok: true,
    callsMade: 1,
    chainCalls: 0,
    timings: { typecheck_ms: 4, execution_ms: 8, serialization_ms: 1 },
  });
});

test("search exposes unavailable servers as partial-result metadata", async () => {
  const { tools } = captureTools({
    projectAvailable: true,
    searchResult: {
      mode: "search",
      detail: "signatures",
      total_tool_count: 1,
      servers: [{ name: "grafana", tool_count: 1 }],
      discovery_failures: [{ server: "marimo-research", error: "All connection attempts failed" }],
      results: [{ call: "grafana.check_health" }],
    },
  });
  const search = tools.get("codemcp_search");
  const result = await search?.execute("id", { query: "health" }, undefined, undefined);
  expect(result?.content[0]?.text).toContain('"discovery_failures"');
  expect(result?.details).toMatchObject({
    matchCount: 1,
    totalToolCount: 1,
    serverCount: 1,
    discoveryFailures: ["marimo-research"],
  });
});

test("chain management lists freely and requires explicit mutation confirmation", async () => {
  const { tools, calls } = captureTools({ projectAvailable: true });
  const manage = tools.get("codemcp_manage_chains");
  expect(manage).toBeDefined();

  const listed = await manage?.execute("id", { action: "list" }, undefined, undefined);
  expect(calls).toEqual(["list"]);
  expect(listed?.content[0]?.text).toContain('"name":"daily"');

  await expect(
    manage?.execute(
      "id",
      { action: "disable", name: "daily", scope: "project" },
      undefined,
      undefined,
    ),
  ).rejects.toThrow("confirmedByUser=true");
  expect(calls).toEqual(["list"]);

  await manage?.execute(
    "id",
    {
      action: "disable",
      name: "daily",
      scope: "project",
      confirmedByUser: true,
    },
    undefined,
    undefined,
  );
  expect(calls).toEqual(["list", "disable"]);
});

test("project chain saves fail before persistence when scope is unavailable", async () => {
  const { tools, calls } = captureTools({ projectAvailable: false });
  const save = tools.get("codemcp_save_chain");
  await expect(
    save?.execute(
      "id",
      {
        name: "daily",
        description: "Daily chain",
        code: "return {}",
        inputSchema: { type: "object" },
        outputSchema: { type: "object" },
      },
      undefined,
      undefined,
    ),
  ).rejects.toThrow("Project saved-chain scope is unavailable");
  expect(calls).toEqual([]);
});
