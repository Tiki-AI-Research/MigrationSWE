# Upgrade react-native-view-shot from React Native 0.84.1 to 0.85.0

## Background

`react-native-view-shot` is a React Native library that captures a view or screen to an image. It integrates with React Native on four layers: the JS/TS bridge in `src/`, an Android Java TurboModule, an iOS Objective-C++ module, and a Windows C# module.

The library is currently built and tested against React Native `0.84.1`. The root `devDependencies` pin `react-native` at `^0.84.1`, and `package-lock.json` resolves `node_modules/react-native` to `0.84.1`. The library `peerDependencies` allow `react-native >= 0.76.0`. Repository installs use `--legacy-peer-deps` because the Windows example depends on `react-native-windows@0.76.17`.

## Goal

- Upgrade the React Native framework that the library is built and tested against from `0.84.1` to `0.85.0`.
- React Native `0.85.0` MUST provide the view-capture responsibilities within scope: the JS/TS bridge plus the Android, iOS, and Windows native modules for `captureRef`, `captureScreen`, and `releaseCapture`.
- Raising the declared React Native version alone MUST NOT be treated as completing the upgrade; the scoped implementations MUST be adapted to the `0.85.0` APIs.
- The library MUST keep exposing the same public API and native module contract described below.
- The example applications (`example`, `example-expo`, `example-windows`) and the html2canvas web implementation are outside this migration.

## Public contract to preserve

- The TurboModule specification `NativeRNViewShotSpec` MUST keep the module name constant `NAME=RNViewShot` and these methods: `releaseCapture(String)`, `captureRef(Double target, ReadableMap withOptions, Promise promise)`, and `captureScreen(ReadableMap options, Promise promise)`.
- The public JS API MUST keep `captureRef(ref, options)`, `captureScreen(options)`, `releaseCapture(uri)`, the `ViewShot` component, and its instance `capture()` method.
- `CaptureOptions` MUST keep the fields `fileName`, `width`, `height`, `format` (`jpg | png | webp | webm | raw`), `quality`, and `result` (`tmpfile | base64 | data-uri | zip-base64`).
- The data-uri result MUST serialize as `data:image/<subtype>;base64,<base64>`, with `jpg` mapped to `jpeg`.
- The Windows C# native contract MUST keep `releaseCapture(string uri)` and `Task<string> captureRef(int tag, JSValue options)`.

## Behavior requirements

- After a successful capture, `captureRef` and `captureScreen` MUST resolve with a temporary file path for `result: "tmpfile"`, a raw base64 string for `result: "base64"`, and a data-uri string for `result: "data-uri"`.
- Format handling MUST keep its platform whitelists. The JS option validation MUST accept `png` and `jpg` everywhere, plus `webp`, `webm`, and `raw` on Android. The Windows C# helper MUST accept only `png`, `jpg`, and `jpeg`, and MUST report `webm`, `gif`, the empty string, and `null` as unsupported.
- `webm` MUST remain accepted as a deprecated alias for `webp`.
- A failed capture MUST reject the capture promise. On Android and Windows the rejection error code MUST be `E_UNABLE_TO_SNAPSHOT`. On Windows, a capture whose encoded byte size exceeds `int.MaxValue` MUST throw `InvalidOperationException`.
- A `tmpfile` capture MUST be written inside the application LocalFolder. `releaseCapture` MUST delete only relative paths inside that directory; an empty URI or an absolute or escaping path MUST return without deleting anything.
- The `mount`, `update`, and `continuous` capture modes MUST each trigger their corresponding capture, and `continuous` MUST keep using a `requestAnimationFrame` loop.

## Constraints

1. The migration MUST NOT remove the required public capabilities or change their contracts beyond what the goal states.
2. Production code within the migration scope MUST be adapted to the React Native `0.85.0` APIs and MUST NOT depend on interfaces removed or changed in `0.85.0`.
3. The migrated capture responsibilities MUST be provided by React Native `0.85.0`. The library MUST NOT keep fulfilling them through the previous `0.84.1` runtime, a nested copy of it, or dynamic loading of it.
4. Changes MUST be limited to the library production sources within the migration scope (`src/index.tsx`, `src/RNViewShot.ts`, `src/specs/NativeRNViewShot.ts`, the Android modules under `android/src/main` and `android/src/paper`, `ios/RNViewShot.mm`, and the Windows C# modules), dependency configuration (`package.json` and the lockfile), and supporting build assets.
5. The example applications (`example`, `example-expo`, `example-windows`) and the web/html2canvas implementation are outside the migration scope; their pinned React Native and `react-native-windows` versions MAY remain unchanged.
6. The public test suites (`src/__tests__/`, `android/src/test/`, and `windows/RNViewShot.Tests/`) MUST be preserved. You MUST NOT delete tests, remove or weaken assertions, skip checks, or hardcode inputs to fake compliance.
7. The repository working directory is `/workspace/repo`.
8. Your work is collected as a patch of this repository against its baseline commit. Every modification MUST be present in the working tree under `/workspace/repo`.

## Validation

From `/workspace/repo`, run:

- `npm test` — runs the Jest unit tests under `src/__tests__/`.
- `npm run build` — compiles the TypeScript sources to `lib/`.
- `npm run type-check` — type-checks the sources without emitting.
- `npm run lint:ci` — runs ESLint with zero warnings allowed.

These public checks cover the JS/TS bridge and option handling. They do not by themselves prove the Android, iOS, or Windows native modules or the on-device capture behavior; those are exercised by the example app Detox suites, which require their own device setup.

The migration baseline of this task is base commit 68880f87a26e56a36965290109e044b598dec141.
