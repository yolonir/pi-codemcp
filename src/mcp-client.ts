import { fileURLToPath } from "node:url";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";

type JsonObject = Record<string, unknown>;

export interface SidecarClientOptions {
  packageRoot?: string;
  environment?: Record<string, string>;
}

export class SidecarClient {
  private readonly packageRoot: string;
  private readonly environment: Record<string, string>;
  private client: Client | undefined;
  private transport: StdioClientTransport | undefined;
  private startPromise: Promise<void> | undefined;
  private closePromise: Promise<void> | undefined;
  private stderrTail = "";

  constructor(options: SidecarClientOptions = {}) {
    this.packageRoot = options.packageRoot ?? fileURLToPath(new URL("..", import.meta.url));
    this.environment = {
      ...codeModeEnvironment(),
      ...(options.environment ?? {}),
    };
  }

  get pid(): number | null {
    return this.transport?.pid ?? null;
  }

  get connected(): boolean {
    return this.client !== undefined && this.transport?.pid !== null;
  }

  async call(
    name: "search" | "get_schema" | "execute" | "status",
    args: JsonObject,
    signal?: AbortSignal,
  ): Promise<JsonObject> {
    await this.ensureStarted(signal);
    const client = this.client;
    if (!client) throw new Error("Sidecar client failed to initialize");
    const timeout = name === "execute" ? 35_000 : 30_000;
    const result = await client.callTool({ name, arguments: args }, undefined, { signal, timeout });

    if (result.isError) {
      throw new Error(textContent(result.content) || `Sidecar tool ${name} failed`);
    }
    if (isJsonObject(result.structuredContent)) {
      return result.structuredContent;
    }

    const text = textContent(result.content);
    if (!text) {
      throw new Error(`Sidecar tool ${name} returned no structured result`);
    }
    const parsed: unknown = JSON.parse(text);
    if (!isJsonObject(parsed)) {
      throw new Error(`Sidecar tool ${name} returned a non-object result`);
    }
    return parsed;
  }

  async close(): Promise<void> {
    if (this.closePromise) return this.closePromise;
    this.closePromise = this.closeInternal();
    try {
      await this.closePromise;
    } finally {
      this.closePromise = undefined;
    }
  }

  private async ensureStarted(signal?: AbortSignal): Promise<void> {
    if (this.client && this.transport && !this.startPromise) return;
    if (!this.startPromise) {
      this.startPromise = this.start(signal).catch(async (error: unknown) => {
        await this.closeInternal();
        const detail = this.stderrTail.trim();
        const message = error instanceof Error ? error.message : String(error);
        throw new Error(detail ? `${message}\n${detail}` : message, { cause: error });
      });
    }

    const startPromise = this.startPromise;
    try {
      await startPromise;
    } finally {
      if (this.startPromise === startPromise) this.startPromise = undefined;
    }
  }

  private async start(signal?: AbortSignal): Promise<void> {
    this.stderrTail = "";
    const transport = new StdioClientTransport({
      command: "uv",
      args: ["run", "--project", "sidecar", "--frozen", "-m", "sidecar.gateway"],
      cwd: this.packageRoot,
      env: this.environment,
      stderr: "pipe",
    });
    transport.stderr?.on("data", (chunk: Buffer | string) => {
      this.stderrTail = `${this.stderrTail}${chunk.toString()}`.slice(-8192);
    });

    const client = new Client({
      name: "pi-mcp-codemode",
      version: "0.1.0",
    });
    this.transport = transport;
    this.client = client;
    await client.connect(transport, {
      signal,
      timeout: 310_000,
    });
  }

  private async closeInternal(): Promise<void> {
    const client = this.client;
    const transport = this.transport;
    this.client = undefined;
    this.transport = undefined;
    this.startPromise = undefined;

    if (client) {
      try {
        await client.close();
      } finally {
        if (transport) await transport.close();
      }
    } else if (transport) {
      await transport.close();
    }
  }
}

function codeModeEnvironment(): Record<string, string> {
  const environment: Record<string, string> = {};
  for (const name of [
    "PI_MCP_CODEMODE_CONFIG",
    "PI_MCP_CODEMODE_OAUTH_DIR",
    "PI_MCP_CODEMODE_CATALOG_DIR",
  ]) {
    const value = process.env[name];
    if (value !== undefined) environment[name] = value;
  }
  return environment;
}

function isJsonObject(value: unknown): value is JsonObject {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function textContent(content: unknown): string {
  if (!Array.isArray(content)) return "";
  return content
    .filter(
      (item): item is { type: "text"; text: string } =>
        isJsonObject(item) && item.type === "text" && typeof item.text === "string",
    )
    .map((item) => item.text)
    .join("\n");
}
