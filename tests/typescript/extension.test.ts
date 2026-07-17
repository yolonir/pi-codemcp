import { describe, expect, test } from "bun:test";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { createCodeMcpExtension, setServerEnabledFromManager } from "../../extensions/index.js";

describe("Pi extension registration", () => {
  test("registers exactly search/execute and one manager command", () => {
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

    expect(tools.map((tool) => tool.name)).toEqual(["codemcp_search", "codemcp_execute"]);
    expect(commands).toEqual(["codemcp"]);
    expect(events).toEqual(["session_start", "session_shutdown"]);

    const search = tools[0];
    expect(search?.description).toBe(
      "Search configured upstream MCP tools and return their typed SDK stubs.",
    );
    expect(search?.parameters).toMatchObject({
      properties: { server: { type: "string" } },
    });
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
