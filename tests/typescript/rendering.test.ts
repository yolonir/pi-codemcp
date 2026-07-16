import { describe, expect, test } from "bun:test";
import type { ExtensionAPI, Theme } from "@earendil-works/pi-coding-agent";
import type { Component } from "@earendil-works/pi-tui";
import { createCodeModeExtension } from "../../extensions/index.js";

interface CapturedTool {
  name: string;
  renderCall?: (args: { code: string }, theme: Theme, context: { expanded: boolean }) => Component;
  renderResult?: (
    result: { content: Array<{ type: "text"; text: string }>; details?: unknown },
    options: { expanded: boolean; isPartial: boolean },
    theme: Theme,
  ) => Component;
}

const plainTheme = {
  fg: (_color: string, text: string) => text,
  bold: (text: string) => text,
} as unknown as Theme;

function captureExecuteTool(): CapturedTool {
  const tools: CapturedTool[] = [];
  const fakePi = {
    registerTool(tool: CapturedTool) {
      tools.push(tool);
    },
    registerCommand() {},
    on() {},
  } as unknown as ExtensionAPI;
  createCodeModeExtension()(fakePi);
  const execute = tools.find((tool) => tool.name === "codemode_execute");
  if (!execute?.renderCall || !execute.renderResult) {
    throw new Error("codemode_execute renderers were not registered");
  }
  return execute;
}

function render(component: Component): string {
  return component.render(160).join("\n");
}

describe("codemode_execute rendering", () => {
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
              text: JSON.stringify({ ok: true, result: { value: 2 }, calls_made: 1 }),
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
                ok: false,
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
                ok: false,
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
