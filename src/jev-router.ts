import { randomUUID } from "node:crypto";
import { choice, noul, type Questions, type TypeSafeClient } from "@typesafe-ai/sdk";
import type { CodeMcpLifecycle } from "./lifecycle.js";

const TOOL_RELEVANCE_THRESHOLD = 0.65;
const NOUL_THRESHOLD = 0.5;
// ponytail: bound injected schemas; raise only if routing evals show recall loss.
const MAX_SELECTED_TOOLS = 8;
const CATALOG_PAGE_SIZE = 20;
const JEV_CHUNK_SIZE = 40;

type ToolRole = "source" | "enrichment" | "sink" | "standalone" | "unspecified";
const TOOL_ROLE_BY_CHOICE: Readonly<Record<string, ToolRole>> = {
  source: "source",
  enrichment: "enrichment",
  sink: "sink",
  standalone: "standalone",
  irrelevant: "unspecified",
};
type WorkflowShape = "single_call" | "parallel" | "pipeline" | "mixed";

interface CatalogTool {
  call: string;
  description?: string;
}

interface IndexedTool {
  index: number;
  tool: CatalogTool;
}

interface ChunkResult {
  tools: JevSelectedTool[];
  needsAnyTool?: number;
  needsCheckpoint?: number;
}

export interface JevSelectedTool extends CatalogTool {
  relevance: number;
  role: ToolRole;
}

export interface JevRoute {
  prompt: string;
  selected: JevSelectedTool[];
  needsAnyTool: number;
  workflowShape: WorkflowShape;
  needsCheckpoint: number;
}

export class JevRouter {
  constructor(
    private readonly lifecycle: Pick<CodeMcpLifecycle, "request">,
    private readonly client: TypeSafeClient,
  ) {}

  async route(task: string, recentContext = "", signal?: AbortSignal): Promise<JevRoute> {
    const catalog = await this.loadCatalog(signal);
    if (catalog.length === 0) return emptyRoute();

    const chunks = chunk(
      catalog.map((tool, index) => ({ tool, index })),
      JEV_CHUNK_SIZE,
    );
    const results = await Promise.all(
      chunks.map((tools, index) =>
        this.evaluateChunk(task, recentContext, tools, index === 0, signal),
      ),
    );
    const needsAnyTool = results[0]?.needsAnyTool ?? 0;
    const needsCheckpoint = results[0]?.needsCheckpoint ?? 0;
    const ranked = results
      .flatMap((result) => result.tools)
      .sort(
        (left, right) => right.relevance - left.relevance || left.call.localeCompare(right.call),
      );
    const selected =
      needsAnyTool >= NOUL_THRESHOLD
        ? ranked
            .filter((tool) => tool.relevance >= TOOL_RELEVANCE_THRESHOLD)
            .slice(0, MAX_SELECTED_TOOLS)
        : [];
    const workflowShape = workflowShapeFor(selected);
    if (selected.length === 0) {
      return { ...emptyRoute(), needsAnyTool, needsCheckpoint, workflowShape };
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
      prompt: jevPrompt(
        selected,
        workflowShape,
        needsCheckpoint,
        [prelude, ...stubs].filter(Boolean).join("\n\n"),
      ),
      selected,
      needsAnyTool,
      workflowShape,
      needsCheckpoint,
    };
  }

