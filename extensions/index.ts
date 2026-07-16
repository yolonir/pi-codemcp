import { DynamicBorder, type ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Container, matchesKey, Text } from "@earendil-works/pi-tui";
import { CodeModeLifecycle } from "../src/lifecycle.js";
import type { SidecarClientOptions } from "../src/mcp-client.js";
import { registerCodeModeTools } from "../src/tools.js";

export function createCodeModeExtension(options: SidecarClientOptions = {}) {
  return function codeModeExtension(pi: ExtensionAPI): void {
    const lifecycle = new CodeModeLifecycle(options);
    registerCodeModeTools(pi, lifecycle);

    pi.registerCommand("codemode", {
      description: "Open Code Mode status",
      handler: async (_args, ctx) => {
        try {
          const status = await lifecycle.request("status", {});
          const upstreams = Array.isArray(status.upstreams)
            ? status.upstreams.filter(isRecord)
            : [];
          if (!ctx.hasUI) {
            ctx.ui.notify(formatStatusSummary(status, upstreams), "info");
            return;
          }

          await ctx.ui.custom<void>((tui, theme, _keybindings, done) => {
            const container = new Container();
            container.addChild(new DynamicBorder((text: string) => theme.fg("borderAccent", text)));
            container.addChild(new Text(theme.fg("accent", theme.bold("Code Mode")), 1, 0));
            container.addChild(
              new Text(
                theme.fg(
                  "muted",
                  `${String(status.tool_count ?? 0)} tools · ${upstreams.length} servers`,
                ),
                1,
                0,
              ),
            );

            for (const upstream of upstreams) {
              const toolCount = Number(upstream.tool_count ?? 0);
              const available = toolCount > 0;
              const marker = available ? theme.fg("success", "●") : theme.fg("warning", "○");
              const metadata = [
                `${toolCount} tools`,
                String(upstream.transport ?? "unknown"),
                ...(upstream.auth ? [String(upstream.auth)] : []),
              ].join(" · ");
              container.addChild(
                new Text(
                  `${marker} ${theme.fg("text", String(upstream.name ?? "unknown"))} ${theme.fg("dim", metadata)}`,
                  1,
                  0,
                ),
              );
            }

            container.addChild(new Text(theme.fg("dim", "esc / enter / q close"), 1, 0));
            container.addChild(new DynamicBorder((text: string) => theme.fg("borderAccent", text)));

            return {
              render: (width: number) => container.render(width),
              invalidate: () => container.invalidate(),
              handleInput: (data: string) => {
                if (matchesKey(data, "escape") || matchesKey(data, "enter") || data === "q") {
                  done(undefined);
                  return;
                }
                tui.requestRender();
              },
            };
          });
        } catch (error) {
          ctx.ui.notify(error instanceof Error ? error.message : String(error), "error");
        }
      },
    });

    pi.on("session_shutdown", async () => {
      await lifecycle.shutdown();
    });
  };
}

export default createCodeModeExtension();

function formatStatusSummary(
  status: Record<string, unknown>,
  upstreams: Record<string, unknown>[],
): string {
  const servers = upstreams
    .map((item) => `${String(item.name)} (${String(item.tool_count ?? 0)})`)
    .join(", ");
  return `Code Mode: ${String(status.tool_count ?? 0)} tools${servers ? ` — ${servers}` : ""}`;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
