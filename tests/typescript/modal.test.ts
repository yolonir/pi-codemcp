import { expect, test } from "bun:test";
import type { ExtensionCommandContext, Theme } from "@earendil-works/pi-coding-agent";
import type { Component } from "@earendil-works/pi-tui";
import {
  type ChainEnabledChange,
  type ChainModalState,
  type ServerEnabledChange,
  type ServerManagerResult,
  type ServerModalState,
  serverStatesFromStatus,
  showServerManagerModal,
  statsStateFromSnapshot,
} from "../../src/modal.js";
import { type CodeMcpSettings, DEFAULT_CODEMCP_SETTINGS } from "../../src/settings.js";

type ModalFactory = (
  tui: { requestRender(): void },
  theme: Theme,
  keybindings: { matches(data: string, id: string): boolean },
  done: (result?: ServerManagerResult) => void,
) => Component;

const theme = {
  fg: (_color: string, text: string) => text,
  bold: (text: string) => text,
} as unknown as Theme;

function renderWithRows(component: Component | undefined, width: number, rows: number): string[] {
  const descriptor = Object.getOwnPropertyDescriptor(process.stdout, "rows");
  Object.defineProperty(process.stdout, "rows", { value: rows, configurable: true });
  try {
    return component?.render(width) ?? [];
  } finally {
    if (descriptor) Object.defineProperty(process.stdout, "rows", descriptor);
    else Reflect.deleteProperty(process.stdout, "rows");
  }
}

test("server status parser preserves server and tool policy state", () => {
  expect(
    serverStatesFromStatus({
      upstreams: [
        {
          name: "grafana",
          transport: "http",
          auth: "oauth",
          enabled: true,
          connected: true,
          discovered: true,
          tool_count: 1,
          total_tool_count: 2,
          tools: [
            { name: "query", enabled: true, description: "Run a query" },
            { name: "admin", enabled: false },
          ],
        },
        {
          name: "disabled",
          transport: "stdio",
          enabled: false,
          connected: false,
          discovered: false,
          tool_count: 0,
        },
      ],
    }),
  ).toEqual([
    {
      name: "grafana",
      transport: "http",
      auth: "oauth",
      enabled: true,
      connected: true,
      discovered: true,
      toolCount: 1,
      totalToolCount: 2,
      tools: [
        { name: "query", enabled: true, description: "Run a query" },
        { name: "admin", enabled: false },
      ],
    },
    {
      name: "disabled",
      transport: "stdio",
      enabled: false,
      connected: false,
      discovered: false,
      toolCount: 0,
      totalToolCount: 0,
      tools: [],
    },
  ]);
});

test("stats parser converts bounded rollups for the modal", () => {
  expect(
    statsStateFromSnapshot({
      updated_at: 100,
      lifetime: {
        count: 5,
        success: 4,
        failure: 1,
        input_bytes: 100,
        output_bytes: 200,
        calls: 8,
        chain_calls: 2,
        duration_ms: {
          count: 5,
          average: 12.5,
          max: 30,
          buckets: [
            { le: 10, count: 3 },
            { le: 50, count: 2 },
          ],
        },
        output_size_bytes: {
          count: 5,
          buckets: [
            { le: 64, count: 3 },
            { le: 256, count: 2 },
          ],
        },
      },
      recent: [{ count: 2, success: 2, failure: 0 }],
      operations: {
        execute: { count: 5, success: 4, failure: 1, duration_ms: { average: 12.5, max: 30 } },
      },
      phases: {
        typecheck: {
          count: 5,
          average: 4,
          max: 9,
          buckets: [
            { le: 5, count: 4 },
            { le: 10, count: 1 },
          ],
        },
      },
      failures: { runtime: 2 },
      servers: { grafana: { output_bytes: 500 } },
      tools: { "grafana.query": {} },
      cache: { hits: 3, misses: 1 },
    }),
  ).toMatchObject({
    updatedAt: 100,
    lifetime: {
      count: 5,
      success: 4,
      failure: 1,
      calls: 8,
      chainCalls: 2,
      p50Ms: 10,
      p95Ms: 50,
      p50OutputBytes: 64,
      p95OutputBytes: 256,
    },
    recent: { count: 2, success: 2 },
    operations: [{ name: "execute", rollup: { count: 5 } }],
    phases: [{ name: "typecheck", count: 5, averageMs: 4, p50Ms: 5, p95Ms: 10, maxMs: 9 }],
    failures: [{ stage: "runtime", count: 2 }],
    upstreamOutputBytes: 500,
    cacheHits: 3,
    cacheMisses: 1,
    serverCount: 1,
    toolCount: 1,
  });
});

