import { expect, test } from "bun:test";
import type { ExtensionCommandContext, Theme } from "@earendil-works/pi-coding-agent";
import type { Component } from "@earendil-works/pi-tui";
import {
  type ChainEnabledChange,
  type ChainModalState,
  type ServerEnabledChange,
  type ServerModalState,
  serverStatesFromStatus,
  showServerManagerModal,
} from "../../src/modal.js";
import { type CodeMcpSettings, DEFAULT_CODEMCP_SETTINGS } from "../../src/settings.js";

type ModalFactory = (
  tui: { requestRender(): void },
  theme: Theme,
  keybindings: { matches(data: string, id: string): boolean },
  done: () => void,
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

test("server manager renders split tabs, discovers, and toggles", async () => {
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

  const ctx = {
    mode: "tui",
    ui: {
      notify() {},
      async custom(factory: ModalFactory, options: Record<string, unknown>) {
        overlayOptions = options;
        component = factory({ requestRender() {} }, theme, { matches: () => false }, () => {
          closed += 1;
        });
      },
    },
  } as unknown as ExtensionCommandContext;

  await showServerManagerModal(ctx, {
    servers,
    chains,
    settings: { ...DEFAULT_CODEMCP_SETTINGS, disabledTools: {} },
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
  expect((component?.render(90) ?? []).join("\n")).toContain("[Settings]");
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
});
