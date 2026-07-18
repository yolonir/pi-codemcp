import { type ExtensionAPI, highlightCode, keyHint } from "@earendil-works/pi-coding-agent";
import { Text } from "@earendil-works/pi-tui";
import { Type } from "typebox";
import { type ChainScope, nativeChainToolName, type SavedChainManager } from "./chains.js";
import {
  getTextContent,
  previewExecutionValue,
  renderExecutionResult,
} from "./execution-rendering.js";
import type { CodeMcpLifecycle } from "./lifecycle.js";
import { type CodeMcpOutputDetails, formatCodeMcpOutput } from "./output.js";

interface SearchRenderDetails extends CodeMcpOutputDetails {
  matchCount: number;
  totalToolCount: number;
  serverCount: number;
  hasMore: boolean;
  nextCursor?: number;
  detail: string;
  preview: string[];
}

const SearchParameters = Type.Object({
  query: Type.Optional(
    Type.String({
      minLength: 1,
      description: "Capability words for search mode; omit for inventory mode",
    }),
  ),
  mode: Type.Optional(
    Type.String({
      enum: ["search", "inventory"],
      description: "Rank by capability (default) or page through compact inventory",
    }),
  ),
  detail: Type.Optional(
    Type.String({
      enum: ["names", "signatures", "full"],
      description: "Disclosure level (default signatures); use inspect for selected full stubs",
    }),
  ),
  limit: Type.Optional(
    Type.Integer({
      minimum: 1,
      maximum: 20,
      description: "Maximum matches per page (default 5)",
    }),
  ),
  cursor: Type.Optional(
    Type.Integer({
      minimum: 0,
      description: "Pagination cursor returned by a previous search",
    }),
  ),
  server: Type.Optional(
    Type.String({
      minLength: 1,
      description: "Exact configured server name, or chains for saved chains",
    }),
  ),
});

const InspectParameters = Type.Object({
  calls: Type.Array(Type.String({ minLength: 1 }), {
    minItems: 1,
    maxItems: 20,
    description: "Exact call identifiers returned by search, such as grafana.query_prometheus",
  }),
});

const SaveChainParameters = Type.Object({
  scope: Type.Optional(
    Type.String({
      enum: ["project", "global"],
      description: "Storage scope for the chain (default project)",
    }),
  ),
  name: Type.String({
    minLength: 1,
    maxLength: 64,
    pattern: "^[a-z][a-z0-9_]{0,63}$",
    description: "Stable lowercase SDK method name used as chains.<name>",
  }),
  description: Type.String({
    minLength: 1,
    maxLength: 1000,
    description: "Purpose and appropriate use of the reusable native tool",
  }),
  code: Type.String({
    minLength: 1,
    description:
      "Sandboxed Python body. Read typed arguments from input, call MCP or chains SDK methods, and return a value matching outputSchema.",
  }),
  inputSchema: Type.Record(Type.String(), Type.Unknown(), {
    description: "JSON Schema for the native tool arguments; the root must be an object",
  }),
  outputSchema: Type.Record(Type.String(), Type.Unknown(), {
    description: "Required JSON Schema for the chain return value",
  }),
});

const ExecuteParameters = Type.Object({
  code: Type.String({
    minLength: 1,
    description:
      "Sandboxed Python body. Call typed SDK methods such as await linear.list_issues(arguments) and return a compact final value.",
  }),
});