test("server manager renders split tabs, stats, discovers, and toggles", async () => {
  const servers: ServerModalState[] = [
    {
      name: "grafana",
      transport: "http",
      auth: "oauth",
      enabled: true,
      connected: false,
      discovered: false,
      toolCount: 0,
      totalToolCount: 0,
      tools: [],
    },
  ];
  const chains: ChainModalState[] = [
    {
      name: "weekly_digest",
      scope: "project",
      description: "Collect Linear issues and post a weekly digest to Slack.",
      nativeTool: "mcp_chain_weekly_digest",
      code: "return {}",
      enabled: true,
      status: "ready",
      inputSchema: {
        type: "object",
        properties: { assignee: { type: "string" } },
        required: ["assignee"],
      },
      outputSchema: {
        type: "object",
        properties: { posted: { type: "boolean" } },
        required: ["posted"],
      },
      dependencies: [
        { kind: "mcp_tool", call: "linear.list_issues", server: "linear" },
        { kind: "mcp_tool", call: "slack.post_message", server: "slack" },
      ],
      staleDependencies: [],
      calledBy: ["monthly_digest"],
    },
  ];
  let component: Component | undefined;
  let overlayOptions: Record<string, unknown> | undefined;
  const savedChanges: Array<{
    settings: CodeMcpSettings;
    serverChanges: ServerEnabledChange[];
    chainChanges: ChainEnabledChange[];
  }> = [];
  const discoveries: string[] = [];
  const revalidated: string[] = [];
  let unsavedAction: "save" | "discard" | "cancel" = "cancel";
  let unsavedPrompts = 0;
  let closed = 0;
  let modalResult: ServerManagerResult;

  const ctx = {
    mode: "tui",
    ui: {
      notify() {},
      async custom(factory: ModalFactory, options: Record<string, unknown>) {
        overlayOptions = options;
        component = factory({ requestRender() {} }, theme, { matches: () => false }, (result) => {
          closed += 1;
          modalResult = result;
        });
      },
    },
  } as unknown as ExtensionCommandContext;

  await showServerManagerModal(ctx, {
    servers,
    chains,
    settings: { ...DEFAULT_CODEMCP_SETTINGS, disabledTools: {} },
    stats: statsStateFromSnapshot({
      updated_at: 100,
      lifetime: {
        count: 100_000,
        success: 90_000,
        failure: 10_000,
        calls: 200_000,
        chain_calls: 2,
        input_bytes: 100,
        output_bytes: 200,
        duration_ms: { count: 5, average: 12.5, max: 30, buckets: [{ le: 25, count: 5 }] },
        output_size_bytes: { count: 5, buckets: [{ le: 256, count: 5 }] },
      },
      operations: {
        execute: { count: 5, success: 4, failure: 1, duration_ms: { average: 12.5, max: 30 } },
      },
      phases: {
        typecheck: { count: 5, average: 4, max: 9, buckets: [{ le: 5, count: 5 }] },
      },
      failures: { runtime: 1 },
      servers: { grafana: { output_bytes: 500 } },
      cache: { hits: 3, misses: 1 },
    }),
    async onDiscover(server) {
      discoveries.push(server.name);
      return {
        ...server,
        discovered: true,
        toolCount: 1,
        totalToolCount: 1,
        tools: [
          {
            name: "query",
            enabled: true,
            description: "Run a query and return the selected rows from the upstream database.",
          },
        ],
      };
    },
    async onSaveChanges(settings, serverChanges, chainChanges) {
      savedChanges.push({ settings, serverChanges, chainChanges });
      return {
        settings,
        servers: servers.map((server) => ({
          ...server,
          tools: server.tools.map((tool) => ({ ...tool })),
        })),
        chains: chains.map((chain) => ({ ...chain })),
      };
    },
    async onResolveUnsaved() {
      unsavedPrompts += 1;
      return unsavedAction;
    },
    async onRevalidateChain(chain) {
      revalidated.push(chain.name);
      return chains;
    },
    async onDeleteChain() {
      return chains;
    },
  });

  expect(overlayOptions).toEqual({
    overlay: true,
    overlayOptions: { width: "90%", minWidth: 72, maxHeight: "85%" },
  });
  expect(component).toBeDefined();
  const lines = renderWithRows(component, 90, 0);
  expect(lines[0]).toBe(`╭${"─".repeat(88)}╮`);
  expect(lines.at(-1)).toBe(`╰${"─".repeat(88)}╯`);
  expect(lines.length).toBeLessThanOrEqual(Math.floor(24 * 0.85));
  const header = lines.find((line) => line.includes("[Servers]"));
  expect(header).toContain("CodeMCP");
  expect(lines.join("\n")).toContain("grafana");
  expect(lines.join("\n")).toContain("[d] Discover tools");
  expect(lines.join("\n")).toContain("Report issue: R");

  component?.handleInput?.("R");
  const problemReportLines = renderWithRows(component, 90, 60).join("\n");
  expect(problemReportLines).toContain("[Settings]");
  expect(problemReportLines).toContain("→ Extension is broken!");
  expect(problemReportLines).toContain("Well, that sucks.");
  for (let index = 0; index < 7; index += 1) component?.handleInput?.("\u001b[A");
  component?.handleInput?.("\t");

  component?.handleInput?.("d");
  await Bun.sleep(0);
  expect(discoveries).toEqual(["grafana"]);
  expect(servers[0]?.tools[0]).toMatchObject({ name: "query", enabled: true });
  const wideLines = renderWithRows(component, 140, 60);
  const cardTop = wideLines.findIndex((line) => line.includes("╭─ query"));
  const toolsHeading = wideLines.findIndex((line) => line.includes("TOOLS"));
  expect(cardTop).toBe(toolsHeading + 1);
  expect(wideLines.join("\n")).toContain("Run a query and return");
  expect(wideLines[cardTop]?.match(/│/g)).toHaveLength(3);
  const narrowLines = renderWithRows(component, 90, 60);
  const narrowFooter = narrowLines.findIndex((line) => line.includes("tab chains"));
  expect(narrowLines[narrowFooter - 1]).toContain("╰──");
  expect(narrowLines.join("\n")).toContain("Report issue: R");

  component?.handleInput?.("\u001b[C");
  component?.handleInput?.("\r");
  await Bun.sleep(0);
  expect(savedChanges).toEqual([]);
  expect(servers[0]?.tools[0]?.enabled).toBe(false);
  expect(servers[0]?.toolCount).toBe(0);

  component?.handleInput?.("\u001b[D");
  component?.handleInput?.("\r");
  await Bun.sleep(0);
  expect(savedChanges).toEqual([]);
  expect(servers[0]?.enabled).toBe(false);
  component?.handleInput?.("d");
  await Bun.sleep(0);
  expect(discoveries).toEqual(["grafana"]);
  expect((component?.render(100) ?? []).join("\n")).toContain(
    "Save this server change before discovering tools",
  );

  component?.handleInput?.("\t");
  const renderedChainLines = renderWithRows(component, 120, 60);
  const chainLines = renderedChainLines.join("\n");
  expect(chainLines).toContain("[Chains]");
  expect(chainLines).toContain("weekly_digest");
  expect(chainLines).toContain("[P]");
  expect(chainLines).toContain("project · ready · mcp_chain_weekly_digest");
  expect(chainLines).toContain("linear.list_issues");
  expect(chainLines).toContain("assignee: string");
  const inputSection = renderedChainLines.findIndex((line) => line.includes("INPUT"));
  const outputSection = renderedChainLines.findIndex((line) => line.includes("OUTPUT"));
  const serversSection = renderedChainLines.findIndex((line) => line.includes("SERVERS"));
  const dependenciesSection = renderedChainLines.findIndex((line) => line.includes("DEPENDENCIES"));
  const calledBySection = renderedChainLines.findIndex((line) => line.includes("CALLED BY"));
  expect(outputSection).toBe(inputSection + 3);
  expect(serversSection).toBe(outputSection + 3);
  expect(dependenciesSection).toBe(serversSection + 3);
  expect(calledBySection).toBe(dependenciesSection + 4);
  component?.handleInput?.("r");
  await Bun.sleep(0);
  expect(revalidated).toEqual(["weekly_digest"]);
  component?.handleInput?.(" ");
  await Bun.sleep(0);
  expect(chains[0]?.enabled).toBe(false);
  expect(savedChanges).toEqual([]);
  component?.handleInput?.("r");
  await Bun.sleep(0);
  expect(revalidated).toEqual(["weekly_digest"]);
  expect((component?.render(120) ?? []).join("\n")).toContain(
    "Save this chain change before revalidation",
  );

  component?.handleInput?.("\t");
  const statsLines = renderWithRows(component, 100, 60).join("\n");
  expect(statsLines).toContain("[Stats]");
  expect(statsLines).toContain("LOCAL TELEMETRY");
  expect(statsLines).toContain("execute");
  expect(statsLines).toContain("Withheld");
  expect(statsLines).toContain("runtime");
  expect(statsLines).toContain("typecheck");
  expect(statsLines).toContain("100,000 runs");
  const renderStarted = performance.now();
  for (let index = 0; index < 100; index += 1) component?.render(100);
  expect(performance.now() - renderStarted).toBeLessThan(500);

  component?.handleInput?.("\t");
  expect((component?.render(90) ?? []).join("\n")).toContain("[Settings]");
  expect((component?.render(90) ?? []).join("\n")).toContain("Extension is broken!");
  component?.handleInput?.("\u001b[C");
  await Bun.sleep(0);
  expect(savedChanges).toEqual([]);
  expect((component?.render(90) ?? []).join("\n")).toContain("Unsaved changes");

  component?.handleInput?.("\u0013");
  await Bun.sleep(0);
  expect(savedChanges).toHaveLength(1);
  expect(savedChanges[0]?.settings).toMatchObject({
    backgroundWarmup: false,
    disabledTools: { grafana: ["query"] },
  });
  expect(savedChanges[0]?.serverChanges).toEqual([
    { name: "grafana", previousEnabled: true, enabled: false },
  ]);
  expect(savedChanges[0]?.chainChanges).toEqual([
    { name: "weekly_digest", scope: "project", previousEnabled: true, enabled: false },
  ]);

  component?.handleInput?.("\u001b[C");
  component?.handleInput?.("\u001b");
  await Bun.sleep(0);
  expect(unsavedPrompts).toBe(1);
  expect(closed).toBe(0);
  unsavedAction = "save";
  component?.handleInput?.("\u001b");
  await Bun.sleep(0);
  expect(unsavedPrompts).toBe(2);
  expect(savedChanges).toHaveLength(2);
  expect(savedChanges[1]?.settings.backgroundWarmup).toBe(true);
  expect(savedChanges[1]?.serverChanges).toEqual([]);
  expect(savedChanges[1]?.chainChanges).toEqual([]);
  expect(closed).toBe(1);

  for (let index = 0; index < 7; index += 1) component?.handleInput?.("\u001b[B");
  expect((component?.render(90) ?? []).join("\n")).toContain("Well, that sucks.");
  component?.handleInput?.("\r");
  expect(closed).toBe(2);
  expect(modalResult).toBe("report-problem");
});
