import { describe, expect, test } from "bun:test";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import {
  createCodeMcpExtension,
  promptForProblemReport,
  saveManagerChanges,
} from "../../extensions/index.js";
import { DEFAULT_CODEMCP_SETTINGS, loadCodeMcpSettings } from "../../src/settings.js";

describe("Pi extension registration", () => {
  test("problem report asks for a description and sends the agent an issue-preparation prompt", async () => {
    const messages: string[] = [];
    const fakePi = {
      sendUserMessage(content: string) {
        messages.push(content);
      },
    } as unknown as ExtensionAPI;
    const ctx = {
      ui: {
        async editor(title: string, initial: string) {
          expect(title).toBe("What went wrong?");
          expect(initial).toBe("");
          return "OAuth fails after reconnect";
        },
      },
    } as unknown as Parameters<typeof promptForProblemReport>[1];

    await promptForProblemReport(fakePi, ctx);

    expect(messages).toHaveLength(1);
    expect(messages[0]).toContain("User's description:\nOAuth fails after reconnect");
    expect(messages[0]).toContain("https://github.com/yolonir/pi-codemcp");
    expect(messages[0]).toContain("prepare a GitHub issue");
    expect(messages[0]).toContain("Do not autosumbit issue without clear approval");
  });

  test("registers search, inspect, execute, save, and one manager command", () => {
    const tools: Array<{ name: string; description?: string; parameters?: unknown }> = [];
    const commands: string[] = [];
    const events: string[] = [];
    const fakePi = {
      registerTool(tool: { name: string; description?: string; parameters?: unknown }) {
        tools.push(tool);
      },
      registerCommand(name: string) {
        commands.push(name);
      },
      on(name: string) {
        events.push(name);
      },
    } as unknown as ExtensionAPI;

    createCodeMcpExtension()(fakePi);

    expect(tools.map((tool) => tool.name)).toEqual([
      "codemcp_search",
      "codemcp_inspect",
      "codemcp_execute",
      "codemcp_save_chain",
      "codemcp_manage_chains",
    ]);
    expect(commands).toEqual(["codemcp"]);
    expect(events).toEqual(["session_start", "session_shutdown"]);

    const search = tools[0];
    expect(search?.description).toContain("compact inventory");
    expect(search?.description).toContain("codemcp_inspect");
    expect(search?.parameters).toMatchObject({
      properties: {
        server: { type: "string" },
        mode: { enum: ["search", "inventory"] },
        detail: { enum: ["names", "signatures", "full"] },
        cursor: { type: "integer" },
      },
    });
    expect(tools[1]?.parameters).toMatchObject({
      properties: { calls: { type: "array" } },
    });
    expect(tools[3]?.parameters).toMatchObject({
      properties: {
        scope: { type: "string", enum: ["project", "global"] },
      },
    });
    expect(tools[4]?.parameters).toMatchObject({
      properties: {
        action: { enum: ["list", "enable", "disable", "revalidate", "delete"] },
        confirmedByUser: { type: "boolean" },
      },
    });
  });

  test("manager batch-save applies settings, servers, and chains together", async () => {
    const temporary = await mkdtemp(join(tmpdir(), "pi-codemcp-manager-save-"));
    const configPath = join(temporary, "mcp.json");
    const settingsPath = join(temporary, "settings.json");
    const reloads: string[] = [];
    const chainBatches: unknown[] = [];
    try {
      await writeFile(
        configPath,
        JSON.stringify({ mcpServers: { alpha: { command: "alpha", enabled: true } } }),
      );
      const result = await saveManagerChanges(
        {
          configPath,
          settingsPath,
          loadSettings: () => loadCodeMcpSettings(settingsPath),
          async reload() {
            reloads.push("reload");
          },
        },
        {
          async applyEnabled(changes) {
            chainBatches.push(changes);
            return {
              status: {
                connected: false,
                tool_count: 0,
                upstreams: [
                  {
                    name: "alpha",
                    transport: "stdio",
                    enabled: false,
                    discovered: false,
                    tool_count: 0,
                    total_tool_count: 0,
                    tools: [],
                  },
                ],
              },
              chains: [],
            };
          },
        },
        { ...DEFAULT_CODEMCP_SETTINGS, backgroundWarmup: false },
        [{ name: "alpha", previousEnabled: true, enabled: false }],
        [{ name: "daily", scope: "project", previousEnabled: true, enabled: false }],
      );

      expect(reloads).toEqual(["reload"]);
      expect(chainBatches).toEqual([[{ name: "daily", scope: "project", enabled: false }]]);
      expect(result.settings.backgroundWarmup).toBe(false);
      expect(result.servers[0]).toMatchObject({ name: "alpha", enabled: false });
      expect(JSON.parse(await readFile(configPath, "utf8"))).toEqual({
        mcpServers: { alpha: { command: "alpha", enabled: false } },
      });
    } finally {
      await rm(temporary, { recursive: true, force: true });
    }
  });

  test("manager batch-save rolls files and runtime back on failure", async () => {
    const temporary = await mkdtemp(join(tmpdir(), "pi-codemcp-manager-rollback-"));
    const configPath = join(temporary, "mcp.json");
    const settingsPath = join(temporary, "settings.json");
    let reloads = 0;
    try {
      await writeFile(
        configPath,
        JSON.stringify({ mcpServers: { alpha: { command: "alpha", enabled: true } } }),
      );
      await expect(
        saveManagerChanges(
          {
            configPath,
            settingsPath,
            loadSettings: () => loadCodeMcpSettings(settingsPath),
            async reload() {
              reloads += 1;
            },
          },
          {
            async applyEnabled() {
              throw new Error("chain batch failed");
            },
          },
          { ...DEFAULT_CODEMCP_SETTINGS, backgroundWarmup: false },
          [{ name: "alpha", previousEnabled: true, enabled: false }],
          [{ name: "daily", scope: "project", previousEnabled: true, enabled: false }],
        ),
      ).rejects.toThrow("chain batch failed");

      expect(reloads).toBe(2);
      expect(loadCodeMcpSettings(settingsPath).backgroundWarmup).toBe(true);
      expect(JSON.parse(await readFile(configPath, "utf8"))).toEqual({
        mcpServers: { alpha: { command: "alpha", enabled: true } },
      });
    } finally {
      await rm(temporary, { recursive: true, force: true });
    }
  });

  test("trusted sessions activate project chains over same-named global chains", async () => {
    const temporary = await mkdtemp(join(tmpdir(), "pi-codemcp-project-scope-"));
    const project = join(temporary, "workspace");
    const globalChains = join(temporary, "pi-codemcp", "chains");
    const projectChains = join(project, ".pi", "pi-codemcp", "chains");
    const manifest = (description: string) => ({
      version: 1,
      id: description,
      name: "shared",
      description,
      code: 'return {"ok": True}',
      input_schema: { type: "object", properties: {} },
      output_schema: { type: "object", properties: { ok: { type: "boolean" } } },
      enabled: true,
      dependencies: [],
      schema_fingerprint: description,
      created_at: 1,
      updated_at: 1,
      validated_at: 1,
    });
    await mkdir(globalChains, { recursive: true });
    await mkdir(projectChains, { recursive: true });
    await writeFile(join(globalChains, "shared.json"), JSON.stringify(manifest("global")));
    await writeFile(join(projectChains, "shared.json"), JSON.stringify(manifest("project")));
    await writeFile(
      join(temporary, "pi-codemcp", "settings.json"),
      JSON.stringify({ backgroundWarmup: false }),
    );

    const tools = new Map<string, { name: string; description?: string }>();
    let active = ["codemcp_search", "codemcp_execute", "codemcp_save_chain"];
    let onSessionStart: ((event: unknown, ctx: unknown) => void) | undefined;
    const fakePi = {
      registerTool(tool: { name: string; description?: string }) {
        tools.set(tool.name, tool);
      },
      registerCommand() {},
      on(name: string, handler: (event: unknown, ctx: unknown) => void) {
        if (name === "session_start") onSessionStart = handler;
      },
      getAllTools() {
        return [...tools.values()];
      },
      getActiveTools() {
        return active;
      },
      setActiveTools(names: string[]) {
        active = names;
      },
    } as unknown as ExtensionAPI;

    try {
      createCodeMcpExtension({ agentDir: temporary })(fakePi);
      expect(onSessionStart).toBeDefined();
      onSessionStart?.(
        {},
        {
          cwd: project,
          isProjectTrusted: () => true,
          ui: { notify() {} },
        },
      );
      expect(tools.get("mcp_chain_shared")?.description).toBe("project");
      expect(active).toContain("mcp_chain_shared");
    } finally {
      await rm(temporary, { recursive: true, force: true });
    }
  });
});
