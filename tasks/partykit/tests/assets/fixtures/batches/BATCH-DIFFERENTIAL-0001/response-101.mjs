export function installResponse101() {
  const OriginalResponse = globalThis.Response;

  class WorkerdResponse extends OriginalResponse {
    constructor(body, init) {
      if (init && init.status === 101) {
        super(null, { status: 200 });
        Object.defineProperty(this, "status", { value: 101 });
        if (init.webSocket) {
          Object.defineProperty(this, "webSocket", { value: init.webSocket });
        }
      } else {
        super(body, init);
      }
    }
  }

  globalThis.Response = WorkerdResponse;

  return () => {
    globalThis.Response = OriginalResponse;
  };
}
