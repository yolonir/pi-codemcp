import type { ExtensionCommandContext, Theme } from "@earendil-works/pi-coding-agent";
import {
  Box,
  type Component,
  type Focusable,
  fuzzyFilter,
  Input,
  Key,
  matchesKey,
  Text,
  truncateToWidth,
  visibleWidth,
} from "@earendil-works/pi-tui";
import { summarizeError } from "./errors.js";
import type { CodeMcpSettings, EditableSettingKey, EditableSettingValue } from "./settings.js";

export interface ToolModalState {
  name: string;
  description?: string;
  enabled: boolean;
  busy?: boolean;
}

export interface ServerModalState {
  name: string;
  transport: string;
  auth?: string;
  enabled: boolean;
  connected: boolean;
  discovered: boolean;
  toolCount: number;
  totalToolCount: number;
  tools: ToolModalState[];
  busy?: boolean;
  error?: string;
}

interface Keybindings {
  matches(data: string, id: "tui.select.up" | "tui.select.down" | "tui.select.cancel"): boolean;
}

interface ServerManagerOptions {
  servers: ServerModalState[];
  settings: CodeMcpSettings;
  onSetServerEnabled(server: ServerModalState, enabled: boolean): Promise<ServerModalState>;
  onDiscover(server: ServerModalState): Promise<ServerModalState>;
  onSetToolEnabled(
    server: ServerModalState,
    tool: ToolModalState,
    enabled: boolean,
  ): Promise<ServerModalState>;
  onSetSetting(key: EditableSettingKey, value: EditableSettingValue): Promise<CodeMcpSettings>;
}

interface SettingChoice {
  value: EditableSettingValue;
  label: string;
}

interface SettingDefinition {
  key: EditableSettingKey;
  label: string;
  description: string;
  choices: SettingChoice[];
}

const OVERLAY_OPTIONS = {
  width: "90%",
  minWidth: 72,
  maxHeight: "85%",
} as const;

const SETTING_DEFINITIONS: SettingDefinition[] = [
  {
    key: "backgroundWarmup",
    label: "Background warmup",
    description: "Start the Python sidecar in the background when a Pi session starts.",
    choices: [
      { value: true, label: "on" },
      { value: false, label: "off" },
    ],
  },
  {
    key: "cacheTtlHours",
    label: "Catalog cache TTL",
    description: "Maximum age of a cached upstream tools/list response.",
    choices: [0, 1, 6, 12, 24, 72, 168].map((value) => ({
      value,
      label: value === 0 ? "off" : `${value}h`,
    })),
  },
  {
    key: "executionTimeoutSeconds",
    label: "Execution timeout",
    description: "Maximum wall-clock duration of one sandboxed CodeMCP program.",
    choices: [10, 30, 60, 120, 300].map(secondsChoice),
  },
  {
    key: "toolTimeoutSeconds",
    label: "Per-tool timeout",
    description: "Maximum duration of one upstream MCP tool call.",
    choices: [10, 30, 60, 120, 300].map(secondsChoice),
  },
  {
    key: "maxCalls",
    label: "Maximum MCP calls",
    description: "Maximum upstream calls made by one sandbox execution.",
    choices: [10, 25, 50, 100, 200].map(numberChoice),
  },
  {
    key: "resultLimitKiB",
    label: "Final result limit",
    description:
      "Maximum serialized value returned by sandbox code before it fails with a shape summary.",
    choices: [4, 8, 16, 32, 64, 128].map(kibChoice),
  },
  {
    key: "outputLimitKiB",
    label: "Agent output limit",
    description: "Maximum CodeMCP tool-result text placed into the agent context.",
    choices: [10, 25, 50, 100, 200, 512].map(kibChoice),
  },
  {
    key: "outputLineLimit",
    label: "Agent line limit",
    description: "Maximum CodeMCP tool-result lines placed into the agent context.",
    choices: [500, 1_000, 2_000, 5_000, 10_000].map(numberChoice),
  },
];

export async function showServerManagerModal(
  ctx: ExtensionCommandContext,
  options: ServerManagerOptions,
): Promise<void> {
  if (ctx.mode !== "tui") {
    throw new Error("CodeMCP server manager requires interactive mode");
  }

  await ctx.ui.custom<void>(
    (tui, theme, keybindings, done) =>
      new ServerManagerModal(
        options,
        theme,
        keybindings,
        () => done(undefined),
        () => tui.requestRender(),
      ),
    { overlay: true, overlayOptions: OVERLAY_OPTIONS },
  );
}

