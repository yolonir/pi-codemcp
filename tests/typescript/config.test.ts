import { expect, test } from "bun:test";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { setMcpServerEnabled, setMcpServersEnabled } from "../../src/config.js";

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

    setMcpServersEnabled(configPath, [
      { name: "alpha", enabled: false },
      { name: "beta", enabled: false },
    ]);

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
