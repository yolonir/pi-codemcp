import { describe, expect, test } from "bun:test";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { createCodeMcpExtension } from "../../extensions/index.js";

describe("Pi extension registration", () => {
  test("registers exactly the three Code Mode tools and one status command", () => {
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
      "codemcp_get_schema",
      "codemcp_execute",
    ]);
    expect(commands).toEqual(["codemcp"]);
    expect(events).toEqual(["session_shutdown"]);

    const search = tools[0];
    expect(search?.description).toBe("Search configured upstream MCP tools by capability.");
    expect(search?.parameters).toMatchObject({
      properties: { server: { type: "string" } },
    });
  });
});
