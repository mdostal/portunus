import { chromium } from '/home/node/spike-deps/node_modules/playwright-core/index.mjs';
const exe = process.env.HOME + '/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome';
const prof = process.argv[2];
const ctx = await chromium.launchPersistentContext(prof, { executablePath: exe, headless: process.env.HEADED ? false : true,
  args: ['--password-store=basic'] });
const page = ctx.pages()[0] || await ctx.newPage();
try {
  await page.goto('chrome://password-manager/passwords');
  const r = await page.evaluate(async () => ({
    has: typeof chrome !== 'undefined' && !!chrome.passwordsPrivate,
    keys: (typeof chrome !== 'undefined' && chrome.passwordsPrivate) ? Object.keys(chrome.passwordsPrivate).sort() : null,
  }));
  console.log(JSON.stringify(r));
} catch (e) { console.log('ERR', e.message.split('\n')[0]); }
await ctx.close();