export function serverStatesFromStatus(status: Record<string, unknown>): ServerModalState[] {
  if (!Array.isArray(status.upstreams)) return [];
  return status.upstreams.flatMap((value) => {
    if (!isRecord(value) || typeof value.name !== "string") return [];
    const tools = parseTools(value.tools);
    const toolCount =
      typeof value.tool_count === "number"
        ? value.tool_count
        : tools.filter((tool) => tool.enabled).length;
    return [
      {
        name: value.name,
        transport: typeof value.transport === "string" ? value.transport : "unknown",
        ...(typeof value.auth === "string" ? { auth: value.auth } : {}),
        enabled: value.enabled !== false,
        connected: value.connected === true,
        discovered: value.discovered === true || tools.length > 0,
        toolCount,
        totalToolCount:
          typeof value.total_tool_count === "number" ? value.total_tool_count : tools.length,
        tools,
      },
    ];
  });
}

class ServerManagerModal implements Component, Focusable {
  private readonly search = new Input();
  private activeTab: "servers" | "settings" = "servers";
  private activePane: "servers" | "tools" = "servers";
  private selectedServerIndex = 0;
  private selectedToolIndex = 0;
  private selectedSettingIndex = 0;
  private settingsError: string | undefined;
  private _focused = false;

  constructor(
    private readonly options: ServerManagerOptions,
    private readonly theme: Theme,
    private readonly keybindings: Keybindings,
    private readonly close: () => void,
    private readonly requestRender: () => void,
  ) {}

  get focused(): boolean {
    return this._focused;
  }

  set focused(value: boolean) {
    this._focused = value;
    this.search.focused = value && this.activeTab === "servers";
  }

  render(width: number): string[] {
    const content = new Box(2, 1);
    content.addChild({
      render: (contentWidth: number) => [this.renderHeader(contentWidth)],
      invalidate: () => {},
    });
    content.addChild({
      render: (bodyWidth: number) =>
        this.activeTab === "servers"
          ? this.renderServers(bodyWidth)
          : this.renderSettings(bodyWidth),
      invalidate: () => this.search.invalidate(),
    });
    content.addChild(new Text(this.theme.fg("dim", this.footer()), 0, 0));
    return renderRoundedFrame(content, width, this.theme);
  }

  invalidate(): void {
    this.search.invalidate();
  }

  handleInput(data: string): void {
    if (this.keybindings.matches(data, "tui.select.cancel") || matchesKey(data, Key.escape)) {
      if (this.activeTab === "servers" && this.search.getValue()) {
        this.search.setValue("");
        this.resetSelections();
        this.requestRender();
        return;
      }
      this.close();
      return;
    }
    if (matchesKey(data, Key.tab)) {
      this.activeTab = this.activeTab === "servers" ? "settings" : "servers";
      this.search.setValue("");
      this.search.focused = this._focused && this.activeTab === "servers";
      this.requestRender();
      return;
    }
    if (this.activeTab === "settings") {
      this.handleSettingsInput(data);
      this.requestRender();
      return;
    }
    this.handleServerInput(data);
    this.requestRender();
  }

  private handleServerInput(data: string): void {
    if (data === "D") {
      this.discoverSelected();
      return;
    }
    if (matchesKey(data, Key.left) || matchesKey(data, Key.right)) {
      this.activePane = this.activePane === "servers" ? "tools" : "servers";
      this.search.setValue("");
      this.selectedToolIndex = 0;
      return;
    }
    if (this.keybindings.matches(data, "tui.select.up") || matchesKey(data, Key.up)) {
      this.moveSelection(-1);
      return;
    }
    if (this.keybindings.matches(data, "tui.select.down") || matchesKey(data, Key.down)) {
      this.moveSelection(1);
      return;
    }
    if (matchesKey(data, Key.enter) || data === " ") {
      if (this.activePane === "servers") this.toggleSelectedServer();
      else this.toggleSelectedTool();
      return;
    }
    const sanitized = data.replace(/ /g, "");
    if (sanitized) {
      this.search.handleInput(sanitized);
      this.resetSelections();
    }
  }

