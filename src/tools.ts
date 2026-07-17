import {
  type ExtensionAPI,
  formatSize,
  highlightCode,
  keyHint,
  type Theme,
} from "@earendil-works/pi-coding-agent";
import { Text } from "@earendil-works/pi-tui";
import { Type } from "typebox";
import type { CodeMcpLifecycle } from "./lifecycle.js";
import { type CodeMcpOutputDetails, formatCodeMcpOutput } from "./output.js";

interface SearchRenderDetails extends CodeMcpOutputDetails {
  matchCount: number;
  totalToolCount: number;
  serverCount: number;
  preview: string[];
}

interface SchemaRenderDetails extends CodeMcpOutputDetails {
  toolCount: number;
  preview: string[];
}

interface ExecuteRenderDetails extends CodeMcpOutputDetails {
  ok: boolean;
  failureStage?: string;
  callsMade: number;
  resultKind: string;
  preview: string[];
}

const SearchParameters = Type.Object({
  query: Type.String({
    minLength: 1,
    description: "Words describing the upstream capability or tool to find",
  }),
  limit: Type.Optional(
    Type.Integer({
      minimum: 1,
      maximum: 20,
      description: "Maximum compact matches to return (default 5)",
    }),
  ),
  server: Type.Optional(
    Type.String({
      minLength: 1,
      description: "Configured upstream server to search",
    }),
  ),
});

const SchemaParameters = Type.Object({
  tools: Type.Array(Type.String(), {
    minItems: 1,
    maxItems: 20,
    uniqueItems: true,
    description: "Exact names returned by codemcp_search",
  }),
});

const ExecuteParameters = Type.Object({
  code: Type.String({
    minLength: 1,
    description:
      "Sandboxed Python body. Call typed SDK methods such as await linear.list_issues(arguments) and return a compact final value.",
  }),
});

