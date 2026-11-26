// Evaluator-owned observable-behavior probe.
//
// Loads a UMD artifact the same way a browser script tag would and records the
// externally observable contract: the public constructor shape, the public
// prototype surface, and the documented native/fallback display behavior for
// desktop, iOS, and Android. The recorded fingerprint is compared against the
// frozen source baseline so that "preserved contract" is judged by observed
// behavior, not by whether a symbol name appears in the text.
import fs from 'node:fs';
import { createRequire } from 'node:module';
import path from 'node:path';

const repoRoot = process.env.EVAL_REPO_ROOT || '/workspace/repo';
const require = createRequire(path.join(repoRoot, 'package.json'));
const { JSDOM } = require('jsdom');

const [artifactPath, outputPath] = process.argv.slice(2);
const content = fs.readFileSync(artifactPath, 'utf8');

const DESKTOP_UA =
  'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/51.0.2704.103 Safari/537.36';
const IOS_UA =
  'Mozilla/5.0 (iPhone; CPU iPhone OS 11_0 like Mac OS X) AppleWebKit/604.1.38 (KHTML, like Gecko) Version/11.0 Mobile/15A356 Safari/604.1';
const ANDROID_UA =
  'Mozilla/5.0 (Linux; Android 4.0.4; Galaxy Nexus Build/IMM76B) AppleWebKit/535.19 (KHTML, like Gecko) Chrome/18.0.1025.133 Mobile Safari/535.19';

function sortedNames(value) {
  if (!value || !value.prototype) return null;
  return Object.getOwnPropertyNames(value.prototype).sort();
}

function setUA(win, ua) {
  Object.defineProperty(win.navigator, 'userAgent', { value: ua, configurable: true, writable: true });
  Object.defineProperty(win.navigator, 'platform', {
    value: /iPhone/.test(ua) ? 'iPhone' : /Android/.test(ua) ? 'Linux armv8l' : 'Linux x86_64',
    configurable: true,
    writable: true,
  });
}

function addNative(win, withDisplay) {
  const P = win.WebVRPolyfill;
  win.VRDisplay = P.VRDisplay;
  win.VRFrameData = P.VRFrameData;
  if (withDisplay) {
    const display = new P.VRDisplay();
    display.displayName = '1.1 test device';
    win.navigator.getVRDisplays = () => Promise.resolve([display]);
    return;
  }
  win.navigator.getVRDisplays = () => Promise.resolve([]);
}

function newWindow() {
  const dom = new JSDOM('', { runScripts: 'dangerously', url: 'https://example.test/' });
  dom.window.eval(content);
  return dom.window;
}

async function scenario(kind, ua, options) {
  const win = newWindow();
  const P = win.WebVRPolyfill;
  setUA(win, ua);
  if (kind === 'native-empty') addNative(win, false);
  if (kind === 'native-display') addNative(win, true);
  const polyfill = new P(options);
  const displays = await win.navigator.getVRDisplays();
  return {
    enabled: Boolean(polyfill.enabled),
    hasNative: Boolean(polyfill.hasNative),
    count: Array.isArray(displays) ? displays.length : null,
    names: Array.isArray(displays) ? displays.map((item) => String(item.displayName)).sort() : null,
  };
}

const window0 = newWindow();
const P0 = window0.WebVRPolyfill;
const fingerprint = {
  constructor: typeof P0,
  version: P0.version,
  VRDisplay: typeof P0.VRDisplay,
  VRFrameData: typeof P0.VRFrameData,
  polyfillPrototype: sortedNames(P0),
  displayPrototype: sortedNames(P0.VRDisplay),
  frameDataPrototype: sortedNames(P0.VRFrameData),
  behavior: {
    'desktop-no-api': await scenario('no-api', DESKTOP_UA, { DPDB_URL: null }),
    'desktop-native-empty': await scenario('native-empty', DESKTOP_UA, { DPDB_URL: null }),
    'desktop-native-display': await scenario('native-display', DESKTOP_UA, { DPDB_URL: null }),
    'ios-no-api': await scenario('no-api', IOS_UA, { DPDB_URL: null }),
    'ios-native-empty': await scenario('native-empty', IOS_UA, { DPDB_URL: null }),
    'ios-native-empty-optout': await scenario('native-empty', IOS_UA, {
      DPDB_URL: null,
      PROVIDE_MOBILE_VRDISPLAY: false,
    }),
    'ios-native-display': await scenario('native-display', IOS_UA, { DPDB_URL: null }),
    'android-no-api': await scenario('no-api', ANDROID_UA, { DPDB_URL: null }),
    'android-native-empty-optout': await scenario('native-empty', ANDROID_UA, {
      DPDB_URL: null,
      PROVIDE_MOBILE_VRDISPLAY: false,
    }),
    'android-native-display': await scenario('native-display', ANDROID_UA, { DPDB_URL: null }),
  },
};

fs.writeFileSync(outputPath, JSON.stringify(fingerprint, null, 2) + '\n', 'utf8');
