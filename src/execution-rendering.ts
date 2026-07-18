import { highlightCode, keyHint, type Theme } from "@earendil-works/pi-coding-agent";
import { Text } from "@earendil-works/pi-tui";
import type { CodeMcpOutputDetails } from "./output.js";

export interface ExecutionRenderDetails extends CodeMcpOutputDetails {
  ok: boolean;
  failureStage?: string;
  callsMade: number;
  chainCalls: number;
  timings?: Record<string, unknown>;
  preview: string[];
}

interface RenderResult {
  content: readonly unknown[];
  details?: unknown;
}

interface ExecutionRendererOptions {
  partialText?: string;
  expandDescription?: string;
}

export function renderExecutionResult(
  result: RenderResult,
  state: { expanded: boolean; isPartial: boolean },
  theme: Theme,
  options: ExecutionRendererOptions = {},
): Text {
  if (state.isPartial) {
    return new Text(
      `\n${theme.fg("warning", options.partialText ?? "Preflight check, then execution...")}`,
      0,
      0,
    );
  }
  const details = result.details as ExecutionRenderDetails | undefined;
  if (state.expanded) return renderExpandedResult(result.content, details, theme);
  const calls = details?.callsMade ?? 0;
  const chainCalls = details?.chainCalls ?? 0;
  const outputTokens = details?.outputTokens ?? 0;
  let text = details?.ok
    ? `\n${theme.fg("success", `✓ Output · ${formatExecutionCalls(calls, chainCalls)} · ${formatTokenEstimate(outputTokens)}`)}`
    : `\n${renderCompactFailure(details?.failureStage, calls, chainCalls, theme)}`;
  for (const line of details?.preview ?? []) {
    text += `\n${theme.fg("dim", `  ${line}`)}`;
  }
  if (details?.truncated) text += `\n${theme.fg("warning", "  output truncated")}`;
  text += `\n${theme.fg(
    "muted",
    keyHint("app.tools.expand", options.expandDescription ?? "code and full output"),
  )}`;
  return new Text(text, 0, 0);
}

export function previewExecutionValue(value: unknown): string[] {
  if (isRecord(value)) {
    return Object.entries(value)
      .slice(0, 3)
      .map(([key, entry]) => `${key}: ${summarizeValue(entry)}`);
  }
  if (Array.isArray(value)) {
    return value.slice(0, 3).map((entry, index) => `[${index}]: ${summarizeValue(entry)}`);
  }
  if (value === null || value === undefined) return [];
  return [summarizeValue(value)];
}

export function getTextContent(content: readonly unknown[]): string {
  return content
    .flatMap((item) => {
      if (!isRecord(item) || item.type !== "text" || typeof item.text !== "string") return [];
      return [item.text];
    })
    .join("\n");
}

function renderExpandedResult(
  content: readonly unknown[],
  details: ExecutionRenderDetails | undefined,
  theme: Theme,
): Text {
  const raw = getTextContent(content);
  const parsed = parseJsonValue(raw);
  const response = isRecord(parsed) ? parsed : undefined;
  const calls = details?.callsMade ?? 0;
  const chainCalls = details?.chainCalls ?? 0;
  if (details?.ok) {
    const output = parsed === undefined ? raw : formatJson(parsed);
    const highlighted = highlightCode(output, "json").join("\n");
    return new Text(
      `\n${theme.fg("success", theme.bold(`Output · ${formatExecutionCalls(calls, chainCalls)}`))}\n${highlighted}`,
      0,
      0,
    );
  }

  const stage = details?.failureStage ?? "runtime";
  const error =
    response && typeof response.error === "string" ? response.error : getTextContent(content);
  const heading = failureHeading(stage, calls, chainCalls, theme);
  const coloredError =
    stage === "preflight" ? theme.fg("warning", error) : theme.fg("error", error);
  return new Text(`\n${heading}\n${coloredError}`, 0, 0);
}

function renderCompactFailure(
  stage: string | undefined,
  calls: number,
  chainCalls: number,
  theme: Theme,
): string {
  const summary = formatExecutionCalls(calls, chainCalls);
  if (stage === "preflight") {
    return theme.fg("warning", `✗ Preflight · code not run · ${summary}`);
  }
  if (stage === "timeout") {
    return theme.fg("error", `✗ Timeout · stopped after ${summary}`);
  }
  if (stage === "cancelled") {
    return theme.fg("warning", `✗ Cancelled · stopped after ${summary}`);
  }
  if (stage === "result") {
    return theme.fg("warning", `✗ Result too large · ${summary} completed`);
  }
  return theme.fg("error", `✗ Runtime · failed after ${summary}`);
}

function failureHeading(stage: string, calls: number, chainCalls: number, theme: Theme): string {
  const summary = formatExecutionCalls(calls, chainCalls);
  if (stage === "preflight") {
    return `${theme.fg("warning", theme.bold("Preflight failed"))}\n${theme.fg("muted", "Code was not executed; no upstream side effects")}`;
  }
  if (stage === "timeout") {
    return `${theme.fg("error", theme.bold("Execution timed out"))}\n${theme.fg("muted", `Stopped after ${summary}`)}`;
  }
  if (stage === "cancelled") {
    return `${theme.fg("warning", theme.bold("Execution cancelled"))}\n${theme.fg("muted", `Stopped after ${summary}`)}`;
  }
  if (stage === "result") {
    return `${theme.fg("warning", theme.bold("Result too large"))}\n${theme.fg("muted", `Call graph completed (${summary}); return a smaller value`)}`;
  }
  return `${theme.fg("error", theme.bold("Runtime failed"))}\n${theme.fg("muted", `Failure occurred after ${summary}`)}`;
}

function formatExecutionCalls(mcpCalls: number, chainCalls: number): string {
  const mcp = `${mcpCalls} MCP ${mcpCalls === 1 ? "call" : "calls"}`;
  return chainCalls > 0
    ? `${mcp} · ${chainCalls} chain ${chainCalls === 1 ? "call" : "calls"}`
    : mcp;
}

function formatTokenEstimate(tokens: number): string {
  return `~${tokens.toLocaleString("en-US")} tokens`;
}

function formatJson(value: unknown): string {
  return JSON.stringify(value, null, 2) ?? String(value);
}

function parseJsonValue(value: string): unknown {
  try {
    return JSON.parse(value);
  } catch {
    return undefined;
  }
}

function summarizeValue(value: unknown): string {
  if (Array.isArray(value)) return `[${value.length} items]`;
  if (isRecord(value)) {
    const keys = Object.keys(value);
    return `{${keys.slice(0, 4).join(", ")}${keys.length > 4 ? ", …" : ""}}`;
  }
  if (typeof value === "string") return truncate(value.replace(/\s+/g, " "), 100);
  return String(value);
}

function truncate(value: string, maxLength: number): string {
  return value.length <= maxLength ? value : `${value.slice(0, maxLength - 1)}…`;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
