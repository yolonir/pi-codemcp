import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { setMcpServerEnabled } from "../src/config.js";
import { summarizeError } from "../src/errors.js";
import { CodeMcpLifecycle } from "../src/lifecycle.js";
import type { SidecarClientOptions } from "../src/mcp-client.js";
import {
  type ServerModalState,
  serverStatesFromStatus,
  showServerManagerModal,
} from "../src/modal.js";
import {
  type CodeMcpSettings,
  saveCodeMcpSettings,
  setEditableSetting,
  setToolEnabled,
} from "../src/settings.js";
import { registerCodeMcpTools } from "../src/tools.js";

export function createCodeMcpExtension(options: SidecarClientOptions = {}) {
  return function codeMcpExtension(pi: ExtensionAPI): void {
    const lifecycle = new CodeMcpLifecycle(options);
    registerCodeMcpTools(pi, lifecycle);

    pi.registerCommand("codemcp", {
      description: "Manage CodeMCP servers, tools, and settings",
      handler: async (_args, ctx) => {
        try {
          const [status, settings] = await Promise.all([
            lifecycle.request("status", {}),
            Promise.resolve(lifecycle.loadSettings()),
          ]);
          const servers = serverStatesFromStatus(status);
          if (ctx.mode !== "tui") {
            if (ctx.hasUI) ctx.ui.notify(formatStatusSummary(servers), "info");
            return;
          }

          await showServerManagerModal(ctx, {
            servers,
            settings,
            onSetServerEnabled: (server, enabled) =>
              setServerEnabledFromManager(lifecycle, server, enabled),
            onDiscover: async (server) =>
              requireServerStatus(
                await lifecycle.request("discover", { server: server.name }),
                server.name,
              ),
            onSetToolEnabled: async (server, tool, enabled) => {
              const updated = setToolEnabled(
                lifecycle.loadSettings(),
                server.name,
                tool.name,
                enabled,
              );
              saveCodeMcpSettings(lifecycle.settingsPath, updated);
              return requireServerStatus(
                await lifecycle.request("reload_settings", {}),
                server.name,
              );
            },
            onSetSetting: async (key, value) => {
              const updated = setEditableSetting(lifecycle.loadSettings(), key, value);
              saveCodeMcpSettings(lifecycle.settingsPath, updated);
              await lifecycle.request("reload_settings", {});
              return updated;
            },
          });
        } catch (error) {
          ctx.ui.notify(summarizeError(error), "error");
        }
      },
    });

    pi.on("session_start", (_event, ctx) => {
      let settings: CodeMcpSettings;
      try {
        settings = lifecycle.loadSettings();
      } catch (error) {
        ctx.ui.notify(`CodeMCP settings failed: ${summarizeError(error)}`, "warning");
        return;
      }
      if (!settings.backgroundWarmup) return;
      void lifecycle.warmup().catch((error: unknown) => {
        ctx.ui.notify(`CodeMCP background warmup failed: ${summarizeError(error)}`, "warning");
      });
    });

    pi.on("session_shutdown", async () => {
      await lifecycle.shutdown();
    });
  };
}

export default createCodeMcpExtension();

export async function setServerEnabledFromManager(
  lifecycle: Pick<CodeMcpLifecycle, "configPath" | "reload" | "request">,
  previous: ServerModalState,
  enabled: boolean,
): Promise<ServerModalState> {
  setMcpServerEnabled(lifecycle.configPath, previous.name, enabled);
  await lifecycle.reload();
  try {
    const status = enabled
      ? await lifecycle.request("discover", { server: previous.name })
      : await lifecycle.request("status", {});
    return requireServerStatus(status, previous.name);
  } catch (error) {
    try {
      const current = requireServerStatus(await lifecycle.request("status", {}), previous.name);
      return { ...current, error: summarizeError(error) };
    } catch {
      throw error;
    }
  }
}

function requireServerStatus(
  status: Record<string, unknown>,
  serverName: string,
): ServerModalState {
  const server = serverStatesFromStatus(status).find((candidate) => candidate.name === serverName);
  if (!server) throw new Error(`CodeMCP returned no status for ${serverName}`);
  return server;
}

function formatStatusSummary(servers: ServerModalState[]): string {
  const enabled = servers.filter((server) => server.enabled).length;
  const tools = servers.reduce((total, server) => total + server.toolCount, 0);
  return `CodeMCP: ${enabled}/${servers.length} servers · ${tools} enabled tools`;
}