  private handleSettingsInput(data: string): void {
    if (this.keybindings.matches(data, "tui.select.up") || matchesKey(data, Key.up)) {
      this.selectedSettingIndex = cycleIndex(
        this.selectedSettingIndex,
        -1,
        SETTING_DEFINITIONS.length,
      );
      return;
    }
    if (this.keybindings.matches(data, "tui.select.down") || matchesKey(data, Key.down)) {
      this.selectedSettingIndex = cycleIndex(
        this.selectedSettingIndex,
        1,
        SETTING_DEFINITIONS.length,
      );
      return;
    }
    if (matchesKey(data, Key.left)) this.cycleSelectedSetting(-1);
    else if (matchesKey(data, Key.right) || matchesKey(data, Key.enter) || data === " ") {
      this.cycleSelectedSetting(1);
    }
  }

  private renderHeader(width: number): string {
    const servers =
      this.activeTab === "servers"
        ? this.theme.fg("accent", this.theme.bold("[Servers]"))
        : this.theme.fg("muted", "Servers");
    const settings =
      this.activeTab === "settings"
        ? this.theme.fg("accent", this.theme.bold("[Settings]"))
        : this.theme.fg("muted", "Settings");
    const tabs = `${servers}  ${settings}`;
    const title = this.theme.fg("dim", this.theme.bold("CodeMCP"));
    const gap = " ".repeat(Math.max(1, width - visibleWidth(tabs) - visibleWidth(title)));
    return truncateToWidth(`${tabs}${gap}${title}`, width);
  }

  private renderServers(width: number): string[] {
    const filterLabel = this.activePane === "servers" ? "Filter servers" : "Filter tools";
    const lines = [this.theme.fg("dim", filterLabel), ...this.search.render(width), ""];
    const splitHeight = Math.max(1, modalBodyRows() - lines.length);
    const leftWidth = Math.min(36, Math.max(24, Math.floor(width * 0.32)));
    const rightWidth = Math.max(1, width - leftWidth - 3);
    const left = this.renderServerList(leftWidth, splitHeight);
    const right = this.renderServerDetails(rightWidth, splitHeight);
    for (let index = 0; index < splitHeight; index += 1) {
      lines.push(
        `${padLine(left[index] ?? "", leftWidth)} ${this.theme.fg("dim", "│")} ${truncateToWidth(right[index] ?? "", rightWidth)}`,
      );
    }
    return lines;
  }

  private renderServerList(width: number, height: number): string[] {
    const servers = this.filteredServers();
    const lines = [this.theme.fg("dim", this.theme.bold("SERVERS"))];
    if (servers.length === 0) return [...lines, this.theme.fg("warning", "No matching servers")];
    this.selectedServerIndex = clampIndex(this.selectedServerIndex, servers.length);
    const visible = visibleWindow(servers, this.selectedServerIndex, height - 1);
    for (const server of visible) {
      const selected =
        this.activePane === "servers" && servers.indexOf(server) === this.selectedServerIndex;
      const prefix = selected ? this.theme.fg("accent", "→") : " ";
      const status = serverIcon(server, this.theme);
      const count = server.discovered ? `${server.toolCount}/${server.totalToolCount}` : "—";
      const reserved = visibleWidth(prefix) + visibleWidth(status) + visibleWidth(count) + 4;
      const name = truncateToWidth(server.name, Math.max(4, width - reserved), "…");
      const gap = " ".repeat(Math.max(1, width - reserved - visibleWidth(name) + 1));
      lines.push(
        truncateToWidth(
          `${prefix} ${status} ${selected ? this.theme.fg("accent", name) : name}${gap}${this.theme.fg("muted", count)}`,
          width,
        ),
      );
    }
    return lines;
  }

