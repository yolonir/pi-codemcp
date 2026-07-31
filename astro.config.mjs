// @ts-check
import starlight from "@astrojs/starlight";
import { defineConfig } from "astro/config";

export default defineConfig({
  site: "https://yolonir.github.io",
  base: "/pi-codemcp",
  srcDir: "./website/src",
  publicDir: "./website/public",
  outDir: "./website/dist",
  integrations: [
    starlight({
      title: "pi-codemcp",
      description: "Typed, sandboxed Code Mode for every MCP server configured in Pi.",
      favicon: "/favicon.svg",
      customCss: ["./website/src/styles/custom.css"],
      components: {
        Head: "./website/src/components/Head.astro",
      },
      editLink: {
        baseUrl: "https://github.com/yolonir/pi-codemcp/edit/main/",
      },
      social: [
        {
          icon: "github",
          label: "GitHub",
          href: "https://github.com/yolonir/pi-codemcp",
        },
      ],
      sidebar: [
        {
          label: "Start here",
          items: [{ label: "Getting started", slug: "getting-started" }],
        },
        {
          label: "Guides",
          items: [
            { label: "Code Mode workflow", slug: "guides/code-mode" },
            { label: "Saved chains", slug: "guides/saved-chains" },
          ],
        },
        {
          label: "Reference",
          items: [
            { label: "Pi tools", slug: "reference/tools" },
            { label: "Configuration", slug: "reference/configuration" },
            { label: "Security and limits", slug: "reference/security" },
          ],
        },
      ],
    }),
  ],
});
