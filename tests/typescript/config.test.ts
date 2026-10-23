import { expect, test } from "bun:test";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  addMcpServer,
  parseMcpServerDraft,
  removeMcpServer,
  setMcpServerEnabled,
} from "../../src/config.js";

test("server toggles preserve config shape and existing enable convention", async () => {
  const temporary = await mkdtemp(join(tmpdir(), "pi-codemcp-config-"));
  const configPath = join(temporary, "mcp.json");
  try {
    await writeFile(
      configPath,
      JSON.stringify({
        metadata: { owner: "test" },
        mcpServers: {
          alpha: { command: "alpha", enabled: true, disabled: true },
          beta: { url: "https://example.test/mcp", headers: { "x-test": "yes" } },
        },
      }),
      "utf8",
    );

    setMcpServerEnabled(configPath, "alpha", false);
    setMcpServerEnabled(configPath, "beta", false);

    const updated = JSON.parse(await readFile(configPath, "utf8"));
    expect(updated).toEqual({
      metadata: { owner: "test" },
      mcpServers: {
        alpha: { command: "alpha", enabled: false },
        beta: {
          url: "https://example.test/mcp",
          headers: { "x-test": "yes" },
          disabled: true,
        },
      },
    });
  } finally {
    await rm(temporary, { recursive: true, force: true });
  }
});

test("server drafts accept common pasted config shapes and validate transport fields", () => {
  expect(
    parseMcpServerDraft(
      JSON.stringify({
        mcpServers: {
          docs: {
            url: "https://example.test/mcp",
            headers: { Authorization: `Bearer \${DOCS_TOKEN}` },
          },
        },
      }),
    ),
  ).toEqual({
    name: "docs",
    config: {
      url: "https://example.test/mcp",
      headers: { Authorization: `Bearer \${DOCS_TOKEN}` },
    },
  });
  expect(parseMcpServerDraft('{"name":"local","command":"uvx","args":["local-mcp"]}')).toEqual({
    name: "local",
    config: { command: "uvx", args: ["local-mcp"] },
  });
  expect(() => parseMcpServerDraft('{"bad name":{"command":"x"}}')).toThrow("letters, digits");
  expect(() => parseMcpServerDraft('{"one":{"command":"x"},"two":{"command":"y"}}')).toThrow(
    "exactly one",
  );
  expect(() => parseMcpServerDraft('{"name":"bad","config":{"url":"not a url"}}')).toThrow(
    "valid URL",
  );
  expect(() =>
    parseMcpServerDraft('{"name":"bad","config":{"command":"x","url":"https://x.test"}}'),
  ).toThrow("exactly one");
});

test("servers can be added and removed without replacing unrelated config", async () => {
  const temporary = await mkdtemp(join(tmpdir(), "pi-codemcp-config-"));
  const configPath = join(temporary, "mcp.json");
  try {
    await writeFile(
      configPath,
      JSON.stringify({ metadata: { owner: "test" }, mcpServers: { alpha: { command: "alpha" } } }),
      "utf8",
    );
    addMcpServer(configPath, {
      name: "beta-server",
      config: { url: "https://example.test/mcp", description: "Example" },
    });
    expect(JSON.parse(await readFile(configPath, "utf8"))).toEqual({
      metadata: { owner: "test" },
      mcpServers: {
        alpha: { command: "alpha" },
        "beta-server": { url: "https://example.test/mcp", description: "Example" },
      },
    });
    expect(() =>
      addMcpServer(configPath, { name: "beta_server", config: { command: "beta" } }),
    ).toThrow("already exists");
    removeMcpServer(configPath, "beta-server");
    expect(JSON.parse(await readFile(configPath, "utf8"))).toEqual({
      metadata: { owner: "test" },
      mcpServers: { alpha: { command: "alpha" } },
    });
  } finally {
    await rm(temporary, { recursive: true, force: true });
  }
});

test("adding the first server creates a wrapped config", async () => {
  const temporary = await mkdtemp(join(tmpdir(), "pi-codemcp-config-"));
  const configPath = join(temporary, "mcp.json");
  try {
    addMcpServer(configPath, { name: "alpha", config: { command: "alpha" } });
    expect(JSON.parse(await readFile(configPath, "utf8"))).toEqual({
      mcpServers: { alpha: { command: "alpha" } },
    });
  } finally {
    await rm(temporary, { recursive: true, force: true });
  }
});

test("server toggles support a root-level server map", async () => {
  const temporary = await mkdtemp(join(tmpdir(), "pi-codemcp-config-"));
  const configPath = join(temporary, "mcp.json");
  try {
    await writeFile(configPath, JSON.stringify({ alpha: { command: "alpha" } }), "utf8");

    setMcpServerEnabled(configPath, "alpha", false);

    expect(JSON.parse(await readFile(configPath, "utf8"))).toEqual({
      alpha: { command: "alpha", disabled: true },
    });
  } finally {
    await rm(temporary, { recursive: true, force: true });
  }
});
