import { randomUUID } from "node:crypto";
import { noul, type Questions, type TypeSafeClient } from "@typesafe-ai/sdk";
import type { CodeMcpLifecycle } from "./lifecycle.js";

const RELEVANCE_THRESHOLD = 0.3;
// ponytail: bound injected schemas; raise only if routing evals show recall loss.
const MAX_SELECTED_TOOLS = 8;
const CATALOG_PAGE_SIZE = 20;
const RECENT_CONTEXT_CHAR_LIMIT = 6_000;

interface CatalogTool {
  call: string;
  description?: string;
}

interface ScoredTool extends CatalogTool {
  relevance: number;
}

export interface JevRoute {
  prompt: string;
  selected: ScoredTool[];
  needsAnyTool: number;
}

export class JevRouter {
  constructor(
    private readonly lifecycle: Pick<CodeMcpLifecycle, "request">,
    private readonly client: TypeSafeClient,
  ) {}

  async route(task: string, recentContext: string, signal?: AbortSignal): Promise<JevRoute> {
    const catalog = await this.loadCatalog(signal);
    if (catalog.length === 0) {
      return {
        prompt: jevPrompt([], undefined),
        selected: [],
        needsAnyTool: 0,
      };
    }

    const questions: Questions = {
      needs_any_tool: noul(
        "Does satisfying the user's request require calling at least one of the configured external-service or saved-workflow tools?",
        {
          true: "The request needs current, private, or external state, or asks for an action provided by a configured tool.",
          false:
            "The request can be fully satisfied with explanation, reasoning, or local coding tools alone.",
        },
      ),
    };
    for (const [index, tool] of catalog.entries()) {
      questions[`tool_${index}`] = noul(
        {
          question:
            "Would calling this tool materially help satisfy an explicit part of the user's request?",
          tool: {
            call: tool.call,
            description: tool.description ?? tool.call,
          },
        },
        {
          true: "This tool directly provides information or performs an action needed by the request.",
          false:
            "This tool is unrelated, merely adjacent, or unnecessary for satisfying the request.",
        },
      );
    }

    const response = await this.client.systemOne(
      {
        state: {
          task,
          ...(recentContext ? { recent_context: recentContext } : {}),
        },
        questions,
      },
      signal ? { signal } : undefined,
    );
    const needsAnyTool = noulValue(response.answers.needs_any_tool);
    const ranked = catalog
      .map((tool, index) => ({
        ...tool,
        relevance: noulValue(response.answers[`tool_${index}`]),
      }))
      .sort(
        (left, right) => right.relevance - left.relevance || left.call.localeCompare(right.call),
      );
    const selected =
      needsAnyTool >= RELEVANCE_THRESHOLD
        ? ranked
            .filter((tool) => tool.relevance >= RELEVANCE_THRESHOLD)
            .slice(0, MAX_SELECTED_TOOLS)
        : [];
    if (selected.length === 0) {
      return {
        prompt: jevPrompt([], undefined),
        selected,
        needsAnyTool,
      };
    }

    const inspection = await this.lifecycle.request(
      "inspect",
      {
        calls: selected.map((tool) => tool.call),
        trace_id: `jev-${randomUUID()}`,
      },
      signal,
    );
    const prelude = typeof inspection.prelude === "string" ? inspection.prelude : "";
    const stubs = Array.isArray(inspection.results)
      ? inspection.results.flatMap((item) => {
          if (!isRecord(item) || typeof item.stub !== "string") return [];
          return [item.stub];
        })
      : [];
    if (stubs.length !== selected.length) {
      throw new Error("CodeMCP inspect returned incomplete Jev-selected contracts");
    }

    return {
      prompt: jevPrompt(selected, [prelude, ...stubs].filter(Boolean).join("\n\n")),
      selected,
      needsAnyTool,
    };
  }

  private async loadCatalog(signal?: AbortSignal): Promise<CatalogTool[]> {
    const tools: CatalogTool[] = [];
    let cursor = 0;
    while (true) {
      const page = await this.lifecycle.request(
        "search",
        {
          mode: "inventory",
          detail: "names",
          limit: CATALOG_PAGE_SIZE,
          cursor,
          trace_id: `jev-${randomUUID()}`,
        },
        signal,
      );
      if (Array.isArray(page.results)) {
        for (const item of page.results) {
          if (!isRecord(item) || typeof item.call !== "string") continue;
          tools.push({
            call: item.call,
            ...(typeof item.description === "string" ? { description: item.description } : {}),
          });
        }
      }
      if (typeof page.next_cursor !== "number") break;
      if (page.next_cursor <= cursor) throw new Error("CodeMCP inventory cursor did not advance");
      cursor = page.next_cursor;
    }
    return tools;
  }
}

export function recentConversation(messages: readonly unknown[], currentPrompt: string): string {
  const sections: string[] = [];
  for (const value of messages) {
    if (!isRecord(value)) continue;
    const message = isRecord(value.message) ? value.message : value;
    if (message.role !== "user" && message.role !== "assistant") continue;
    const text = textContent(message.content).trim();
    if (!text || (message.role === "user" && text === currentPrompt.trim())) continue;
    sections.push(`${message.role === "user" ? "User" : "Assistant"}: ${text}`);
  }
  return sections.slice(-4).join("\n\n").slice(-RECENT_CONTEXT_CHAR_LIMIT);
}

function jevPrompt(selected: ScoredTool[], contracts: string | undefined): string {
  if (selected.length === 0 || !contracts) {
    return "<codemcp_jev>Jev found no configured MCP tool relevant to this request.</codemcp_jev>";
  }
  return [
    "<codemcp_jev>",
    `Jev selected these MCP calls for the current request: ${selected.map((tool) => tool.call).join(", ")}.`,
    "Use their exact typed SDK contracts through codemcp_execute:",
    "```python",
    contracts,
    "```",
    "</codemcp_jev>",
  ].join("\n");
}

function noulValue(answer: unknown): number {
  if (
    !isRecord(answer) ||
    answer.type !== "noul" ||
    typeof answer.noul !== "number" ||
    !Number.isFinite(answer.noul)
  ) {
    throw new Error("TypeSafe returned an invalid Jev routing answer");
  }
  return answer.noul;
}

function textContent(content: unknown): string {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content
    .flatMap((item) =>
      isRecord(item) && item.type === "text" && typeof item.text === "string" ? [item.text] : [],
    )
    .join("\n");
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
