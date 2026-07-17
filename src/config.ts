import {
  type JsonRecord,
  readJsonObject,
  requireJsonObject,
  writeJsonObjectAtomically,
} from "./json-file.js";

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
