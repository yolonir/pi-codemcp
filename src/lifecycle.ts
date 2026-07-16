import { SidecarClient, type SidecarClientOptions } from "./mcp-client.js";

export class CodeModeLifecycle {
  readonly sidecar: SidecarClient;

  constructor(options: SidecarClientOptions = {}) {
    this.sidecar = new SidecarClient(options);
  }

  async request(
    name: "search" | "get_schema" | "execute" | "status",
    args: Record<string, unknown>,
    signal?: AbortSignal,
  ): Promise<Record<string, unknown>> {
    return this.sidecar.call(name, args, signal);
  }

  async shutdown(): Promise<void> {
    await this.sidecar.close();
  }
}
