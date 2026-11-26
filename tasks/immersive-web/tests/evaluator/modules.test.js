import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { describe, expect, it } from 'vitest';

const repoRoot = process.env.EVAL_REPO_ROOT || '/workspace/repo';
const config = (
  await import(pathToFileURL(path.join(repoRoot, 'src/config.js')).href)
).default;
const { copyArray, extend, isIOS, isMobile, race } = await import(
  pathToFileURL(path.join(repoRoot, 'src/util.js')).href
);

describe('Evaluator-owned frozen module contracts', () => {
  it('p2p.config-defaults', () => {
    expect(config).toEqual({
      ADDITIONAL_VIEWERS: [],
      DEFAULT_VIEWER: '',
      PROVIDE_MOBILE_VRDISPLAY: true,
      MOBILE_WAKE_LOCK: true,
      DEBUG: false,
      DPDB_URL: 'https://dpdb.webvr.rocks/dpdb.json',
      K_FILTER: 0.98,
      PREDICTION_TIME_S: 0.040,
      CARDBOARD_UI_DISABLED: false,
      ROTATE_INSTRUCTIONS_DISABLED: false,
      YAW_ONLY: false,
      BUFFER_SCALE: 0.5,
      DIRTY_SUBMIT_FRAME_BINDINGS: false,
    });
  });

  it('p2p.util-race', async () => {
    await expect(race([Promise.resolve('first'), new Promise(resolve => setTimeout(() => resolve('late'), 5))])).resolves.toBe('first');
    await expect(race([Promise.reject(new Error('rejected'))])).rejects.toThrow('rejected');
    const nativeRace = Promise.race;
    try {
      Promise.race = undefined;
      await expect(race([Promise.resolve('fallback')])).resolves.toBe('fallback');
      await expect(race([Promise.reject(new Error('fallback-rejected'))])).rejects.toThrow('fallback-rejected');
    } finally {
      Promise.race = nativeRace;
    }
  });

  it('p2p.util-user-agent', () => {
    Object.defineProperty(global.navigator, 'platform', { value: 'iPhone', configurable: true });
    Object.defineProperty(global.navigator, 'userAgent', { value: 'iPhone Mobile', configurable: true });
    expect(isIOS()).toBe(true);
    expect(isMobile()).toBe(true);
    Object.defineProperty(global.navigator, 'platform', { value: 'Linux armv8l', configurable: true });
    Object.defineProperty(global.navigator, 'userAgent', { value: 'Android Mobile', configurable: true });
    expect(isIOS()).toBe(false);
    expect(isMobile()).toBe(true);
    Object.defineProperty(global.navigator, 'userAgent', { value: 'Desktop Browser', configurable: true });
    expect(isMobile()).toBe(false);
  });

  it('p2p.util-copy-extend', () => {
    const destination = [0, 0, 3];
    expect(copyArray([1, 2], destination)).toBeUndefined();
    expect(destination).toEqual([1, 2, 3]);
    const inherited = { inherited: true };
    const source = Object.create(inherited);
    source.own = 2;
    const target = { keep: 1 };
    expect(extend(target, source)).toBe(target);
    expect(target).toEqual({ keep: 1, own: 2 });
    expect(target).not.toHaveProperty('inherited');
  });
});
