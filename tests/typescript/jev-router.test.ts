import { expect, test } from "bun:test";
import { TypeSafeClient } from "@typesafe-ai/sdk";
import { JevRouter, recentConversation } from "../../src/jev-router.js";

test("Jev selects multiple useful MCP contracts in one model request", async () => {
  const apiRequests: unknown[] = [];
  const client = new TypeSafeClient({
    apiKey: "test-key",
    fetch: async (_input, init) => {
      apiRequests.push(JSON.parse(String(init?.body)));
      return new Response(
        JSON.stringify({
          model: "jev-test",
          answers: {
            needs_any_tool: { type: "noul", noul: 0.97 },
            tool_0: { type: "noul", noul: 0.91 },
            tool_1: { type: "noul", noul: 0.86 },
            tool_2: { type: "noul", noul: 0.04 },
          },
          usage: { input_tokens: 100, output_tokens: 4 },
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      );
    },
  });
  const sidecarRequests: Array<{ name: string; args: Record<string, unknown> }> = [];
  const lifecycle = {
    async request(name: string, args: Record<string, unknown>) {
      sidecarRequests.push({ name, args });
      if (name === "search" && args.cursor === 0) {
        return {
          next_cursor: 2,
          results: [
            { call: "linear.list_issues", description: "List matching Linear issues" },
            { call: "slack.post_message", description: "Post a message to Slack" },
          ],
        };
      }
      if (name === "search" && args.cursor === 2) {
        return {
          next_cursor: null,
          results: [{ call: "grafana.query", description: "Run a Prometheus query" }],
        };
      }
      if (name === "inspect") {
        expect(args.calls).toEqual(["linear.list_issues", "slack.post_message"]);
        return {
          prelude: "type JsonValue = object",
          results: [
            { stub: "async def list_issues(): ..." },
            { stub: "async def post_message(): ..." },
          ],
        };
      }
      throw new Error(`Unexpected sidecar request: ${name}`);
    },
  };

  const result = await new JevRouter(lifecycle, client).route(
    "Summarize my open Linear issues and post them to Slack",
    "Assistant: Which team?\n\nUser: ENG",
  );

  expect(apiRequests).toHaveLength(1);
  expect(apiRequests[0]).toMatchObject({
    state: {
      task: "Summarize my open Linear issues and post them to Slack",
      recent_context: "Assistant: Which team?\n\nUser: ENG",
    },
    questions: {
      needs_any_tool: { type: "noul" },
      tool_0: { type: "noul" },
      tool_1: { type: "noul" },
      tool_2: { type: "noul" },
    },
  });
  expect(result.selected.map((tool) => tool.call)).toEqual([
    "linear.list_issues",
    "slack.post_message",
  ]);
  expect(result.prompt).toContain("async def list_issues(): ...");
  expect(result.prompt).toContain("async def post_message(): ...");
  expect(result.prompt).not.toContain("grafana.query");
  expect(sidecarRequests.map((request) => request.name)).toEqual(["search", "search", "inspect"]);
});

test("recent conversation keeps only useful text context", () => {
  expect(
    recentConversation(
      [
        { type: "message", message: { role: "user", content: "Use the ENG team" } },
        {
          type: "message",
          message: {
            role: "assistant",
            content: [{ type: "text", text: "Ready when you are." }],
          },
        },
        { type: "message", message: { role: "toolResult", content: "ignored" } },
        { type: "message", message: { role: "user", content: "do it" } },
      ],
      "do it",
    ),
  ).toBe("User: Use the ENG team\n\nAssistant: Ready when you are.");
});