export function registerCodeMcpTools(
  pi: ExtensionAPI,
  lifecycle: CodeMcpLifecycle,
  chains: SavedChainManager,
): void {
  pi.registerTool({
    name: "codemcp_search",
    label: "MCP Search",
    description:
      "Search configured upstream MCP tools and saved chains, returning their typed SDK stubs.",
    promptSnippet: "Search MCP capabilities and reusable chains, then inspect typed SDK stubs",
    promptGuidelines: [
      "Use codemcp_search before codemcp_execute; every upstream or saved-chain match includes the complete typed SDK stub needed to write the execution.",
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
          ...(params.query === undefined ? {} : { query: params.query }),
          mode: params.mode ?? "search",
          detail: params.detail ?? "signatures",
          limit: params.limit ?? 5,
          cursor: params.cursor ?? 0,
          ...(params.server === undefined ? {} : { server: params.server }),
        },
        signal,
      );
      const output = formatCodeMcpOutput(result, outputLimits(lifecycle));
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
          hasMore: result.has_more === true,
          nextCursor: typeof result.next_cursor === "number" ? result.next_cursor : undefined,
          detail: typeof result.detail === "string" ? result.detail : "signatures",
          preview,
        },
      };
    },
    renderCall(args, theme) {
      const subject = args.mode === "inventory" ? "inventory" : `"${args.query ?? ""}"`;
      return new Text(
        `${theme.fg("toolTitle", theme.bold("MCP Search "))}${theme.fg("accent", subject)}`,
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
        `${details?.matchCount ?? 0} matches · ${details?.detail ?? "signatures"} · ${details?.totalToolCount ?? 0} tools · ${details?.serverCount ?? 0} servers`,
      );
      for (const name of details?.preview ?? []) {
        text += `\n${theme.fg("dim", `  ${name}`)}`;
      }
      if (details?.hasMore) {
        text += `\n${theme.fg("muted", `  more at cursor ${details.nextCursor ?? "?"}`)}`;
      }
      text += `\n${theme.fg("muted", keyHint("app.tools.expand", "full results"))}`;
      return new Text(text, 0, 0);
    },
  });

  pi.registerTool({
    name: "codemcp_inspect",
    label: "MCP Inspect",
    description:
      "Return the exact typed SDK stubs for selected call identifiers from codemcp_search. The response deduplicates the shared JsonValue/type prelude.",
    promptSnippet: "Inspect exact typed SDK stubs for selected MCP calls",
    promptGuidelines: [
      "Use codemcp_inspect after compact search when exact argument or result types are needed.",
    ],
    parameters: InspectParameters,
    async execute(_toolCallId, params, signal, onUpdate) {
      onUpdate?.({
        content: [{ type: "text", text: "Inspecting MCP tool contracts..." }],
        details: undefined,
      });
      const result = await lifecycle.request("inspect", { calls: params.calls }, signal);
      const output = formatCodeMcpOutput(result, outputLimits(lifecycle));
      const results = Array.isArray(result.results) ? result.results : [];
      return {
        content: [{ type: "text", text: output.text }],
        details: {
          ...output.details,
          matchCount: results.length,
          preview: results
            .slice(0, 3)
            .flatMap((item) =>
              isRecord(item) && typeof item.call === "string" ? [item.call] : [],
            ),
        },
      };
    },
    renderCall(args, theme) {
      return new Text(
        `${theme.fg("toolTitle", theme.bold("MCP Inspect "))}${theme.fg("accent", `${args.calls.length} calls`)}`,
        0,
        0,
      );
    },
    renderResult(result, { expanded, isPartial }, theme) {
      if (isPartial) return new Text(theme.fg("warning", "Loading exact contracts..."), 0, 0);
      if (expanded) return renderExpandedJson(result.content);
      const details = result.details as
        | (CodeMcpOutputDetails & { matchCount: number; preview: string[] })
        | undefined;
      let text = theme.fg("success", `${details?.matchCount ?? 0} exact contracts`);
      for (const call of details?.preview ?? []) text += `\n${theme.fg("dim", `  ${call}`)}`;
      text += `\n${theme.fg("muted", keyHint("app.tools.expand", "full stubs"))}`;
      return new Text(text, 0, 0);
    },
  });

  pi.registerTool({
    name: "codemcp_execute",
    label: "MCP Execute",
    description:
      "Type-check and execute one sandboxed Python MCP call graph. Supports sequential and dependent calls, loops, conditions, cross-server calls, enabled upstream tools, and reusable chains.* calls. The code has no host filesystem, environment, network, or subprocess access. Return a compact final value within the configured result limit; oversized values fail with a shape summary.",
    promptSnippet: "Run a typed, sandboxed multi-call chain across configured MCP servers",
    promptGuidelines: [
      "Use codemcp_execute if you know tool schemas; call the returned server.method facade and use top-level return for the compact final value.",
      "It is always better to execute multiple MCP calls in one codemcp_execute call rather than multiple single-call invocations.",
      "You can compose upstream SDK calls and saved chains.* calls, running independent work with asyncio.gather or dependent work sequentially.",
    ],
    parameters: ExecuteParameters,
    async execute(_toolCallId, params, signal, onUpdate) {
      onUpdate?.({
        content: [{ type: "text", text: "Type-checking MCP chain..." }],
        details: undefined,
      });
      const result = await lifecycle.request("execute", { code: params.code }, signal);
      const output = formatCodeMcpOutput(result, outputLimits(lifecycle));
      const ok = result.ok === true;
      return {
        content: [{ type: "text", text: output.text }],
        details: {
          ...output.details,
          ok,
          failureStage: typeof result.failure_stage === "string" ? result.failure_stage : undefined,
          callsMade: Number(result.calls_made ?? 0),
          chainCalls: Number(result.chain_calls ?? 0),
          preview: previewExecutionValue(ok ? result.result : result.error),
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
    renderResult(result, state, theme) {
      return renderExecutionResult(result, state, theme);
    },
  });

  pi.registerTool({
    name: "codemcp_save_chain",
    label: "Save MCP Chain",
    description:
      "Validate and persist a reusable typed MCP chain in project scope by default or global scope when explicitly requested. Project chains override same-named global chains. The effective chain is immediately registered as a native mcp_chain_<name> tool and as chains.<name> inside CodeMCP. Requires explicit input and output JSON Schemas. Saving the same scoped name updates and re-enables it.",
    promptSnippet: "Save a repeated MCP execution as a typed reusable native tool",
    promptGuidelines: [
      "If a user repeatedly performs the same MCP workflow, you may offer to save it with codemcp_save_chain, but do not persist it until the user explicitly asks or accepts.",
      "Use codemcp_save_chain only after the user explicitly asks to save a chain or accepts your suggestion to do so.",
      "When using codemcp_save_chain, parameterize repeated values through the typed input object and provide exact inputSchema and outputSchema contracts.",
      "Save chains in project scope unless the user explicitly asks to make one available globally across projects.",
    ],
    parameters: SaveChainParameters,
    async execute(_toolCallId, params, signal, onUpdate) {
      onUpdate?.({
        content: [{ type: "text", text: `Validating saved chain ${params.name}...` }],
        details: undefined,
      });
      const scope = requireChainScope(params.scope ?? "project");
      const view = await chains.save(
        {
          scope,
          name: params.name,
          description: params.description,
          code: params.code,
          inputSchema: params.inputSchema,
          outputSchema: params.outputSchema,
        },
        signal,
      );
      const result = {
        saved: true,
        scope: view.scope,
        name: view.chain.name,
        native_tool: nativeChainToolName(view.chain.name),
        call: `chains.${view.chain.name}`,
        status: view.status,
        dependencies: view.chain.dependencies.map((dependency) => dependency.call),
      };
      const output = formatCodeMcpOutput(result, outputLimits(lifecycle));
      return {
        content: [{ type: "text", text: output.text }],
        details: {
          ...output.details,
          name: view.chain.name,
          nativeTool: nativeChainToolName(view.chain.name),
          dependencyCount: view.chain.dependencies.length,
        },
      };
    },
    renderCall(args, theme) {
      return new Text(
        `${theme.fg("toolTitle", theme.bold("Save MCP Chain "))}${theme.fg("accent", args.name)} ${theme.fg("muted", `· ${args.scope ?? "project"}`)}`,
        0,
        0,
      );
    },
    renderResult(result, { expanded, isPartial }, theme, context) {
      if (isPartial) return new Text(theme.fg("warning", "Validating chain contract..."), 0, 0);
      const details = result.details as
        | (CodeMcpOutputDetails & {
            name: string;
            nativeTool: string;
            dependencyCount: number;
          })
        | undefined;
      if (context.isError || !details) {
        const message = getTextContent(result.content).trim();
        if (expanded) {
          return new Text(theme.fg("error", message || "Saved chain validation failed"), 0, 0);
        }
        const preview = message.split("\n", 1)[0];
        return new Text(
          theme.fg("error", `✗ Save failed${preview ? ` · ${truncate(preview, 120)}` : ""}`),
          0,
          0,
        );
      }
      if (expanded) return renderExpandedJson(result.content);
      return new Text(
        theme.fg(
          "success",
          `✓ ${details.name} · ${details.nativeTool} · ${details.dependencyCount} dependencies`,
        ),
        0,
        0,
      );
    },
  });
}

function renderExpandedJson(content: readonly unknown[]): Text {
  return new Text(highlightCode(getTextContent(content), "json").join("\n"), 0, 0);
}

function outputLimits(lifecycle: CodeMcpLifecycle): { maxBytes: number; maxLines: number } {
  const settings = lifecycle.loadSettings();
  return {
    maxBytes: settings.outputLimitKiB * 1024,
    maxLines: settings.outputLineLimit,
  };
}

function truncate(value: string, maxLength: number): string {
  return value.length <= maxLength ? value : `${value.slice(0, maxLength - 1)}…`;
}

function requireChainScope(value: string): ChainScope {
  if (value !== "project" && value !== "global") {
    throw new TypeError("Saved chain scope must be project or global");
  }
  return value;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