  private renderServerDetails(width: number, height: number): string[] {
    const server = this.selectedServer();
    if (!server) return [this.theme.fg("muted", "Select a server")];
    const lines = [
      this.theme.fg("accent", this.theme.bold(server.name)),
      this.theme.fg(
        "muted",
        `${serverStatus(server)} · ${server.toolCount}/${server.totalToolCount} tools enabled`,
      ),
      this.theme.fg("dim", `${server.transport}${server.auth ? ` · ${server.auth}` : ""}`),
      server.busy
        ? this.theme.fg("warning", "Working…")
        : `${this.theme.fg("accent", "[D]")} Discover tools  ${this.theme.fg("accent", "[Space]")} ${server.enabled ? "Disable server" : "Enable server"}`,
      ...(server.error ? [this.theme.fg("warning", `Error: ${server.error}`)] : []),
      this.theme.fg("dim", "─".repeat(Math.max(1, width))),
      this.theme.fg("dim", this.theme.bold("TOOLS")),
    ];
    if (!server.discovered) {
      lines.push(this.theme.fg("muted", "No catalog yet. Press D to discover tools."));
      return lines;
    }
    const tools = this.filteredTools(server);
    if (tools.length === 0) {
      lines.push(this.theme.fg("warning", "No matching tools"));
      return lines;
    }
    this.selectedToolIndex = clampIndex(this.selectedToolIndex, tools.length);
    const selectedTool = tools[this.selectedToolIndex];
    const available = Math.max(1, height - lines.length);
    if (selectedTool && width >= 72 && available >= 6) {
      const cardWidth = Math.min(42, Math.max(28, Math.floor(width * 0.42)));
      const gapWidth = 2;
      const listWidth = Math.max(1, width - cardWidth - gapWidth);
      const list = this.renderToolList(tools, listWidth, available);
      const cardHeight = Math.min(available, 12);
      const card = renderToolCard(selectedTool, cardWidth, cardHeight, this.theme);
      for (let index = 0; index < available; index += 1) {
        lines.push(
          `${padLine(list[index] ?? "", listWidth)}${" ".repeat(gapWidth)}${card[index] ?? ""}`,
        );
      }
      return lines;
    }

    const cardHeight =
      selectedTool && available >= 5
        ? Math.min(9, available - 1, Math.max(4, Math.floor(available * 0.35)))
        : 0;
    const listHeight = available - cardHeight;
    const list = listHeight > 0 ? this.renderToolList(tools, width, listHeight) : [];
    lines.push(...list, ...Array.from({ length: listHeight - list.length }, () => ""));
    if (selectedTool && cardHeight >= 4) {
      lines.push(...renderToolCard(selectedTool, width, cardHeight, this.theme));
    }
    return lines;
  }

  private renderToolList(tools: ToolModalState[], width: number, height: number): string[] {
    const lines: string[] = [];
    for (const tool of visibleWindow(tools, this.selectedToolIndex, height)) {
      const selected =
        this.activePane === "tools" && tools.indexOf(tool) === this.selectedToolIndex;
      const prefix = selected ? this.theme.fg("accent", "→") : " ";
      const state = tool.busy
        ? this.theme.fg("warning", "…")
        : tool.enabled
          ? this.theme.fg("success", "✓")
          : this.theme.fg("dim", "○");
      lines.push(
        truncateToWidth(
          `${prefix} ${state} ${selected ? this.theme.fg("accent", tool.name) : tool.name}`,
          width,
        ),
      );
    }
    return lines;
  }

  private renderSettings(width: number): string[] {
    const splitHeight = Math.max(1, modalBodyRows());
    const leftWidth = Math.min(38, Math.max(28, Math.floor(width * 0.42)));
    const rightWidth = Math.max(1, width - leftWidth - 3);
    const left = [this.theme.fg("dim", this.theme.bold("SETTINGS"))];
    for (const [index, definition] of SETTING_DEFINITIONS.entries()) {
      const selected = index === this.selectedSettingIndex;
      const prefix = selected ? this.theme.fg("accent", "→") : " ";
      const value = settingLabel(definition, this.options.settings[definition.key]);
      const reserved = visibleWidth(prefix) + visibleWidth(value) + 3;
      const label = truncateToWidth(definition.label, Math.max(4, leftWidth - reserved), "…");
      const gap = " ".repeat(Math.max(1, leftWidth - reserved - visibleWidth(label) + 1));
      left.push(
        truncateToWidth(
          `${prefix} ${selected ? this.theme.fg("accent", label) : label}${gap}${this.theme.fg("muted", value)}`,
          leftWidth,
        ),
      );
    }
    const definition = SETTING_DEFINITIONS[this.selectedSettingIndex];
    const right = definition
      ? [
          this.theme.fg("accent", this.theme.bold(definition.label)),
          this.theme.fg("muted", settingLabel(definition, this.options.settings[definition.key])),
          "",
          ...wrapPlainText(definition.description, rightWidth).map((line) =>
            this.theme.fg("muted", line),
          ),
          "",
          this.theme.fg("dim", "←/→ change · Enter next"),
          ...(this.settingsError
            ? ["", this.theme.fg("warning", `Error: ${this.settingsError}`)]
            : []),
        ]
      : [];
    const lines: string[] = [];
    for (let index = 0; index < splitHeight; index += 1) {
      lines.push(
        `${padLine(left[index] ?? "", leftWidth)} ${this.theme.fg("dim", "│")} ${truncateToWidth(right[index] ?? "", rightWidth)}`,
      );
    }
    return lines;
  }

