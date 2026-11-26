import { createRequire } from 'node:module';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

const repoRoot = process.env.EVAL_REPO_ROOT || '/workspace/repo';
const WebVRPolyfill = (
  await import(pathToFileURL(path.join(repoRoot, 'src/webvr-polyfill.js')).href)
).default;

const require = createRequire(path.join(repoRoot, 'package.json'));
const { JSDOM } = require('jsdom');
const localStorage = require('localStorage');

const DEFAULT = { DPDB_URL: null };
const desktopUA = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/51.0 Safari/537.36';
const iosUA = 'Mozilla/5.0 (iPhone; CPU iPhone OS 11_0 like Mac OS X) AppleWebKit/604.1 Mobile/15A356 Safari/604.1';
const androidUA = 'Mozilla/5.0 (Linux; Android 4.0.4; Galaxy Nexus) AppleWebKit/535.19 Mobile Safari/535.19';

function setUA(ua) {
  Object.defineProperty(global.navigator, 'userAgent', {
    value: ua,
    configurable: true,
    writable: true,
  });
  Object.defineProperty(global.navigator, 'platform', {
    value: /iPhone/.test(ua) ? 'iPhone' : /Android/.test(ua) ? 'Linux armv8l' : 'Linux x86_64',
    configurable: true,
    writable: true,
  });
}

function setupWindow() {
  const dom = new JSDOM('', { url: 'https://example.test/' });
  global.window = dom.window;
  global.document = dom.window.document;
  global.navigator = dom.window.navigator;
  global.screen = {};
  global.localStorage = localStorage;
  Object.defineProperty(global.window, 'localStorage', {
    value: localStorage,
    configurable: true,
  });
  localStorage.clear();
  setUA(desktopUA);
}

function addNativeNoDisplay() {
  window.navigator.getVRDisplays = () => Promise.resolve([]);
  window.VRDisplay = WebVRPolyfill.VRDisplay;
  window.VRFrameData = WebVRPolyfill.VRFrameData;
}

function addNativeWithDisplay() {
  addNativeNoDisplay();
  const display = new WebVRPolyfill.VRDisplay();
  display.displayName = '1.1 test device';
  window.navigator.getVRDisplays = () => Promise.resolve([display]);
  return display;
}

async function displaysFrom(options = DEFAULT) {
  const polyfill = new WebVRPolyfill(options);
  const result = navigator.getVRDisplays();
  expect(result).toBeInstanceOf(Promise);
  return { polyfill, displays: await result };
}

beforeEach(setupWindow);
afterEach(() => {
  delete global.window;
  delete global.document;
  delete global.navigator;
  delete global.screen;
  delete global.localStorage;
});

describe('Evaluator-owned WebVR behavior', () => {
  it('p2p.globals-enabled', () => {
    new WebVRPolyfill(DEFAULT);
    expect(navigator.getVRDisplays).toBeTypeOf('function');
    expect(window.VRDisplay).toBe(WebVRPolyfill.VRDisplay);
    expect(window.VRFrameData).toBe(WebVRPolyfill.VRFrameData);
    expect(WebVRPolyfill.version).toBe('0.10.12');
  });

  it('p2p.enable-absent-api', () => {
    const polyfill = new WebVRPolyfill(DEFAULT);
    expect(polyfill.enabled).toBe(true);
  });

  it('p2p.disable-desktop-native-optout', () => {
    addNativeWithDisplay();
    const native = navigator.getVRDisplays;
    const polyfill = new WebVRPolyfill({ ...DEFAULT, PROVIDE_MOBILE_VRDISPLAY: false });
    expect(polyfill.enabled).toBe(false);
    expect(navigator.getVRDisplays).toBe(native);
  });

  it('p2p.disable-mobile-native-optout', () => {
    setUA(iosUA);
    addNativeWithDisplay();
    const polyfill = new WebVRPolyfill({ ...DEFAULT, PROVIDE_MOBILE_VRDISPLAY: false });
    expect(polyfill.enabled).toBe(false);
  });

  it('p2p.enable-mobile-native-default', () => {
    setUA(iosUA);
    addNativeWithDisplay();
    const polyfill = new WebVRPolyfill({ ...DEFAULT, PROVIDE_MOBILE_VRDISPLAY: true });
    expect(polyfill.enabled).toBe(true);
  });

  it('p2p.has-native-false', () => {
    expect(new WebVRPolyfill(DEFAULT).hasNative).toBe(false);
  });

  it('p2p.has-native-true', () => {
    addNativeWithDisplay();
    expect(new WebVRPolyfill(DEFAULT).hasNative).toBe(true);
  });

  it('p2p.desktop-no-api', async () => {
    const { displays } = await displaysFrom();
    expect(displays).toEqual([]);
  });

  it('p2p.desktop-native-empty', async () => {
    addNativeNoDisplay();
    const { displays } = await displaysFrom();
    expect(displays).toEqual([]);
  });

  it('p2p.desktop-native-display', async () => {
    const native = addNativeWithDisplay();
    const { displays } = await displaysFrom();
    expect(displays).toEqual([native]);
    expect(displays[0].displayName).toBe('1.1 test device');
  });

  for (const [platform, ua] of [['ios', iosUA], ['android', androidUA]]) {
    it(`p2p.${platform}-no-api`, async () => {
      setUA(ua);
      const { displays } = await displaysFrom();
      expect(displays).toHaveLength(1);
      expect(displays[0].displayName).toContain('Cardboard');
    });

    it(`p2p.${platform}-native-empty`, async () => {
      setUA(ua);
      addNativeNoDisplay();
      const { displays } = await displaysFrom();
      expect(displays).toHaveLength(1);
      expect(displays[0].displayName).toContain('Cardboard');
    });

    it(`p2p.${platform}-native-empty-optout`, async () => {
      setUA(ua);
      addNativeNoDisplay();
      const { displays } = await displaysFrom({ ...DEFAULT, PROVIDE_MOBILE_VRDISPLAY: false });
      expect(displays).toEqual([]);
    });

    it(`p2p.${platform}-native-display`, async () => {
      setUA(ua);
      const native = addNativeWithDisplay();
      const { displays } = await displaysFrom();
      expect(displays).toEqual([native]);
      expect(displays[0].displayName).toBe('1.1 test device');
    });
  }
});
