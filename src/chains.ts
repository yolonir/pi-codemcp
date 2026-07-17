import { existsSync, readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { type ExtensionAPI, highlightCode, type Theme } from "@earendil-works/pi-coding-agent";
import { Text } from "@earendil-works/pi-tui";
import type { TSchema } from "typebox";
import { summarizeError } from "./errors.js";
import { previewExecutionValue, renderExecutionResult } from "./execution-rendering.js";
import type { CodeMcpLifecycle } from "./lifecycle.js";
import { formatCodeMcpOutput } from "./output.js";

export interface ChainJsonSchema extends TSchema {
  [key: string]: unknown;
}

export interface SavedChainDependency {
  kind: "mcp_tool" | "saved_chain";
  name: string;
  call: string;
  server: string;
  schemaFingerprint: string;
}

export interface SavedChainManifest {
  version: 1;
  id: string;
  name: string;
  description: string;
  code: string;
  inputSchema: ChainJsonSchema;
  outputSchema: ChainJsonSchema;
  enabled: boolean;
  dependencies: SavedChainDependency[];
  schemaFingerprint: string;
  createdAt: number;
  updatedAt: number;
  validatedAt: number;
}

export interface SavedChainView {
  chain: SavedChainManifest;
  status: "ready" | "disabled" | "stale";
  staleDependencies: string[];
  calledBy: string[];
}

export interface SaveChainInput {
  name: string;
  description: string;
  code: string;
  inputSchema: ChainJsonSchema;
  outputSchema: ChainJsonSchema;
}

interface LoadedChains {
  chains: SavedChainManifest[];
  errors: string[];
}

const CHAIN_NAME = /^[a-z][a-z0-9_]{0,63}$/;

export class SavedChainManager {
  readonly startupErrors: string[] = [];
  private readonly manifests = new Map<string, SavedChainManifest>();
  private readonly registered = new Set<string>();

  constructor(
    private readonly pi: ExtensionAPI,
    private readonly lifecycle: CodeMcpLifecycle,
  ) {
    const loaded = loadSavedChains(lifecycle.chainsPath);
    this.startupErrors.push(...loaded.errors);
    for (const chain of loaded.chains) this.manifests.set(chain.name, chain);
  }

  activatePersisted(): void {
    for (const chain of this.manifests.values()) {
      if (this.registered.has(chain.name)) continue;
      try {
        this.register(chain);
      } catch (error) {
        this.startupErrors.push(summarizeError(error));
      }
    }
    const managedNames = new Set([...this.registered].map((name) => nativeChainToolName(name)));
    const active = this.pi.getActiveTools().filter((name) => !managedNames.has(name));
    for (const chain of this.manifests.values()) {
      if (chain.enabled) active.push(nativeChainToolName(chain.name));
    }
    this.pi.setActiveTools([...new Set(active)]);
  }

  async save(input: SaveChainInput, signal?: AbortSignal): Promise<SavedChainView> {
    assertChainName(input.name);
    this.assertToolNameAvailable(input.name);
    const result = await this.lifecycle.request(
      "save_chain",
      {
        name: input.name,
        description: input.description,
        code: input.code,
        input_schema: input.inputSchema,
        output_schema: input.outputSchema,
      },
      signal,
    );
    const root = requireRecord(result.chain, "save_chain.chain");
    const view = parseSavedChainView(root, "save_chain.chain");
    this.register(view.chain);
    this.setActive(view.chain.name, true);
    return view;
  }

  async list(signal?: AbortSignal): Promise<SavedChainView[]> {
    const result = await this.lifecycle.request("list_chains", {}, signal);
    const values = Array.isArray(result.chains) ? result.chains : [];
    const views = values.map((value, index) =>
      parseSavedChainView(value, `list_chains.chains[${index}]`),
    );
    for (const view of views) {
      this.register(view.chain);
      this.setActive(view.chain.name, view.chain.enabled);
    }
    return views;
  }

  async setEnabled(name: string, enabled: boolean, signal?: AbortSignal): Promise<SavedChainView> {
    const result = await this.lifecycle.request("set_chain_enabled", { name, enabled }, signal);
    const view = parseSavedChainView(result, "set_chain_enabled");
    this.register(view.chain);
    this.setActive(name, enabled);
    return view;
  }

  async revalidate(name: string, signal?: AbortSignal): Promise<SavedChainView> {
    const result = await this.lifecycle.request("revalidate_chain", { name }, signal);
    const view = parseSavedChainView(result, "revalidate_chain");
    this.register(view.chain);
    return view;
  }

  async delete(name: string, signal?: AbortSignal): Promise<SavedChainView[]> {
    const result = await this.lifecycle.request("delete_chain", { name }, signal);
    this.setActive(name, false);
    this.manifests.delete(name);
    const values = Array.isArray(result.chains) ? result.chains : [];
    return values.map((value, index) =>
      parseSavedChainView(value, `delete_chain.chains[${index}]`),
    );
  }

  private register(chain: SavedChainManifest): void {
    this.assertToolNameAvailable(chain.name);
    const manager = this;
    this.pi.registerTool({
      name: nativeChainToolName(chain.name),
      label: chain.name,
      description: chain.description,
      parameters: chain.inputSchema,
      async execute(_toolCallId, params, signal, onUpdate) {
        onUpdate?.({
          content: [{ type: "text", text: `Running saved MCP chain ${chain.name}...` }],
          details: undefined,
        });
        const arguments_ = requireRecord(params, `${chain.name} arguments`);
        const result = await manager.lifecycle.request(
          "execute_chain",
          { name: chain.name, arguments: arguments_ },
          signal,
        );
        if (result.ok !== true) {
          const error =
            typeof result.error === "string" ? result.error : `Saved chain ${chain.name} failed`;
          throw new Error(error);
        }
        const settings = manager.lifecycle.loadSettings();
        const output = formatCodeMcpOutput(result, {
          maxBytes: settings.outputLimitKiB * 1024,
          maxLines: settings.outputLineLimit,
        });
        return {
          content: [{ type: "text", text: output.text }],
          details: {
            ...output.details,
            chain: chain.name,
            ok: true,
            callsMade: Number(result.calls_made ?? 0),
            chainCalls: Number(result.chain_calls ?? 0),
            preview: previewExecutionValue(result.result),
          },
        };
      },
      renderCall(args, theme, context) {
        return renderSavedChainCall(
          chain,
          requireRecord(args, `${chain.name} arguments`),
          theme,
          context.expanded,
        );
      },
      renderResult(result, state, theme) {
        return renderExecutionResult(result, state, theme, {
          partialText: `Running saved MCP chain ${chain.name}...`,
          expandDescription: "arguments and full output",
        });
      },
    });
    this.manifests.set(chain.name, chain);
    this.registered.add(chain.name);
  }

  private assertToolNameAvailable(name: string): void {
    if (this.registered.has(name)) return;
    const nativeName = nativeChainToolName(name);
    if (this.pi.getAllTools().some((tool) => tool.name === nativeName)) {
      throw new Error(
        `Cannot register saved chain ${name}: native tool ${nativeName} already exists`,
      );
    }
  }

  private setActive(name: string, enabled: boolean): void {
    const nativeName = nativeChainToolName(name);
    const active = this.pi.getActiveTools().filter((toolName) => toolName !== nativeName);
    if (enabled) active.push(nativeName);
    this.pi.setActiveTools([...new Set(active)]);
  }
}

function renderSavedChainCall(
  chain: SavedChainManifest,
  args: Record<string, unknown>,
  theme: Theme,
  expanded: boolean,
): Text {
  const title = theme.fg("toolTitle", theme.bold("MCP Chain"));
  const name = theme.fg("accent", theme.bold(chain.name));
  const count = Object.keys(args).length;
  const argumentLabel = `${count} ${count === 1 ? "argument" : "arguments"}`;
  if (expanded) {
    const serialized = JSON.stringify(args, null, 2);
    return new Text(
      `${title} ${name}\n${theme.fg("accent", theme.bold(`Arguments · ${argumentLabel}`))}\n${highlightCode(serialized, "json").join("\n")}`,
      0,
      0,
    );
  }
  return new Text(
    `${title} ${name} ${theme.fg("muted", "·")} ${theme.fg("muted", argumentLabel)}`,
    0,
    0,
  );
}

export function nativeChainToolName(name: string): string {
  return `mcp_chain_${name}`;
}

export function parseSavedChainView(value: unknown, label: string): SavedChainView {
  const root = requireRecord(value, label);
  const status = root.status;
  if (status !== "ready" && status !== "disabled" && status !== "stale") {
    throw new TypeError(`${label}.status must be ready, disabled, or stale`);
  }
  return {
    chain: parseSavedChainManifest(root.chain, `${label}.chain`),
    status,
    staleDependencies: stringArray(root.stale_dependencies, `${label}.stale_dependencies`),
    calledBy: stringArray(root.called_by, `${label}.called_by`),
  };
}

export function parseSavedChainManifest(value: unknown, label: string): SavedChainManifest {
  const root = requireRecord(value, label);
  if (root.version !== 1) throw new TypeError(`${label}.version must be 1`);
  const name = requiredString(root.name, `${label}.name`);
  assertChainName(name);
  const dependencies = Array.isArray(root.dependencies)
    ? root.dependencies.map((dependency, index) =>
        parseDependency(dependency, `${label}.dependencies[${index}]`),
      )
    : [];
  return {
    version: 1,
    id: requiredString(root.id, `${label}.id`),
    name,
    description: requiredString(root.description, `${label}.description`),
    code: requiredString(root.code, `${label}.code`),
    inputSchema: requireSchema(root.input_schema, `${label}.input_schema`, true),
    outputSchema: requireSchema(root.output_schema, `${label}.output_schema`, false),
    enabled: requiredBoolean(root.enabled, `${label}.enabled`),
    dependencies,
    schemaFingerprint: requiredString(root.schema_fingerprint, `${label}.schema_fingerprint`),
    createdAt: requiredNumber(root.created_at, `${label}.created_at`),
    updatedAt: requiredNumber(root.updated_at, `${label}.updated_at`),
    validatedAt: requiredNumber(root.validated_at, `${label}.validated_at`),
  };
}

function loadSavedChains(directory: string): LoadedChains {
  if (!existsSync(directory)) return { chains: [], errors: [] };
  const chains: SavedChainManifest[] = [];
  const errors: string[] = [];
  for (const filename of readdirSync(directory)
    .filter((name) => name.endsWith(".json"))
    .sort()) {
    const path = join(directory, filename);
    try {
      const parsed: unknown = JSON.parse(readFileSync(path, "utf8"));
      chains.push(parseSavedChainManifest(parsed, path));
    } catch (error) {
      errors.push(`Saved chain ${filename} failed to load: ${summarizeError(error)}`);
    }
  }
  return { chains, errors };
}

function parseDependency(value: unknown, label: string): SavedChainDependency {
  const root = requireRecord(value, label);
  const kind = root.kind;
  if (kind !== "mcp_tool" && kind !== "saved_chain") {
    throw new TypeError(`${label}.kind must be mcp_tool or saved_chain`);
  }
  return {
    kind,
    name: requiredString(root.name, `${label}.name`),
    call: requiredString(root.call, `${label}.call`),
    server: requiredString(root.server, `${label}.server`),
    schemaFingerprint: requiredString(root.schema_fingerprint, `${label}.schema_fingerprint`),
  };
}

function requireSchema(value: unknown, label: string, requireObject: boolean): ChainJsonSchema {
  const schema = requireRecord(value, label);
  if (requireObject && schema.type !== "object") {
    throw new TypeError(`${label}.type must be object`);
  }
  return schema;
}

function assertChainName(name: string): void {
  if (!CHAIN_NAME.test(name)) {
    throw new TypeError(
      "Saved chain name must start with a lowercase letter and contain only lowercase letters, digits, and underscores (maximum 64 characters)",
    );
  }
}

function requiredString(value: unknown, label: string): string {
  if (typeof value !== "string" || !value) throw new TypeError(`${label} must be a string`);
  return value;
}

function requiredBoolean(value: unknown, label: string): boolean {
  if (typeof value !== "boolean") throw new TypeError(`${label} must be a boolean`);
  return value;
}

function requiredNumber(value: unknown, label: string): number {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new TypeError(`${label} must be a number`);
  }
  return value;
}

function stringArray(value: unknown, label: string): string[] {
  if (!Array.isArray(value) || value.some((item) => typeof item !== "string")) {
    throw new TypeError(`${label} must be an array of strings`);
  }
  return [...new Set(value)];
}

function requireRecord(value: unknown, label: string): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new TypeError(`${label} must be an object`);
  }
  return Object.fromEntries(Object.entries(value));
}