  private footer(): string {
    if (this.activeTab === "settings") {
      return "tab servers · ↑/↓ navigate · ←/→/enter change · esc close";
    }
    return "tab settings · ←/→ pane · ↑/↓ navigate · space toggle · esc close";
  }

  private moveSelection(direction: -1 | 1): void {
    if (this.activePane === "servers") {
      this.selectedServerIndex = cycleIndex(
        this.selectedServerIndex,
        direction,
        this.filteredServers().length,
      );
      this.selectedToolIndex = 0;
      return;
    }
    const server = this.selectedServer();
    this.selectedToolIndex = cycleIndex(
      this.selectedToolIndex,
      direction,
      server ? this.filteredTools(server).length : 0,
    );
  }

  private toggleSelectedServer(): void {
    const server = this.selectedServer();
    if (!server || server.busy) return;
    const enabled = !server.enabled;
    server.busy = true;
    delete server.error;
    void this.options
      .onSetServerEnabled(server, enabled)
      .then((updated) => applyServerUpdate(server, updated))
      .catch((error: unknown) => {
        server.error = summarizeError(error);
      })
      .finally(() => {
        server.busy = false;
        this.requestRender();
      });
  }

  private discoverSelected(): void {
    const server = this.selectedServer();
    if (!server || server.busy) return;
    if (!server.enabled) {
      server.error = "Enable this server to discover its tools";
      return;
    }
    server.busy = true;
    delete server.error;
    this.requestRender();
    void this.options
      .onDiscover(server)
      .then((updated) => applyServerUpdate(server, updated))
      .catch((error: unknown) => {
        server.error = summarizeError(error);
      })
      .finally(() => {
        server.busy = false;
        this.requestRender();
      });
  }

  private toggleSelectedTool(): void {
    const server = this.selectedServer();
    if (!server || server.busy) return;
    const tools = this.filteredTools(server);
    const tool = tools[this.selectedToolIndex];
    if (!tool || tool.busy) return;
    tool.busy = true;
    void this.options
      .onSetToolEnabled(server, tool, !tool.enabled)
      .then((updated) => applyServerUpdate(server, updated))
      .catch((error: unknown) => {
        server.error = summarizeError(error);
      })
      .finally(() => {
        tool.busy = false;
        this.requestRender();
      });
  }

  private cycleSelectedSetting(direction: -1 | 1): void {
    const definition = SETTING_DEFINITIONS[this.selectedSettingIndex];
    if (!definition) return;
    const current = this.options.settings[definition.key];
    const currentIndex = Math.max(
      0,
      definition.choices.findIndex((choice) => choice.value === current),
    );
    const choice =
      definition.choices[cycleIndex(currentIndex, direction, definition.choices.length)];
    if (!choice) return;
    this.settingsError = undefined;
    void this.options
      .onSetSetting(definition.key, choice.value)
      .then((settings) => {
        this.options.settings = settings;
      })
      .catch((error: unknown) => {
        this.settingsError = summarizeError(error);
      })
      .finally(() => this.requestRender());
  }

  private filteredServers(): ServerModalState[] {
    const query = this.activePane === "servers" ? this.search.getValue().trim() : "";
    return query
      ? fuzzyFilter(this.options.servers, query, (server) =>
          [server.name, serverStatus(server)].join(" "),
        )
      : this.options.servers;
  }

  private filteredTools(server: ServerModalState): ToolModalState[] {
    const query = this.activePane === "tools" ? this.search.getValue().trim() : "";
    return query
      ? fuzzyFilter(server.tools, query, (tool) =>
          [tool.name, tool.description].filter(Boolean).join(" "),
        )
      : server.tools;
  }

  private selectedServer(): ServerModalState | undefined {
    const servers = this.filteredServers();
    this.selectedServerIndex = clampIndex(this.selectedServerIndex, servers.length);
    return servers[this.selectedServerIndex];
  }

  private resetSelections(): void {
    this.selectedServerIndex = 0;
    this.selectedToolIndex = 0;
  }
}

function parseTools(value: unknown): ToolModalState[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((tool) => {
    if (!isRecord(tool) || typeof tool.name !== "string") return [];
    return [
      {
        name: tool.name,
        enabled: tool.enabled !== false,
        ...(typeof tool.description === "string" ? { description: tool.description } : {}),
      },
    ];
  });
}

function applyServerUpdate(target: ServerModalState, updated: ServerModalState): void {
  target.transport = updated.transport;
  if (updated.auth === undefined) delete target.auth;
  else target.auth = updated.auth;
  target.enabled = updated.enabled;
  target.connected = updated.connected;
  target.discovered = updated.discovered;
  target.toolCount = updated.toolCount;
  target.totalToolCount = updated.totalToolCount;
  target.tools = updated.tools;
  if (updated.error === undefined) delete target.error;
  else target.error = updated.error;
}

