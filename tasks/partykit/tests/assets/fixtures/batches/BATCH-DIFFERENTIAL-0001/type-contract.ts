import { partyserverMiddleware } from "hono-party";

type E = { Bindings: { JWT_SECRET: string } };

partyserverMiddleware<E>({
  options: {
    prefix: "/party",
    cors: { origin: "*" },
    jurisdiction: "eu",
    locationHint: "weur",
    props: { room: "x" },
    routingRetry: { maxAttempts: 3 },
    onBeforeConnect: (request, lobby, context) => {
      const secret: string = context.env.JWT_SECRET;
      const url: string = request.url;
      const room: string = lobby.name;
      void secret;
      void url;
      void room;
    },
    onBeforeRequest: (request, lobby, context) => {
      const secret: string = context.env.JWT_SECRET;
      const url: string = request.url;
      const room: string = lobby.name;
      void secret;
      void url;
      void room;
    }
  },
  onError: (error: Error) => {
    void error.message;
  }
});

partyserverMiddleware<{ Bindings: {} }>({
  options: {
    // @ts-expect-error JWT_SECRET is absent from the supplied bindings type
    onBeforeRequest: (_request, _lobby, context) => context.env.JWT_SECRET
  }
});
