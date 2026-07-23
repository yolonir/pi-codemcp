import { type ExtensionAPI, highlightCode, keyHint } from "@earendil-works/pi-coding-agent";
import { Text } from "@earendil-works/pi-tui";
import { Type } from "typebox";
import {
  type ChainScope,
  nativeChainToolName,
  type SavedChainManager,
  type SavedChainView,
} from "./chains.js";
import {
  getTextContent,
  previewExecutionValue,
  renderExecutionResult,
} from "./execution-rendering.js";
import type { CodeMcpLifecycle } from "./lifecycle.js";
import { type CodeMcpOutputDetails, formatCodeMcpOutput } from "./output.js";
import {
  EXECUTE_PROMPT_GUIDELINES,
  INSPECT_PROMPT_GUIDELINES,
  MANAGE_CHAIN_PROMPT_GUIDELINES,
  SAVE_CHAIN_PROMPT_GUIDELINES,
  SEARCH_PROMPT_GUIDELINES,
} from "./prompts.js";

interface SearchRenderDetails extends CodeMcpOutputDetails {
  matchCount: number;
  totalToolCount: number;
  serverCount: number;
  hasMore: boolean;
  nextCursor?: number;
  detail: string;
  preview: string[];
  discoveryFailures: string[];
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

const ManageChainsParameters = Type.Object({
  action: Type.String({
    enum: ["list", "enable", "disable", "revalidate", "delete"],
    description: "Chain management action",
  }),
  name: Type.Optional(
    Type.String({
      minLength: 1,
      maxLength: 64,
      pattern: "^[a-z][a-z0-9_]{0,63}$",
      description: "Saved-chain name; required for mutations",
    }),
  ),
  scope: Type.Optional(
    Type.String({
      enum: ["project", "global"],
      description: "Saved-chain scope; required for mutations",
    }),
  ),
  confirmedByUser: Type.Optional(
    Type.Boolean({
      description: "Must be true for enable, disable, revalidate, or delete",
    }),
  ),
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
      "Search configured MCP capabilities or page through compact inventory. Default signature search includes exact stubs for up to three top matches plus compact alternatives; codemcp_inspect loads other selected stubs. Unscoped searches return available results plus explicit discovery failures; scoped searches remain fail-fast. Returns ranking evidence, pagination, scope, and execution limits; invalid server names fail with suggestions.",
    promptSnippet: "Discover compact MCP capabilities or inventory",
    promptGuidelines: [...SEARCH_PROMPT_GUIDELINES],
    parameters: SearchParameters,
    async execute(toolCallId, params, signal, onUpdate) {
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
          trace_id: toolCallId,
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
      const discoveryFailures = Array.isArray(result.discovery_failures)
        ? result.discovery_failures.flatMap((item) =>
            isRecord(item) && typeof item.server === "string" ? [item.server] : [],
          )
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
          discoveryFailures,
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
      const discoveryFailures = details?.discoveryFailures ?? [];
      const unavailable = discoveryFailures.length;
      let text = theme.fg(
        unavailable > 0 ? "warning" : "success",
        `${details?.matchCount ?? 0} matches · ${details?.detail ?? "signatures"} · ${details?.totalToolCount ?? 0} tools · ${details?.serverCount ?? 0} servers${unavailable > 0 ? ` · ${unavailable} unavailable` : ""}`,
      );
      for (const name of details?.preview ?? []) {
        text += `\n${theme.fg("dim", `  ${name}`)}`;
      }
      for (const server of discoveryFailures) {
        text += `\n${theme.fg("warning", `  unavailable: ${server}`)}`;
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
    promptSnippet: "Load exact typed contracts for selected MCP calls",
    promptGuidelines: [...INSPECT_PROMPT_GUIDELINES],
    parameters: InspectParameters,
    async execute(toolCallId, params, signal, onUpdate) {
      onUpdate?.({
        content: [{ type: "text", text: "Inspecting MCP tool contracts..." }],
        details: undefined,
      });
      const result = await lifecycle.request(
        "inspect",
        { calls: params.calls, trace_id: toolCallId },
        signal,
      );
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
      "Type-check and execute one bounded sandboxed Python MCP call graph. Use it when code can deterministically process intermediate results; preserve a model turn for semantic decisions or approvals. The sandbox has no host filesystem, environment, network, or subprocess access. Oversized results fail with bounded shape, size, and sample diagnostics.",
    promptSnippet: "Run a bounded typed MCP workflow and return a compact result",
    promptGuidelines: [...EXECUTE_PROMPT_GUIDELINES],
    parameters: ExecuteParameters,
    async execute(toolCallId, params, signal, onUpdate) {
      onUpdate?.({
        content: [{ type: "text", text: "Type-checking MCP chain..." }],
        details: undefined,
      });
      const result = await lifecycle.request(
        "execute",
        { code: params.code, trace_id: toolCallId },
        signal,
      );
      const ok = result.ok === true;
      const modelValue = ok
        ? result.result
        : {
            failure_stage: result.failure_stage,
            error: result.error,
            shape: result.shape,
            calls_made: result.calls_made,
            chain_calls: result.chain_calls,
          };
      const output = formatCodeMcpOutput(modelValue, outputLimits(lifecycle));
      return {
        content: [{ type: "text", text: output.text }],
        details: {
          ...output.details,
          ok,
          failureStage: typeof result.failure_stage === "string" ? result.failure_stage : undefined,
          callsMade: Number(result.calls_made ?? 0),
          chainCalls: Number(result.chain_calls ?? 0),
          ...(isRecord(result.timings) ? { timings: result.timings } : {}),
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
    promptGuidelines: [...SAVE_CHAIN_PROMPT_GUIDELINES],
    parameters: SaveChainParameters,
    async execute(toolCallId, params, signal, onUpdate) {
      const scope = requireChainScope(params.scope ?? "project");
      if (scope === "project" && lifecycle.projectChainsPath === undefined) {
        throw new Error(
          "Project saved-chain scope is unavailable in this session; ask before using global scope",
        );
      }
      onUpdate?.({
        content: [{ type: "text", text: `Validating saved chain ${params.name}...` }],
        details: undefined,
      });
      const view = await chains.save(
        {
          scope,
          name: params.name,
          description: params.description,
          code: params.code,
          inputSchema: params.inputSchema,
          outputSchema: params.outputSchema,
        },
        toolCallId,
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

  pi.registerTool({
    name: "codemcp_manage_chains",
    label: "Manage MCP Chains",
    description:
      "List saved chains, or explicitly enable, disable, revalidate, or delete one scoped chain. Mutations require confirmedByUser=true and never bypass dependency checks.",
    promptSnippet: "List or explicitly manage saved MCP chains",
    promptGuidelines: [...MANAGE_CHAIN_PROMPT_GUIDELINES],
    parameters: ManageChainsParameters,
    async execute(toolCallId, params, signal, onUpdate) {
      const action = params.action;
      let views: SavedChainView[];
      if (action === "list") {
        views = await chains.list(toolCallId, signal);
      } else {
        if (params.confirmedByUser !== true) {
          throw new Error(`${action} requires confirmedByUser=true after explicit user approval`);
        }
        if (params.name === undefined || params.scope === undefined) {
          throw new Error(`${action} requires name and scope`);
        }
        const scope = requireChainScope(params.scope);
        if (scope === "project" && lifecycle.projectChainsPath === undefined) {
          throw new Error("Project saved-chain scope is unavailable in this session");
        }
        onUpdate?.({
          content: [{ type: "text", text: `${action} saved chain ${params.name}...` }],
          details: undefined,
        });
        if (action === "enable" || action === "disable") {
          await chains.setEnabled(params.name, scope, action === "enable", toolCallId, signal);
          views = await chains.list(toolCallId, signal);
        } else if (action === "revalidate") {
          await chains.revalidate(params.name, scope, toolCallId, signal);
          views = await chains.list(toolCallId, signal);
        } else {
          views = await chains.delete(params.name, scope, toolCallId, signal);
        }
      }
      const result = {
        action,
        project_scope_available: lifecycle.projectChainsPath !== undefined,
        chains: views.map(compactChainView),
      };
      const output = formatCodeMcpOutput(result, outputLimits(lifecycle));
      return {
        content: [{ type: "text", text: output.text }],
        details: {
          ...output.details,
          action,
          chainCount: views.length,
        },
      };
    },
    renderCall(args, theme) {
      return new Text(
        `${theme.fg("toolTitle", theme.bold("Manage MCP Chains "))}${theme.fg("accent", args.action)}${args.name ? ` ${theme.fg("muted", args.name)}` : ""}`,
        0,
        0,
      );
    },
    renderResult(result, { expanded, isPartial }, theme, context) {
      if (isPartial) return new Text(theme.fg("warning", "Managing saved chains..."), 0, 0);
      const details = result.details as
        | (CodeMcpOutputDetails & { action: string; chainCount: number })
        | undefined;
      if (context.isError || !details) {
        return new Text(
          theme.fg("error", getTextContent(result.content).trim() || "Chain management failed"),
          0,
          0,
        );
      }
      if (expanded) return renderExpandedJson(result.content);
      return new Text(
        theme.fg("success", `${details.action} · ${details.chainCount} chains`),
        0,
        0,
      );
    },
  });
}

function compactChainView(view: SavedChainView) {
  return {
    name: view.chain.name,
    scope: view.scope,
    status: view.status,
    enabled: view.chain.enabled,
    description: view.chain.description,
    native_tool: nativeChainToolName(view.chain.name),
    call: `chains.${view.chain.name}`,
    stale_dependencies: view.staleDependencies,
    called_by: view.calledBy,
  };
}

function renderExpandedJson(content: readonly unknown[]): Text {
  const raw = getTextContent(content);
  let formatted = raw;
  try {
    formatted = JSON.stringify(JSON.parse(raw), null, 2);
  } catch {
    // Keep explicit non-JSON errors readable.
  }
  return new Text(highlightCode(formatted, "json").join("\n"), 0, 0);
}

function outputLimits(lifecycle: CodeMcpLifecycle): { maxBytes: number } {
  const settings = lifecycle.loadSettings();
  return { maxBytes: settings.outputLimitKiB * 1024 };
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