function serverStatus(server: ServerModalState): string {
  if (server.busy) return "working";
  if (!server.enabled) return "disabled";
  if (server.connected) return "connected";
  if (server.discovered) return "ready";
  return "not discovered";
}

function serverIcon(server: ServerModalState, theme: Theme): string {
  if (server.busy) return theme.fg("warning", "…");
  if (!server.enabled) return theme.fg("dim", "○");
  if (server.discovered) return theme.fg("success", "●");
  return theme.fg("warning", "◌");
}

function settingLabel(definition: SettingDefinition, value: EditableSettingValue): string {
  return definition.choices.find((choice) => choice.value === value)?.label ?? String(value);
}

function secondsChoice(value: number): SettingChoice {
  return { value, label: `${value}s` };
}

function numberChoice(value: number): SettingChoice {
  return { value, label: value.toLocaleString("en-US") };
}

function kibChoice(value: number): SettingChoice {
  return { value, label: `${value} KiB` };
}

function cycleIndex(index: number, direction: -1 | 1, length: number): number {
  return length === 0 ? 0 : (index + direction + length) % length;
}

function clampIndex(index: number, length: number): number {
  return Math.max(0, Math.min(index, Math.max(0, length - 1)));
}

function visibleWindow<T>(items: T[], selectedIndex: number, maximum: number): T[] {
  const size = Math.max(1, maximum);
  const start = Math.max(0, Math.min(selectedIndex - Math.floor(size / 2), items.length - size));
  return items.slice(start, start + size);
}

function wrapPlainText(text: string, width: number): string[] {
  const words = text.split(/\s+/);
  const lines: string[] = [];
  let line = "";
  for (const word of words) {
    if (!line) line = word;
    else if (line.length + word.length + 1 <= width) line += ` ${word}`;
    else {
      lines.push(line);
      line = word;
    }
  }
  if (line) lines.push(line);
  return lines;
}

function modalBodyRows(): number {
  const overlayRows = Math.floor((process.stdout.rows ?? 24) * 0.85);
  // Frame, Box padding, header, and footer consume six rows; keep one row as
  // safety because Pi clips overlays at maxHeight before the final border.
  return Math.max(1, overlayRows - 7);
}

function renderToolCard(
  tool: ToolModalState,
  width: number,
  height: number,
  theme: Theme,
): string[] {
  const cardWidth = Math.max(8, width);
  const cardHeight = Math.max(3, height);
  const innerWidth = Math.max(1, cardWidth - 2);
  const title = truncateToWidth(tool.name, Math.max(1, cardWidth - 5), "…");
  const top = [
    theme.fg("dim", "╭─ "),
    theme.fg("accent", theme.bold(title)),
    theme.fg("dim", ` ${"─".repeat(Math.max(0, cardWidth - visibleWidth(title) - 5))}╮`),
  ].join("");
  const bottom = theme.fg("dim", `╰${"─".repeat(Math.max(0, cardWidth - 2))}╯`);
  const description = tool.description ?? "No description provided.";
  const content = wrapPlainText(description, innerWidth).map((line) => theme.fg("muted", line));
  const body: string[] = [];
  for (let index = 0; index < cardHeight - 2; index += 1) {
    body.push(
      `${theme.fg("dim", "│")}${padLine(content[index] ?? "", innerWidth)}${theme.fg("dim", "│")}`,
    );
  }
  return [top, ...body, bottom];
}

function padLine(line: string, width: number): string {
  const truncated = truncateToWidth(line, width, "", true);
  return truncated + " ".repeat(Math.max(0, width - visibleWidth(truncated)));
}

function renderRoundedFrame(content: Component, width: number, theme: Theme): string[] {
  const color = (text: string) => theme.fg("accent", text);
  const innerWidth = Math.max(1, width - 2);
  const body = content.render(innerWidth).map((line) => {
    const truncated = truncateToWidth(line, innerWidth, "", true);
    const padded = truncated + " ".repeat(Math.max(0, innerWidth - visibleWidth(truncated)));
    return `${color("│")}${padded}${color("│")}`;
  });
  return [
    color(`╭${"─".repeat(Math.max(0, width - 2))}╮`),
    ...body,
    color(`╰${"─".repeat(Math.max(0, width - 2))}╯`),
  ];
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
