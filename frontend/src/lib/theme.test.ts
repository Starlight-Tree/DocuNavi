import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { THEME_INIT_SCRIPT, THEME_KEY, chooseTheme, currentTheme, storedTheme, systemTheme } from './theme';

function stubSystem(dark: boolean) {
  vi.stubGlobal('matchMedia', (query: string) => ({ matches: dark && query === '(prefers-color-scheme: dark)', media: query }));
}

beforeEach(() => {
  window.localStorage.clear();
  delete document.documentElement.dataset.theme;
});
afterEach(() => vi.unstubAllGlobals());

describe('テーマ', () => {
  it('選んでいなければ OS に従う（保存も data-theme も無い）', () => {
    stubSystem(true);
    expect(storedTheme()).toBeNull();
    expect(systemTheme()).toBe('dark');
    expect(currentTheme()).toBe('dark');
    stubSystem(false);
    expect(currentTheme()).toBe('light');
  });

  it('選ぶと、適用して保存し、OS より優先する', () => {
    stubSystem(true);
    chooseTheme('light');
    expect(document.documentElement.dataset.theme).toBe('light');
    expect(window.localStorage.getItem(THEME_KEY)).toBe('light');
    expect(storedTheme()).toBe('light');
    expect(currentTheme()).toBe('light');
  });

  it('保存値が壊れていれば、無いものとして扱う', () => {
    window.localStorage.setItem(THEME_KEY, 'blue');
    expect(storedTheme()).toBeNull();
  });

  it('localStorage が使えなくても、例外にせず、このページでは適用する', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('blocked');
    });
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('blocked');
    });
    expect(storedTheme()).toBeNull();
    expect(() => chooseTheme('dark')).not.toThrow();
    expect(document.documentElement.dataset.theme).toBe('dark');
    vi.restoreAllMocks();
  });

  it('描画前のスクリプトは、保存値（light / dark）だけを data-theme へ入れる', () => {
    window.localStorage.setItem(THEME_KEY, 'dark');
    new Function(THEME_INIT_SCRIPT)();
    expect(document.documentElement.dataset.theme).toBe('dark');

    delete document.documentElement.dataset.theme;
    window.localStorage.setItem(THEME_KEY, '<script>');
    new Function(THEME_INIT_SCRIPT)();
    expect(document.documentElement.dataset.theme).toBeUndefined();
  });
});

describe('ダークの値の 2 か所（OS 追従と data-theme）', () => {
  // CSS では 2 つの条件で共有できないので、同じ内容を 2 か所に書いている。ずれたらここで気づく。
  for (const file of ['styles.css', 'shell.css']) {
    it(`${file}: 同じ宣言が入っている`, () => {
      const css = readFileSync(join(process.cwd(), 'src', file), 'utf8');
      const declarations = (block: string) =>
        [...block.matchAll(/(--[\w-]+):\s*([^;]+);/g)].map(([, name, value]) => `${name}: ${value?.trim()}`).sort();
      const media = /@media \(prefers-color-scheme: dark\) \{\s*:root:not\(\[data-theme='light'\]\) \{([^}]*)\}/.exec(css)?.[1];
      const manual = /:root\[data-theme='dark'\] \{([^}]*)\}/.exec(css)?.[1];
      expect(media).toBeDefined();
      expect(manual).toBeDefined();
      expect(declarations(manual ?? '')).toEqual(declarations(media ?? ''));
      expect(declarations(manual ?? '').length).toBeGreaterThan(5);
    });
  }
});
