import { readFileSync, existsSync } from "node:fs";
import { dirname, join } from "node:path";

import { describe, expect, it, vi } from "vitest";

import PartySocket from "../../../packages/partysocket/src/index";

const ROOT = process.cwd();

type Surface = {
  observationId: string;
  packagePath: string;
  subpath: string;
  sourceFile: string;
};

const SURFACES: Surface[] = [
  {
    observationId: "P2P-007",
    packagePath: "packages/y-partyserver/package.json",
    subpath: "./provider",
    sourceFile: "src/provider/index.ts"
  },
  {
    observationId: "P2P-010",
    packagePath: "packages/partysub/package.json",
    subpath: "./server",
    sourceFile: "src/server/index.ts"
  },
  {
    observationId: "P2P-015",
    packagePath: "packages/partysocket/package.json",
    subpath: "./ws",
    sourceFile: "src/ws.ts"
  },
  {
    observationId: "P2P-017",
    packagePath: "packages/partysocket/package.json",
    subpath: "./react",
    sourceFile: "src/react.ts"
  },
  {
    observationId: "P2P-019",
    packagePath: "packages/partysocket/package.json",
    subpath: "./event-target-polyfill",
    sourceFile: "src/event-target-polyfill.ts"
  },
  {
    observationId: "P2P-020",
    packagePath: "packages/partysocket/package.json",
    subpath: "./use-ws",
    sourceFile: "src/use-ws.ts"
  }
];

describe("hono-party P2P package export surfaces", () => {
  for (const surface of SURFACES) {
    it(`${surface.observationId} preserves ${surface.subpath}`, () => {
      const packageJsonPath = join(ROOT, surface.packagePath);
      const packageJson = JSON.parse(readFileSync(packageJsonPath, "utf8")) as {
        exports?: Record<string, unknown>;
      };
      const entry = packageJson.exports?.[surface.subpath];
      const importTarget =
        typeof entry === "string"
          ? entry
          : (entry as { import?: string } | undefined)?.import;
      const sourcePath = join(
        ROOT,
        dirname(surface.packagePath),
        surface.sourceFile
      );

      expect({
        observationId: surface.observationId,
        subpath: surface.subpath,
        declared: entry !== undefined,
        hasImportTarget: typeof importTarget === "string",
        sourcePresent: existsSync(sourcePath)
      }).toEqual({
        observationId: surface.observationId,
        subpath: surface.subpath,
        declared: true,
        hasImportTarget: true,
        sourcePresent: true
      });
    });
  }
});

describe("hono-party P2P executable behavior projections", () => {
  it("P2P-001 passes custom WebSocket constructor", () => {
    const customWS = class extends WebSocket {};
    const socket = new PartySocket({
      host: "example.com",
      room: "my-room",
      WebSocket: customWS,
      startClosed: true
    });

    const projection = {
      created: socket instanceof PartySocket,
      host: socket.host,
      room: socket.room
    };
    socket.close();

    expect(projection).toEqual({
      created: true,
      host: "example.com",
      room: "my-room"
    });
  });

  it("P2P-002 reconnects when host changes", () => {
    const socket = new PartySocket({
      host: "example.com",
      room: "test-room",
      startClosed: true
    });
    const firstHost = socket.host;
    const firstUrl = socket.roomUrl;

    socket.updateProperties({ host: "different.com" });
    const secondHost = socket.host;
    const secondUrl = socket.roomUrl;
    socket.close();

    expect({
      firstHost,
      firstUrl,
      secondHost,
      secondUrl
    }).toEqual({
      firstHost: "example.com",
      firstUrl: "wss://example.com/parties/main/test-room",
      secondHost: "different.com",
      secondUrl: "wss://different.com/parties/main/test-room"
    });
  });

  it("P2P-003 respects startClosed option", () => {
    const socket = new PartySocket({
      host: "example.com",
      room: "test-room",
      startClosed: true
    });
    const readyState = socket.readyState;
    socket.close();

    expect({ readyState, closed: WebSocket.CLOSED }).toEqual({
      readyState: 3,
      closed: 3
    });
  });

  it("P2P-004 omits null and undefined query parameters", async () => {
    const mockFetch = vi.fn().mockResolvedValue(new Response("ok"));

    await PartySocket.fetch({
      host: "example.com",
      room: "my-room",
      query: { foo: "bar", nullParam: null, undefinedParam: undefined },
      fetch: mockFetch
    });

    const calledUrl = String(mockFetch.mock.calls[0][0]);
    expect({
      hasFoo: calledUrl.includes("foo=bar"),
      hasNull: calledUrl.includes("nullParam"),
      hasUndefined: calledUrl.includes("undefinedParam")
    }).toEqual({ hasFoo: true, hasNull: false, hasUndefined: false });
  });

  it("P2P-006 propagates fetch errors", async () => {
    const mockFetch = vi.fn().mockRejectedValue(new Error("Network failure"));
    let message = "";

    try {
      await PartySocket.fetch({
        host: "example.com",
        room: "my-room",
        fetch: mockFetch
      });
    } catch (error) {
      message = (error as Error).message;
    }

    expect({ message }).toEqual({ message: "Network failure" });
  });

  it("P2P-012 works without optional parameters", async () => {
    const mockFetch = vi.fn().mockResolvedValue(new Response("ok"));

    await PartySocket.fetch({
      host: "example.com",
      room: "my-room",
      fetch: mockFetch
    });

    expect({ url: String(mockFetch.mock.calls[0][0]) }).toEqual({
      url: "https://example.com/parties/main/my-room?"
    });
  });
});
