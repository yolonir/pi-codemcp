import { existsSync } from "node:fs";
import {
  type JsonRecord,
  readJsonObject,
  requireJsonObject,
  writeJsonObjectAtomically,
} from "./json-file.js";

export interface McpServerDraft {
  name: string;
  config: JsonRecord;
}

export function parseMcpServerDraft(text: string): McpServerDraft {
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch (error) {
    throw new Error("Server config is not valid JSON", { cause: error });
  }
  const root = requireJsonObject(parsed, "Server config");
  let name: unknown;
  let config: unknown;

  if (typeof root.name === "string" && Object.hasOwn(root, "config")) {
    name = root.name;
    config = root.config;
  } else if (
    typeof root.name === "string" &&
    (Object.hasOwn(root, "command") || Object.hasOwn(root, "url"))
  ) {
    name = root.name;
    config = Object.fromEntries(Object.entries(root).filter(([key]) => key !== "name"));
  } else {
    const servers = Object.hasOwn(root, "mcpServers")
      ? requireJsonObject(root.mcpServers, "Server config mcpServers")
      : root;
    const entries = Object.entries(servers);
    if (entries.length !== 1) {
      throw new Error("Paste exactly one named MCP server entry");
    }
    [name, config] = entries[0] ?? [];
  }

  requireServerName(name);
  const validated = requireJsonObject(config, `MCP server ${JSON.stringify(name)}`);
  validateMcpServerConfig(name, validated);
  return { name, config: validated };
}

export function addMcpServer(configPath: string, draft: McpServerDraft): void {
  requireServerName(draft.name);
  validateMcpServerConfig(draft.name, draft.config);
  const root = existsSync(configPath) ? readJsonObject(configPath, "mcp.json root") : {};
  const hasServerBlock = Object.hasOwn(root, "mcpServers") || !existsSync(configPath);
  const servers = Object.hasOwn(root, "mcpServers")
    ? requireJsonObject(root.mcpServers, "mcp.json mcpServers")
    : hasServerBlock
      ? {}
      : root;
  const collision = Object.keys(servers).find(
    (name) => normalizedServerName(name) === normalizedServerName(draft.name),
  );
  if (collision) throw new Error(`MCP server already exists: ${collision}`);
  const updatedServers: JsonRecord = { ...servers, [draft.name]: draft.config };
  writeJsonObjectAtomically(
    configPath,
    hasServerBlock ? { ...root, mcpServers: updatedServers } : updatedServers,
  );
}

export function removeMcpServer(configPath: string, name: string): void {
  const root = readJsonObject(configPath, "mcp.json root");
  const hasServerBlock = Object.hasOwn(root, "mcpServers");
  const servers = hasServerBlock ? requireJsonObject(root.mcpServers, "mcp.json mcpServers") : root;
  if (!Object.hasOwn(servers, name)) return;
  const updatedServers = Object.fromEntries(
    Object.entries(servers).filter(([key]) => key !== name),
  );
  writeJsonObjectAtomically(
    configPath,
    hasServerBlock ? { ...root, mcpServers: updatedServers } : updatedServers,
  );
}

export function setMcpServerEnabled(configPath: string, name: string, enabled: boolean): void {
  const root = readJsonObject(configPath, "mcp.json root");
  const hasServerBlock = Object.hasOwn(root, "mcpServers");
  const servers = hasServerBlock ? requireJsonObject(root.mcpServers, "mcp.json mcpServers") : root;
  const server = requireJsonObject(servers[name], `MCP server ${JSON.stringify(name)}`);
  const updatedServer: JsonRecord = { ...server };

  if (typeof server.enabled === "boolean") {
    updatedServer.enabled = enabled;
    delete updatedServer.disabled;
  } else {
    updatedServer.disabled = !enabled;
  }

  const updatedServers: JsonRecord = { ...servers, [name]: updatedServer };
  const updatedRoot: JsonRecord = hasServerBlock
    ? { ...root, mcpServers: updatedServers }
    : updatedServers;
  writeJsonObjectAtomically(configPath, updatedRoot);
}

