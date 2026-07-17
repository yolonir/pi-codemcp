import { expect, test } from "bun:test";
import type { ExtensionCommandContext, Theme } from "@earendil-works/pi-coding-agent";
import type { Component } from "@earendil-works/pi-tui";
import {
  type ServerModalState,
  serverStatesFromStatus,
  showServerManagerModal,
} from "../../src/modal.js";
import { DEFAULT_CODEMCP_SETTINGS } from "../../src/settings.js";

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
  let component: Component | undefined;
  let overlayOptions: Record<string, unknown> | undefined;
  const toggles: Array<{ name: string; enabled: boolean }> = [];
  const toolToggles: Array<{ name: string; enabled: boolean }> = [];
  const settingChanges: Array<{ key: string; value: boolean | number }> = [];
  const discoveries: string[] = [];

  const ctx = {
    mode: "tui",
    ui: {
      notify() {},
      async custom(factory: ModalFactory, options: Record<string, unknown>) {
        overlayOptions = options;
        component = factory(
          { requestRender() {} },
          theme,
          { matches: () => false },
          () => undefined,
        );
      },
    },
  } as unknown as ExtensionCommandContext;

  await showServerManagerModal(ctx, {
    servers,
    settings: { ...DEFAULT_CODEMCP_SETTINGS, disabledTools: {} },
    async onSetServerEnabled(server, enabled) {
      toggles.push({ name: server.name, enabled });
      return { ...server, enabled };
    },
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
    async onSetToolEnabled(server, tool, enabled) {
      toolToggles.push({ name: tool.name, enabled });
      return {
        ...server,
        toolCount: enabled ? 1 : 0,
        tools: server.tools.map((candidate) =>
          candidate.name === tool.name ? { ...candidate, enabled } : candidate,
        ),
      };
    },
    async onSetSetting(key, value) {
      settingChanges.push({ key, value });
      return { ...DEFAULT_CODEMCP_SETTINGS, [key]: value, disabledTools: {} };
    },
  });

  expect(overlayOptions).toEqual({
    overlay: true,
    overlayOptions: { width: "90%", minWidth: 72, maxHeight: "85%" },
  });
  expect(component).toBeDefined();
  const lines = component?.render(90) ?? [];
  expect(lines[0]).toBe(`╭${"─".repeat(88)}╮`);
  expect(lines.at(-1)).toBe(`╰${"─".repeat(88)}╯`);
  expect(lines.length).toBeLessThanOrEqual(Math.floor(24 * 0.85));
  const header = lines.find((line) => line.includes("[Servers]"));
  expect(header).toContain("CodeMCP");
  expect(lines.join("\n")).toContain("grafana");
  expect(lines.join("\n")).toContain("[D] Discover tools");

  component?.handleInput?.("D");
  await Bun.sleep(0);
  expect(discoveries).toEqual(["grafana"]);
  expect(servers[0]?.tools[0]).toMatchObject({ name: "query", enabled: true });
  const originalRows = process.stdout.rows;
  Object.defineProperty(process.stdout, "rows", { value: 60, configurable: true });
  try {
    const wideLines = component?.render(140) ?? [];
    const cardTop = wideLines.findIndex((line) => line.includes("╭─ query"));
    const toolsHeading = wideLines.findIndex((line) => line.includes("TOOLS"));
    expect(cardTop).toBe(toolsHeading + 1);
    expect(wideLines.join("\n")).toContain("Run a query and return");
    expect(wideLines[cardTop]?.match(/│/g)).toHaveLength(3);
    const narrowLines = component?.render(90) ?? [];
    const narrowFooter = narrowLines.findIndex((line) => line.includes("tab settings"));
    expect(narrowLines[narrowFooter - 1]).toContain("╰──");
  } finally {
    Object.defineProperty(process.stdout, "rows", {
      value: originalRows,
      configurable: true,
    });
  }

  component?.handleInput?.("\u001b[C");
  component?.handleInput?.("\r");
  await Bun.sleep(0);
  expect(toolToggles).toEqual([{ name: "query", enabled: false }]);
  expect(servers[0]?.toolCount).toBe(0);

  component?.handleInput?.("\u001b[D");
  component?.handleInput?.("\r");
  await Bun.sleep(0);
  expect(toggles).toEqual([{ name: "grafana", enabled: false }]);
  expect(servers[0]?.enabled).toBe(false);

  component?.handleInput?.("\t");
  expect((component?.render(90) ?? []).join("\n")).toContain("[Settings]");
  component?.handleInput?.("\u001b[C");
  await Bun.sleep(0);
  expect(settingChanges).toEqual([{ key: "backgroundWarmup", value: false }]);
});
