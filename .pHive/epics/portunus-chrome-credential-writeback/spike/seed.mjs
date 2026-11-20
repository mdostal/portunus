import { chromium } from '/home/node/spike-deps/node_modules/playwright-core/index.mjs';
import crypto from 'node:crypto';
const exe = process.env.HOME + '/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome';
const [prof, url, user, pw] = process.argv.slice(2);
const d = s => crypto.createHash('sha256').update(s).digest('hex').slice(0,16);
const ctx = await chromium.launchPersistentContext(prof, { executablePath: exe, headless: !process.env.HEADED, args: ['--password-store=basic'] });
const page = ctx.pages()[0] || await ctx.newPage();
await page.goto('chrome://password-manager/passwords');
const r = await page.evaluate(async ([url,user,pw]) => {
  const pp = chrome.passwordsPrivate;
  let add;
  try { await pp.addPassword({url, username: user, password: pw, note: '', useAccountStore: false}); add='ok'; } catch(e) { add = 'err:' + e.message; }
  const list = await pp.getSavedPasswordList();
  return {add, list: list.map(x => ({id:x.id, url:x.urls.link, signon:x.urls.signonRealm, user:x.username, storedIn:x.storedIn}))};
}, [url,user,pw]);
console.log(JSON.stringify({expected_sha256_16: d(pw), ...r}));
await new Promise(r=>setTimeout(r,1500));
await ctx.close();