function requireServerName(value: unknown): asserts value is string {
  if (typeof value !== "string" || !/^[A-Za-z0-9_-]+$/.test(value)) {
    throw new TypeError("MCP server names may contain only letters, digits, _ and -");
  }
}

function normalizedServerName(name: string): string {
  return name.replaceAll("-", "_");
}

function validateMcpServerConfig(name: string, config: JsonRecord): void {
  const hasCommand = Object.hasOwn(config, "command");
  const hasUrl = Object.hasOwn(config, "url");
  if (hasCommand === hasUrl) {
    throw new Error(`MCP server ${JSON.stringify(name)} must define exactly one of command or url`);
  }
  if (hasCommand) {
    requireNonEmptyString(config.command, `${name}.command`);
    if (config.type !== undefined && config.type !== "stdio") {
      throw new TypeError(`${name}.type must be stdio`);
    }
    requireStringArray(config.args, `${name}.args`);
    requireStringRecord(config.env, `${name}.env`);
    requireOptionalString(config.cwd, `${name}.cwd`);
  } else {
    const url = requireNonEmptyString(config.url, `${name}.url`);
    let parsed: URL;
    try {
      parsed = new URL(url);
    } catch (error) {
      throw new TypeError(`${name}.url must be a valid URL`, { cause: error });
    }
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
      throw new TypeError(`${name}.url must use http or https`);
    }
    if (config.type !== undefined && config.type !== "http" && config.type !== "streamable-http") {
      throw new TypeError(`${name}.type must be http or streamable-http`);
    }
    requireStringRecord(config.headers, `${name}.headers`);
    if (config.oauth !== undefined) requireJsonObject(config.oauth, `${name}.oauth`);
  }
  requireOptionalString(config.description, `${name}.description`);
  requireOptionalBoolean(config.enabled, `${name}.enabled`);
  requireOptionalBoolean(config.disabled, `${name}.disabled`);
  if (
    config.timeout !== undefined &&
    (typeof config.timeout !== "number" || !Number.isFinite(config.timeout) || config.timeout <= 0)
  ) {
    throw new TypeError(`${name}.timeout must be a positive number`);
  }
  if (
    config.exposure !== undefined &&
    !["codemode", "codemode-deferred", "deferred", "direct", "hidden"].includes(
      String(config.exposure),
    )
  ) {
    throw new TypeError(`${name}.exposure is invalid`);
  }
  if (config.toolExposure !== undefined) {
    const toolExposure = requireJsonObject(config.toolExposure, `${name}.toolExposure`);
    for (const [tool, exposure] of Object.entries(toolExposure)) {
      if (
        typeof exposure !== "string" ||
        !["codemode", "codemode-deferred", "deferred", "direct", "hidden"].includes(exposure)
      ) {
        throw new TypeError(`${name}.toolExposure.${tool} is invalid`);
      }
    }
  }
}

function requireNonEmptyString(value: unknown, label: string): string {
  if (typeof value !== "string" || !value.trim()) {
    throw new TypeError(`${label} must be a non-empty string`);
  }
  return value;
}

function requireOptionalString(value: unknown, label: string): void {
  if (value !== undefined && typeof value !== "string") {
    throw new TypeError(`${label} must be a string`);
  }
}

function requireOptionalBoolean(value: unknown, label: string): void {
  if (value !== undefined && typeof value !== "boolean") {
    throw new TypeError(`${label} must be a boolean`);
  }
}

function requireStringArray(value: unknown, label: string): void {
  if (
    value !== undefined &&
    (!Array.isArray(value) || value.some((item) => typeof item !== "string"))
  ) {
    throw new TypeError(`${label} must be an array of strings`);
  }
}

function requireStringRecord(value: unknown, label: string): void {
  if (value === undefined) return;
  const record = requireJsonObject(value, label);
  if (Object.values(record).some((item) => typeof item !== "string")) {
    throw new TypeError(`${label} values must be strings`);
  }
}