export function registerCodeMcpTools(pi: ExtensionAPI, lifecycle: CodeMcpLifecycle): void {
  pi.registerTool({
    name: "codemcp_search",
    label: "MCP Search",
    description: "Search configured upstream MCP tools by capability.",
    promptSnippet: "Search configured upstream MCP tools by capability",
    promptGuidelines: [
      "Use codemcp_search before codemcp_get_schema when the exact upstream MCP tool names are unknown.",
    ],
    parameters: SearchParameters,
    async execute(_toolCallId, params, signal, onUpdate) {
      onUpdate?.({
        content: [{ type: "text", text: "Searching MCP tools..." }],
        details: undefined,
      });
      const result = await lifecycle.request(
        "search",
        {
          query: params.query,
          limit: params.limit ?? 5,
          ...(params.server === undefined ? {} : { server: params.server }),
        },
        signal,
      );
      const output = formatCodeMcpOutput(result);
      const results = Array.isArray(result.results) ? result.results : [];
      const preview = results
        .slice(0, 3)
        .map((item) => (isRecord(item) ? String(item.call ?? item.name ?? "unknown") : "unknown"));
      const servers = Array.isArray(result.servers)
        ? result.servers.flatMap((item) => {
            if (!isRecord(item)) return [];
            return [`${String(item.name)} ${String(item.tool_count ?? 0)}`];
          })
        : [];
      return {
        content: [{ type: "text", text: output.text }],
        details: {
          ...output.details,
          matchCount: results.length,
          totalToolCount: Number(result.total_tool_count ?? 0),
          serverCount: servers.length,
          preview,
        },
      };
    },
    renderCall(args, theme) {
      return new Text(
        `${theme.fg("toolTitle", theme.bold("MCP Search "))}${theme.fg("accent", `"${args.query}"`)}`,
        0,
        0,
      );
    },
    renderResult(result, { expanded, isPartial }, theme) {
      if (isPartial) return new Text(theme.fg("warning", "Searching catalog..."), 0, 0);
      if (expanded) return renderExpandedJson(result.content);
      const details = result.details as SearchRenderDetails | undefined;
      let text = theme.fg(
        "success",
        `${details?.matchCount ?? 0} matches · ${details?.totalToolCount ?? 0} tools · ${details?.serverCount ?? 0} servers`,
      );
      for (const name of details?.preview ?? []) {
        text += `\n${theme.fg("dim", `  ${name}`)}`;
      }
      text += `\n${theme.fg("muted", keyHint("app.tools.expand", "full results"))}`;
      return new Text(text, 0, 0);
    },
  });

  pi.registerTool({
    name: "codemcp_get_schema",
    label: "MCP Schema",
    description: "Return compact typed Python SDK signatures for selected MCP tools.",
    promptSnippet: "Inspect compact typed signatures for selected MCP tools",
    promptGuidelines: [
      "Use codemcp_get_schema for the exact tools selected by codemcp_search before writing a codemcp_execute chain.",
    ],
    parameters: SchemaParameters,
    async execute(_toolCallId, params, signal, onUpdate) {
      onUpdate?.({
        content: [{ type: "text", text: "Loading typed MCP schemas..." }],
        details: undefined,
      });
      const result = await lifecycle.request("get_schema", { tools: params.tools }, signal);
      const output = formatCodeMcpOutput(result);
      const tools = Array.isArray(result.tools) ? result.tools : [];
      return {
        content: [{ type: "text", text: output.text }],
        details: {
          ...output.details,
          toolCount: tools.length,
          preview: params.tools.slice(0, 3),
        },
      };
    },
    renderCall(args, theme) {
      const names = args.tools.slice(0, 2).join(", ");
      const rest = args.tools.length > 2 ? ` +${args.tools.length - 2}` : "";
      return new Text(
        `${theme.fg("toolTitle", theme.bold("MCP Schema "))}${theme.fg("accent", names)}${theme.fg("muted", rest)}`,
        0,
        0,
      );
    },
    renderResult(result, { expanded, isPartial }, theme) {
      if (isPartial) return new Text(theme.fg("warning", "Loading schemas..."), 0, 0);
      if (expanded) return renderExpandedJson(result.content);
      const details = result.details as SchemaRenderDetails | undefined;
      let text = theme.fg("success", `${details?.toolCount ?? 0} typed schemas`);
      for (const name of details?.preview ?? []) {
        text += `\n${theme.fg("dim", `  ${name}`)}`;
      }
      text += `\n${theme.fg("muted", keyHint("app.tools.expand", "full schemas"))}`;
      return new Text(text, 0, 0);
    },
  });

  pi.registerTool({
    name: "codemcp_execute",
    label: "MCP Execute",
    description:
      "Type-check and execute one sandboxed Python MCP chain. Supports sequential and dependent calls, loops, conditions, cross-server calls, and all upstream tools. The code has no host filesystem, environment, network, or subprocess access. Return a compact final value smaller than 16 KiB; oversized values fail with a shape summary.",
    promptSnippet: "Run a typed, sandboxed multi-call chain across configured MCP servers",
    promptGuidelines: [
      "Use codemcp_execute if you know tool schemas; call the returned server.method facade and use top-level return for the compact final value.",
      "It is always better to execute multiple MCP calls in one codemcp_execute call rather than multiple single-call invocations.",
      "You can chain multiple MCP results, call in parallel or sequentially, you have full control over the execution flow as long as it is efficient",
    ],
    parameters: ExecuteParameters,
    async execute(_toolCallId, params, signal, onUpdate) {
      onUpdate?.({
        content: [{ type: "text", text: "Type-checking MCP chain..." }],
        details: undefined,
      });
      const result = await lifecycle.request("execute", { code: params.code }, signal);
      const output = formatCodeMcpOutput(result);
      const ok = result.ok === true;
      return {
        content: [{ type: "text", text: output.text }],
        details: {
          ...output.details,
          ok,
          failureStage: typeof result.failure_stage === "string" ? result.failure_stage : undefined,
          callsMade: Number(result.calls_made ?? 0),
          resultKind: describeKind(result.result),
          preview: previewValue(ok ? result.result : result.error),
        },
      };
    },
    renderCall(args, theme, context) {
      const code = args.code.trim();
      const lineCount = code ? code.split("\n").length : 0;
      const title = theme.fg("toolTitle", theme.bold("MCP Execute"));
      const codeLabel = theme.fg(
        "accent",
        theme.bold(`Agent code · ${lineCount} ${lineCount === 1 ? "line" : "lines"}`),
      );
      if (context.expanded && code) {
        return new Text(
          `${title}\n${codeLabel}\n${highlightCode(code, "python").join("\n")}`,
          0,
          0,
        );
      }
      const firstLine =
        code
          .split("\n")
          .find((line) => line.trim())
          ?.trim() ?? "";
      return new Text(
        `${title} ${theme.fg("muted", "·")} ${codeLabel}${firstLine ? `\n${theme.fg("dim", `  ${truncate(firstLine, 100)}`)}` : ""}`,
        0,
        0,
      );
    },
    renderResult(result, { expanded, isPartial }, theme) {
      if (isPartial) {
        return new Text(`\n${theme.fg("warning", "Preflight check, then execution...")}`, 0, 0);
      }
      const details = result.details as ExecuteRenderDetails | undefined;
      if (expanded) return renderExpandedExecuteResult(result.content, details, theme);
      const calls = details?.callsMade ?? 0;
      const size = formatSize(details?.totalBytes ?? 0);
      let text = details?.ok
        ? `\n${theme.fg("success", `✓ Output · ${formatMcpCalls(calls)} · ${details.resultKind} · ${size}`)}`
        : `\n${renderCompactFailure(details?.failureStage, calls, theme)}`;
      for (const line of details?.preview ?? []) {
        text += `\n${theme.fg("dim", `  ${line}`)}`;
      }
      if (details?.truncated) text += `\n${theme.fg("warning", "  output truncated")}`;
      text += `\n${theme.fg("muted", keyHint("app.tools.expand", "code and full output"))}`;
      return new Text(text, 0, 0);
    },
  });
}

