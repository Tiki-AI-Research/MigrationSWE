import { readFile } from "node:fs/promises";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { Elysia } from "elysia";

const entry = process.env.HONO_PARTY_ENTRY ?? "hono-party";
const moduleSpecifier = entry.startsWith("/") || entry.startsWith(".")
  ? pathToFileURL(resolve(entry)).href
  : entry;

const mod = await import(moduleSpecifier);
const repoRoot = process.env.HONO_PARTY_REPO ?? process.cwd();

export const subject = {
  name: "target",
  framework: "elysia",
  mod,
  downstream: [],
  create({ options, onError } = {}) {
    this.downstream = [];
    const app = new Elysia().use(mod.partyserverMiddleware({ options, onError }));
    app.get("/parties/chat/room", () => {
      this.downstream.push("/parties/chat/room");
      return "next";
    });
    app.get("/party/chat/room", () => {
      this.downstream.push("/party/chat/room");
      return "party-next";
    });
    app.get("/other", () => {
      this.downstream.push("/other");
      return "other";
    });
    app.get("/health", () => {
      this.downstream.push("/health");
      return "ok";
    });
    return app;
  },
  async handle(app, request) {
    return app.handle(request);
  },
  async registerZeroArgumentPlugin(plugin) {
    const app = new Elysia().use(plugin).get("/health", () => "ok");
    const response = await app.handle(new Request("http://localhost/health"));
    return response.status === 200 && (await response.text()) === "ok";
  },
  async packageJson() {
    return JSON.parse(
      await readFile(join(repoRoot, "packages/hono-party/package.json"), "utf8")
    );
  }
};
