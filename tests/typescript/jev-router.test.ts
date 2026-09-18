import { expect, test } from "bun:test";
import { TypeSafeClient } from "@typesafe-ai/sdk";
import { JevRouter } from "../../src/jev-router.js";

test("Jev selects and composes multiple MCP contracts in one model request", async () => {
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
            workflow_shape: {
              type: "choice",
              choice: "pipeline",
              confidence: 0.91,
              probabilities: {
                single_call: 0.01,
                parallel: 0.03,
                pipeline: 0.91,
                mixed: 0.05,
              },
            },
            needs_checkpoint: { type: "noul", noul: 0.08 },
            tool_0: { type: "noul", noul: 0.91 },
            role_0: {
              type: "choice",
              choice: "source",
              confidence: 0.9,
              probabilities: {
                source: 0.9,
                enrichment: 0.03,
                sink: 0.01,
                standalone: 0.05,
                irrelevant: 0.01,
              },
            },
            tool_1: { type: "noul", noul: 0.86 },
            role_1: {
              type: "choice",
              choice: "sink",
              confidence: 0.92,
              probabilities: {
                source: 0.01,
                enrichment: 0.02,
                sink: 0.92,
                standalone: 0.04,
                irrelevant: 0.01,
              },
            },
            tool_2: { type: "noul", noul: 0.04 },
            role_2: {
              type: "choice",
              choice: "irrelevant",
              confidence: 0.96,
              probabilities: {
                source: 0.01,
                enrichment: 0.01,
                sink: 0.01,
                standalone: 0.01,
                irrelevant: 0.96,
              },
            },
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
      needs_checkpoint: { type: "noul" },
      tool_0: { type: "noul" },
      role_0: { type: "choice" },
      tool_1: { type: "noul" },
      role_1: { type: "choice" },
      tool_2: { type: "noul" },
      role_2: { type: "choice" },
    },
  });
  expect(result.selected.map((tool) => tool.call)).toEqual([
    "linear.list_issues",
    "slack.post_message",
  ]);
  expect(result.selected.map((tool) => tool.role)).toEqual(["source", "sink"]);
  expect(result.workflowShape).toBe("pipeline");
  expect(result.prompt).toContain("passes earlier outputs into dependent calls");
  expect(result.prompt).toContain("async def list_issues(): ...");
  expect(result.prompt).toContain("async def post_message(): ...");
  expect(result.prompt).not.toContain("grafana.query");
  expect(sidecarRequests.map((request) => request.name)).toEqual(["search", "search", "inspect"]);
});

test("Jev chunks a large catalog in parallel and merges raw answers", async () => {
  let requestCount = 0;
  let inFlight = 0;
  let maxInFlight = 0;
  const client = new TypeSafeClient({
    apiKey: "test-key",
    fetch: async (_input, init) => {
      requestCount += 1;
      inFlight += 1;
      maxInFlight = Math.max(maxInFlight, inFlight);
      await Bun.sleep(10);
      const request = JSON.parse(String(init?.body)) as {
        questions: Record<string, { type: string }>;
      };
      const answers: Record<string, unknown> = {};
      for (const [name, question] of Object.entries(request.questions)) {
        if (name === "needs_any_tool") answers[name] = { type: "noul", noul: 0.99 };
        else if (name === "needs_checkpoint") answers[name] = { type: "noul", noul: 0.01 };
        else if (name.startsWith("tool_")) {
          const index = Number(name.slice(5));
          answers[name] = { type: "noul", noul: index === 0 || index === 40 ? 0.9 : 0.01 };
        } else if (name.startsWith("role_")) {
          const index = Number(name.slice(5));
          const selected = index === 0 ? "source" : index === 40 ? "sink" : "irrelevant";
          answers[name] = {
            type: "choice",
            choice: selected,
            confidence: 0.99,
            probabilities: { [selected]: 0.99 },
          };
        } else {
          throw new Error(`Unexpected question: ${name} (${question.type})`);
        }
      }
      inFlight -= 1;
      return new Response(
        JSON.stringify({
          model: "jev-test",
          answers,
          usage: { input_tokens: 100, output_tokens: 4 },
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      );
    },
  });
  const tools = Array.from({ length: 41 }, (_, index) => ({
    call: `server.tool_${index}`,
    description: `Tool ${index}`,
  }));
  const lifecycle = {
    async request(name: string, args: Record<string, unknown>) {
      if (name === "search") {
        const cursor = Number(args.cursor ?? 0);
        const results = tools.slice(cursor, cursor + 20);
        const next = cursor + results.length;
        return { results, next_cursor: next < tools.length ? next : null };
      }
      if (name === "inspect") {
        expect(args.calls).toEqual(["server.tool_0", "server.tool_40"]);
        return {
          prelude: "type JsonValue = object",
          results: [{ stub: "async def tool_0(): ..." }, { stub: "async def tool_40(): ..." }],
        };
      }
      throw new Error(`Unexpected sidecar request: ${name}`);
    },
  };

  const result = await new JevRouter(lifecycle, client).route("Fetch data and publish it");

  expect(requestCount).toBe(2);
  expect(maxInFlight).toBe(2);
  expect(result.selected.map((tool) => tool.call)).toEqual(["server.tool_0", "server.tool_40"]);
  expect(result.workflowShape).toBe("pipeline");
});