function renderExpandedJson(content: readonly unknown[]): Text {
  return new Text(highlightCode(getTextContent(content), "json").join("\n"), 0, 0);
}

function renderExpandedExecuteResult(
  content: readonly unknown[],
  details: ExecuteRenderDetails | undefined,
  theme: Theme,
): Text {
  const response = parseJsonObject(getTextContent(content));
  const calls = details?.callsMade ?? 0;
  if (details?.ok) {
    const output = response ? formatJson(response.result) : getTextContent(content);
    const highlighted = highlightCode(output, "json").join("\n");
    return new Text(
      `\n${theme.fg("success", theme.bold(`Output · ${formatMcpCalls(calls)}`))}\n${highlighted}`,
      0,
      0,
    );
  }

  const stage = details?.failureStage ?? "runtime";
  const error =
    response && typeof response.error === "string" ? response.error : getTextContent(content);
  const heading = failureHeading(stage, calls, theme);
  const coloredError =
    stage === "preflight" ? theme.fg("warning", error) : theme.fg("error", error);
  return new Text(`\n${heading}\n${coloredError}`, 0, 0);
}

function renderCompactFailure(stage: string | undefined, calls: number, theme: Theme): string {
  if (stage === "preflight") {
    return theme.fg("warning", `✗ Preflight · code not run · ${formatMcpCalls(calls)}`);
  }
  if (stage === "timeout") {
    return theme.fg("error", `✗ Timeout · stopped after ${formatMcpCalls(calls)}`);
  }
  if (stage === "cancelled") {
    return theme.fg("warning", `✗ Cancelled · stopped after ${formatMcpCalls(calls)}`);
  }
  if (stage === "result") {
    return theme.fg("warning", `✗ Result too large · ${formatMcpCalls(calls)} completed`);
  }
  return theme.fg("error", `✗ Runtime · failed after ${formatMcpCalls(calls)}`);
}

function failureHeading(stage: string, calls: number, theme: Theme): string {
  if (stage === "preflight") {
    return `${theme.fg("warning", theme.bold("Preflight failed"))}\n${theme.fg("muted", "Code was not executed; no upstream side effects")}`;
  }
  if (stage === "timeout") {
    return `${theme.fg("error", theme.bold("Execution timed out"))}\n${theme.fg("muted", `Stopped after ${formatMcpCalls(calls)}`)}`;
  }
  if (stage === "cancelled") {
    return `${theme.fg("warning", theme.bold("Execution cancelled"))}\n${theme.fg("muted", `Stopped after ${formatMcpCalls(calls)}`)}`;
  }
  if (stage === "result") {
    return `${theme.fg("warning", theme.bold("Result too large"))}\n${theme.fg("muted", `Upstream calls completed (${formatMcpCalls(calls)}); return a smaller value`)}`;
  }
  return `${theme.fg("error", theme.bold("Runtime failed"))}\n${theme.fg("muted", `Failure occurred after ${formatMcpCalls(calls)}`)}`;
}

function formatMcpCalls(calls: number): string {
  return `${calls} MCP ${calls === 1 ? "call" : "calls"}`;
}

function formatJson(value: unknown): string {
  return JSON.stringify(value, null, 2) ?? String(value);
}

function parseJsonObject(value: string): Record<string, unknown> | undefined {
  try {
    const parsed: unknown = JSON.parse(value);
    return isRecord(parsed) ? parsed : undefined;
  } catch {
    return undefined;
  }
}

function getTextContent(content: readonly unknown[]): string {
  return content
    .flatMap((item) => {
      if (!isRecord(item) || item.type !== "text" || typeof item.text !== "string") return [];
      return [item.text];
    })
    .join("\n");
}

function previewValue(value: unknown): string[] {
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

function summarizeValue(value: unknown): string {
  if (Array.isArray(value)) return `[${value.length} items]`;
  if (isRecord(value)) {
    const keys = Object.keys(value);
    return `{${keys.slice(0, 4).join(", ")}${keys.length > 4 ? ", …" : ""}}`;
  }
  if (typeof value === "string") return truncate(value.replace(/\s+/g, " "), 100);
  return String(value);
}

function describeKind(value: unknown): string {
  if (Array.isArray(value)) return `array[${value.length}]`;
  if (value === null) return "null";
  return typeof value === "object" ? "object" : typeof value;
}

function truncate(value: string, maxLength: number): string {
  return value.length <= maxLength ? value : `${value.slice(0, maxLength - 1)}…`;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
