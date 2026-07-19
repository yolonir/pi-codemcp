import { describe, expect, test } from "bun:test";
import { type ExtensionAPI, initTheme, type Theme } from "@earendil-works/pi-coding-agent";
import type { Component } from "@earendil-works/pi-tui";
import { createCodeMcpExtension } from "../../extensions/index.js";

interface CapturedTool {
  name: string;
  renderCall?: (
    args: Record<string, unknown>,
    theme: Theme,
    context: { expanded: boolean },
  ) => Component;
  renderResult?: (
    result: { content: Array<{ type: "text"; text: string }>; details?: unknown },
    options: { expanded: boolean; isPartial: boolean },
    theme: Theme,
    context?: { isError: boolean },
  ) => Component;
}

initTheme(undefined, false);

const plainTheme = {
  fg: (_color: string, text: string) => text,
  bold: (text: string) => text,
} as unknown as Theme;

function captureTool(name: string): CapturedTool {
  const tools: CapturedTool[] = [];
  const fakePi = {
    registerTool(tool: CapturedTool) {
      tools.push(tool);
    },
    registerCommand() {},
    on() {},
  } as unknown as ExtensionAPI;
  createCodeMcpExtension()(fakePi);
  const captured = tools.find((tool) => tool.name === name);
  if (!captured?.renderCall || !captured.renderResult) {
    throw new Error(`${name} renderers were not registered`);
  }
  return captured;
}

function captureExecuteTool(): CapturedTool {
  return captureTool("codemcp_execute");
}

function render(component: Component): string {
  return component.render(160).join("\n");
}

describe("codemcp_execute rendering", () => {
  test("separates expanded agent code from successful output", () => {
    const tool = captureExecuteTool();
    const call = render(
      tool.renderCall?.(
        { code: 'value = await alpha.get_number({"seed": 1})\nreturn value' },
        plainTheme,
        { expanded: true },
      ) as Component,
    );
    const result = render(
      tool.renderResult?.(
        {
          content: [
            {
              type: "text",
              text: JSON.stringify({ value: 2 }),
            },
          ],
          details: {
            ok: true,
            callsMade: 1,
            resultKind: "object",
            totalBytes: 48,
            preview: [],
          },
        },
        { expanded: true, isPartial: false },
        plainTheme,
      ) as Component,
    );

    expect(call).toContain("Agent code · 2 lines");
    expect(result).toContain("Output · 1 MCP call");
    expect(result).toContain("value");
    expect(result).not.toContain("stage");
    expect(result).not.toContain("calls_made");

    const compact = render(
      tool.renderResult?.(
        {
          content: [{ type: "text", text: "result" }],
          details: {
            ok: true,
            callsMade: 1,
            outputTokens: 875,
            preview: [],
          },
        },
        { expanded: false, isPartial: false },
        plainTheme,
      ) as Component,
    );
    expect(compact).toContain("Output · 1 MCP call · ~875 tokens");
    expect(compact).not.toContain("object");
    expect(compact).not.toContain("KB");
  });

  test("formats preflight and runtime failures differently", () => {
    const tool = captureExecuteTool();
    const preflight = render(
      tool.renderResult?.(
        {
          content: [
            {
              type: "text",
              text: JSON.stringify({
                failure_stage: "preflight",
                error: "missing required argument: id",
                calls_made: 0,
              }),
            },
          ],
          details: {
            ok: false,
            failureStage: "preflight",
            callsMade: 0,
            preview: [],
          },
        },
        { expanded: true, isPartial: false },
        plainTheme,
      ) as Component,
    );
    const runtime = render(
      tool.renderResult?.(
        {
          content: [
            {
              type: "text",
              text: JSON.stringify({
                failure_stage: "runtime",
                error: "upstream rejected the request",
                calls_made: 2,
              }),
            },
          ],
          details: {
            ok: false,
            failureStage: "runtime",
            callsMade: 2,
            preview: [],
          },
        },
        { expanded: true, isPartial: false },
        plainTheme,
      ) as Component,
    );

    expect(preflight).toContain("Preflight failed");
    expect(preflight).toContain("Code was not executed; no upstream side effects");
    expect(preflight).toContain("missing required argument: id");
    expect(runtime).toContain("Runtime failed");
    expect(runtime).toContain("Failure occurred after 2 MCP calls");
    expect(runtime).toContain("upstream rejected the request");
  });
});

describe("codemcp_manage_chains rendering", () => {
  test("renders manager failures explicitly", () => {
    const tool = captureTool("codemcp_manage_chains");
    const failed = render(
      tool.renderResult?.(
        { content: [{ type: "text", text: "delete requires confirmedByUser=true" }] },
        { expanded: false, isPartial: false },
        plainTheme,
        { isError: true },
      ) as Component,
    );

    expect(failed).toContain("confirmedByUser=true");
    expect(failed).not.toContain("0 chains");
  });
});

describe("codemcp_save_chain rendering", () => {
  test("renders thrown validation failures as errors", () => {
    const tool = captureTool("codemcp_save_chain");
    const failed = render(
      tool.renderResult?.(
        {
          content: [
            {
              type: "text",
              text: "Saved chain failed preflight: return value violates output schema",
            },
          ],
        },
        { expanded: false, isPartial: false },
        plainTheme,
        { isError: true },
      ) as Component,
    );

    expect(failed).toContain("✗ Save failed");
    expect(failed).toContain("violates output schema");
    expect(failed).not.toContain("✓");
    expect(failed).not.toContain("native tool");
  });
});
