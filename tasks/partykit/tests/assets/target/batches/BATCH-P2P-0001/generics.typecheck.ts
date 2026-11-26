import {
  partyserverMiddleware,
  type HonoPartyServerOptions
} from "hono-party";

type Bindings = { Bindings: { JWT_SECRET: string } };

partyserverMiddleware<Bindings>({
  options: {
    onBeforeConnect: (_request, _lobby, context) => {
      const secret: string = context.env.JWT_SECRET;
      void secret;
    },
    onBeforeRequest: (_request, _lobby, context) => {
      const secret: string = context.env.JWT_SECRET;
      void secret;
    }
  }
});

const options: HonoPartyServerOptions<Bindings> = {
  onBeforeRequest: (_request, _lobby, context) => {
    // @ts-expect-error the configured bindings do not contain MISSING_BINDING
    context.env.MISSING_BINDING;
  }
};

partyserverMiddleware<Bindings>({ options });

partyserverMiddleware<{ Bindings: { OTHER: string } }>({
  options: {
    onBeforeRequest: (_request, _lobby, context) => {
      // @ts-expect-error JWT_SECRET is not part of this bindings type
      context.env.JWT_SECRET;
    }
  }
});
