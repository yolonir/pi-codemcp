import { describe, expect, test } from "bun:test";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { createCodeMcpExtension, setServerEnabledFromManager } from "../../extensions/index.js";

describe("Pi extension registration", () => {
  test("registers search, execute, save, and one manager command", () => {
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
      "codemcp_execute",
      "codemcp_save_chain",
    ]);
    expect(commands).toEqual(["codemcp"]);
    expect(events).toEqual(["session_start", "session_shutdown"]);

    const search = tools[0];
    expect(search?.description).toBe(
      "Search configured upstream MCP tools and saved chains, returning their typed SDK stubs.",
    );
    expect(search?.parameters).toMatchObject({
      properties: { server: { type: "string" } },
    });
    expect(tools[2]?.parameters).toMatchObject({
      properties: {
        scope: { type: "string", enum: ["project", "global"] },
      },
    });
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

  test("enabling a server reloads config and immediately discovers tools", async () => {
    const temporary = await mkdtemp(join(tmpdir(), "pi-codemcp-enable-"));
    const configPath = join(temporary, "mcp.json");
    try {
      await writeFile(
        configPath,
        JSON.stringify({ mcpServers: { alpha: { command: "alpha", disabled: true } } }),
        "utf8",
      );
      const calls: string[] = [];
      const lifecycle = {
        configPath,
        async reload() {
          calls.push("reload");
        },
        async request(name: "search" | "discover" | "reload_settings" | "execute" | "status") {
          calls.push(name);
          return {
            upstreams: [
              {
                name: "alpha",
                transport: "stdio",
                enabled: true,
                discovered: true,
                tool_count: 1,
                total_tool_count: 1,
                tools: [{ name: "run", enabled: true }],
              },
            ],
          };
        },
      };

      const updated = await setServerEnabledFromManager(
        lifecycle,
        {
          name: "alpha",
          transport: "stdio",
          enabled: false,
          connected: false,
          discovered: false,
          toolCount: 0,
          totalToolCount: 0,
          tools: [],
        },
        true,
      );

      expect(calls).toEqual(["reload", "discover"]);
      expect(updated).toMatchObject({ enabled: true, discovered: true, toolCount: 1 });
      expect(JSON.parse(await readFile(configPath, "utf8"))).toEqual({
        mcpServers: { alpha: { command: "alpha", disabled: false } },
      });
    } finally {
      await rm(temporary, { recursive: true, force: true });
    }
  });
});
