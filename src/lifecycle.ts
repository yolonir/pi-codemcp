import { SidecarClient, type SidecarClientOptions } from "./mcp-client.js";
import { type CodeMcpSettings, loadCodeMcpSettings } from "./settings.js";

export class CodeMcpLifecycle {
  readonly sidecar: SidecarClient;
  private reloadBarrier: Promise<void> = Promise.resolve();

  constructor(options: SidecarClientOptions = {}) {
    this.sidecar = new SidecarClient(options);
  }

  get configPath(): string {
    return this.sidecar.configPath;
  }

  get settingsPath(): string {
    return this.sidecar.settingsPath;
  }

  loadSettings(): CodeMcpSettings {
    return loadCodeMcpSettings(this.settingsPath);
  }

  async request(
    name: "search" | "discover" | "reload_settings" | "execute" | "status",
    args: Record<string, unknown>,
    signal?: AbortSignal,
  ): Promise<Record<string, unknown>> {
    await this.reloadBarrier;
    return this.sidecar.call(name, args, signal);
  }

  async warmup(): Promise<void> {
    await this.request("status", {});
  }

  reload(): Promise<void> {
    const operation = this.reloadBarrier.then(() => this.sidecar.close());
    this.reloadBarrier = operation.catch(() => undefined);
    return operation;
  }

  async shutdown(): Promise<void> {
    await this.reloadBarrier;
    await this.sidecar.close();
  }
}
