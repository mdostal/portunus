// passwordsPrivate.changeCredential + importPasswords probe on a throwaway profile (headless unless HEADED)
import { chromium } from '/home/node/spike-deps/node_modules/playwright-core/index.mjs';
import crypto from 'node:crypto';
const exe = process.env.HOME + '/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome';
const [prof, mode] = process.argv.slice(2);
const d = s => crypto.createHash('sha256').update(s).digest('hex').slice(0, 16);
const OLD = 'dummy-old-' + crypto.randomBytes(6).toString('hex'), NEW = 'dummy-new-' + crypto.randomBytes(6).toString('hex');
const out = { mode, old_sha: d(OLD), new_sha: d(NEW) };
const ctx = await chromium.launchPersistentContext(prof, { executablePath: exe, headless: !process.env.HEADED, args: ['--password-store=basic'] });
const pm = ctx.pages()[0] || await ctx.newPage();
await pm.goto('chrome://password-manager/passwords');
out.r = await pm.evaluate(async ([OLD, NEW, mode]) => {
  const pp = chrome.passwordsPrivate; const r = {};
  await pp.addPassword({ url: 'http://localhost:8765', username: 'spike-user', password: OLD, note: '', useAccountStore: false });
  await new Promise(x => setTimeout(x, 500));
  const list = await pp.getSavedPasswordList();
  r.list = list.map(x => ({ id: x.id, keys: Object.keys(x).sort() }));
  if (mode === 'change') {
    try { const det = await pp.requestCredentialsDetails([list[0].id]); r.details_keys = Object.keys(det[0] || {}).sort();
      const cred = det[0]; const upd = { ...cred, password: NEW }; await pp.changeCredential(upd); r.change = 'ok';
    } catch (e) { r.change = 'err:' + String(e.message).slice(0, 200); }
    for (const k of ['details_keys']) {}
  }
  return r;
}, [OLD, NEW, mode]);
await pm.waitForTimeout(1000); await ctx.close();
console.log(JSON.stringify(out));
