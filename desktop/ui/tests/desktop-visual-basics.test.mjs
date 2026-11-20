import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const app = new URL('../src/desktop-chat/DesktopChatApp.tsx', import.meta.url);
const theme = new URL('../src/hooks/useTheme.ts', import.meta.url);
const styles = new URL('../src/style.css', import.meta.url);
const notifications = new URL('../src/utils/notifications.ts', import.meta.url);

test('desktop viewport uses WebView CSS pixels without device scale division', async () => {
  const component = await readFile(app, 'utf8');
  const css = await readFile(styles, 'utf8');
  assert.doesNotMatch(component, /devicePixelRatio|tauri-viewport-width|tauri-viewport-height/);
  assert.match(css, /#root\s*\{[^}]*height:\s*100dvh/s);
});

test('the selected theme resolves system preference and drives every dark variant', async () => {
  const hook = await readFile(theme, 'utf8');
  const css = await readFile(styles, 'utf8');
  assert.match(hook, /matchMedia\(['"]\(prefers-color-scheme:\s*dark\)['"]\)/);
  assert.match(hook, /setAttribute\(['"]data-theme['"],\s*resolved\)/);
  assert.match(css, /@custom-variant dark/);
  assert.match(css, /:root\[data-theme="dark"\]/);
  assert.match(css, /:root\[data-theme="light"\]/);
});

test('notification colors refer to defined theme tokens', async () => {
  const css = await readFile(styles, 'utf8');
  const source = await readFile(notifications, 'utf8');
  const used = [...source.matchAll(/var\((--[\w-]+)/g)].map(match => match[1]);
  for (const token of used) assert.match(css, new RegExp(`${token}\\s*:`));
});
