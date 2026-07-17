import {
  type JsonRecord,
  readJsonObject,
  requireJsonObject,
  writeJsonObjectAtomically,
} from "./json-file.js";

export interface McpServerEnabledChange {
  name: string;
  enabled: boolean;
}

export function setMcpServerEnabled(configPath: string, name: string, enabled: boolean): void {
  setMcpServersEnabled(configPath, [{ name, enabled }]);
}

export function setMcpServersEnabled(
  configPath: string,
  changes: readonly McpServerEnabledChange[],
): void {
  if (changes.length === 0) return;
  const duplicate = duplicateName(changes.map((change) => change.name));
  if (duplicate) throw new Error(`Duplicate MCP server change: ${JSON.stringify(duplicate)}`);

  const root = readJsonObject(configPath, "mcp.json root");
  const hasServerBlock = Object.hasOwn(root, "mcpServers");
  const servers = hasServerBlock ? requireJsonObject(root.mcpServers, "mcp.json mcpServers") : root;
  const updatedServers: JsonRecord = { ...servers };

  for (const change of changes) {
    const server = requireJsonObject(
      servers[change.name],
      `MCP server ${JSON.stringify(change.name)}`,
    );
    const updatedServer: JsonRecord = { ...server };
    if (typeof server.enabled === "boolean") {
      updatedServer.enabled = change.enabled;
      delete updatedServer.disabled;
    } else {
      updatedServer.disabled = !change.enabled;
    }
    updatedServers[change.name] = updatedServer;
  }

  const updatedRoot: JsonRecord = hasServerBlock
    ? { ...root, mcpServers: updatedServers }
    : updatedServers;
  writeJsonObjectAtomically(configPath, updatedRoot);
}

function duplicateName(names: readonly string[]): string | undefined {
  const seen = new Set<string>();
  for (const name of names) {
    if (seen.has(name)) return name;
    seen.add(name);
  }
  return undefined;
}