  private async evaluateChunk(
    task: string,
    recentContext: string,
    tools: IndexedTool[],
    includeTaskQuestions: boolean,
    signal?: AbortSignal,
  ): Promise<ChunkResult> {
    const questions: Questions = {};
    if (includeTaskQuestions) {
      questions.needs_any_tool = noul(
        "Does the current task (the agent's routing intent) require at least one configured external-service or saved-workflow tool? Use the original user request as context, not as a requirement that every step be explicitly named.",
        {
          true: "The request needs current, private, or external state, or asks for an external action.",
          false: "Explanation, reasoning, or local coding tools can fully satisfy the request.",
        },
      );
      questions.needs_checkpoint = noul(
        "For the current task, must the agent inspect an intermediate result, make a semantic decision, or obtain user approval before the next external call?",
        {
          true: "A model or user decision is required between tool stages.",
          false: "One deterministic CodeMCP program can safely run the complete workflow.",
        },
      );
    }
    for (const { index, tool } of tools) {
      const toolDescription = {
        call: tool.call,
        description: tool.description ?? tool.call,
      };
      questions[`tool_${index}`] = noul(
        {
          question:
            "Is this tool needed for the current task (the agent's routing intent), including prerequisite discovery or diagnostic calls? The user need not explicitly name each step.",
          tool: toolDescription,
        },
        {
          true: "The task needs this capability directly or as a prerequisite, such as finding a datasource before querying logs.",
          false: "The tool is unrelated, redundant, optional, or merely adjacent.",
        },
      );
      questions[`role_${index}`] = choice(
        {
          question: "What role should this tool have in the minimal workflow for the current task?",
          tool: toolDescription,
        },
        {
          source: "Retrieves initial data.",
          enrichment: "Retrieves data using an earlier result.",
          sink: "Performs a downstream action using earlier results.",
          standalone: "Independently completes one requested action.",
          irrelevant: "Should not be used for this task.",
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
    return {
      tools: tools.map(({ index, tool }) => ({
        ...tool,
        relevance: noulValue(response.answers[`tool_${index}`]),
        role: toolRoleValue(response.answers[`role_${index}`]),
      })),
      ...(includeTaskQuestions
        ? {
            needsAnyTool: noulValue(response.answers.needs_any_tool),
            needsCheckpoint: noulValue(response.answers.needs_checkpoint),
          }
        : {}),
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

function emptyRoute(): JevRoute {
  return {
    prompt: "Jev found no configured MCP tool relevant to this request.",
    selected: [],
    needsAnyTool: 0,
    workflowShape: "single_call",
    needsCheckpoint: 0,
  };
}

function workflowShapeFor(selected: JevSelectedTool[]): WorkflowShape {
  if (selected.length <= 1) return "single_call";
  const independent = selected.filter(
    (tool) => tool.role === "source" || tool.role === "standalone",
  ).length;
  const downstream = selected.some((tool) => tool.role === "enrichment" || tool.role === "sink");
  if (!downstream) return "parallel";
  return independent > 1 ? "mixed" : "pipeline";
}

function jevPrompt(
  selected: JevSelectedTool[],
  workflowShape: WorkflowShape,
  needsCheckpoint: number,
  contracts: string,
): string {
  const roles = selected.map((tool) => `- ${tool.call}: ${tool.role}`).join("\n");
  const execution =
    needsCheckpoint >= NOUL_THRESHOLD
      ? "Run only the first stage in codemcp_execute, return compact decision data, then preserve a model/user checkpoint before downstream calls."
      : workflowInstruction(workflowShape);
  return [
    `Jev selected these MCP calls:\n${roles}`,
    `Composition: ${workflowShape}.`,
    `Execution recommendation: ${execution}`,
    "Use these exact typed SDK contracts:",
    "```python",
    contracts,
    "```",
  ].join("\n\n");
}

function workflowInstruction(shape: WorkflowShape): string {
  switch (shape) {
    case "single_call":
      return "Write and run one minimal codemcp_execute program using the selected call.";
    case "parallel":
      return "Write and run one codemcp_execute program using asyncio.gather for independent calls, then combine their results locally.";
    case "pipeline":
      return "Write and run one codemcp_execute program that passes earlier outputs into dependent calls.";
    case "mixed":
      return "Write and run one codemcp_execute program that gathers independent source calls, then feeds their results into downstream calls.";
  }
}

function toolRoleValue(answer: unknown): ToolRole {
  const role = TOOL_ROLE_BY_CHOICE[choiceValue(answer)];
  if (!role) throw new Error("TypeSafe returned an invalid Jev tool role");
  return role;
}

function choiceValue(answer: unknown): string {
  if (!isRecord(answer) || answer.type !== "choice" || typeof answer.choice !== "string") {
    throw new Error("TypeSafe returned an invalid Jev routing choice");
  }
  return answer.choice;
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

function chunk<T>(values: T[], size: number): T[][] {
  const chunks: T[][] = [];
  for (let index = 0; index < values.length; index += size) {
    chunks.push(values.slice(index, index + size));
  }
  return chunks;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
