import { expect, test } from "bun:test";
import { searchMcpRegistry } from "../../src/server-catalog.js";

test("registry search maps supported remote and package transports into editable configs", async () => {
  let requested = "";
  const fetcher = async (input: string | URL) => {
    requested = String(input);
    return new Response(
      JSON.stringify({
        servers: [
          {
            server: {
              name: "io.github.example/filesystem-mcp",
              title: "Filesystem",
              description: "Read files",
              remotes: [
                {
                  type: "streamable-http",
                  url: "https://example.test/{tenant}/mcp",
                  headers: [{ name: "Authorization", value: "Bearer {api_key}", isSecret: true }],
                },
                { type: "sse", url: "https://example.test/sse" },
              ],
              packages: [
                {
                  registryType: "npm",
                  identifier: "@example/filesystem-mcp",
                  version: "1.2.3",
                  transport: { type: "stdio" },
                  runtimeArguments: [{ type: "positional", value: "-y" }],
                  packageArguments: [{ type: "positional", valueHint: "root", isRequired: true }],
                  environmentVariables: [{ name: "API_TOKEN", isSecret: true }],
                },
                {
                  registryType: "mcpb",
                  identifier: "https://example.test/server.mcpb",
                  transport: { type: "stdio" },
                },
              ],
            },
          },
        ],
        metadata: { count: 1 },
      }),
      { status: 200, headers: { "content-type": "application/json" } },
    );
  };

  const candidates = await searchMcpRegistry("filesystem", { fetcher });

  expect(requested).toContain("search=filesystem");
  expect(requested).toContain("version=latest");
  expect(candidates).toHaveLength(2);
  expect(candidates[0]).toMatchObject({
    name: "filesystem-mcp",
    title: "Filesystem",
    source: "HTTP example.test",
    config: {
      url: `https://example.test/\${TENANT}/mcp`,
      headers: { Authorization: `Bearer \${API_KEY}` },
      description: "Read files",
    },
  });
  expect(candidates[1]).toMatchObject({
    source: "npm @example/filesystem-mcp@1.2.3",
    config: {
      command: "npx",
      args: ["-y", "@example/filesystem-mcp@1.2.3", "<root>"],
      env: { API_TOKEN: `\${API_TOKEN}` },
      description: "Read files",
    },
  });
});

test("registry search can be cancelled while the registry is slow", async () => {
  const controller = new AbortController();
  const pending = searchMcpRegistry("figma", {
    signal: controller.signal,
    fetcher: (_input, init) =>
      new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener("abort", () => reject(init.signal?.reason), { once: true });
      }),
  });
  controller.abort();
  await expect(pending).rejects.toThrow();
});

test("registry search reports invalid responses and HTTP failures", async () => {
  await expect(
    searchMcpRegistry("x", { fetcher: async () => new Response("no", { status: 503 }) }),
  ).rejects.toThrow("503");
  await expect(
    searchMcpRegistry("x", {
      fetcher: async () =>
        new Response("{}", {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
    }),
  ).rejects.toThrow("invalid response");
});
