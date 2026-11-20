import { chromium } from '/home/node/spike-deps/node_modules/playwright-core/index.mjs';
import crypto from 'node:crypto';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
const exe = process.env.HOME + '/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome';
const [prof, csvDir] = process.argv.slice(2);
const d = s => crypto.createHash('sha256').update(s).digest('hex').slice(0, 16);
const OLD = 'dummy-old-' + crypto.randomBytes(6).toString('hex'), NEW = 'dummy-new-' + crypto.randomBytes(6).toString('hex');
const out = { old_sha: d(OLD), new_sha: d(NEW) };
// throwaway CSV in a 0700 dir, 0600 file, deleted in finally
fs.mkdirSync(csvDir, { recursive: true, mode: 0o700 });
const csv = csvDir + '/import.csv';
fs.writeFileSync(csv, `name,url,username,password,note\nlocalhost,http://localhost:8765/,spike-user,${NEW},\n`, { mode: 0o600 });
const ctx = await chromium.launchPersistentContext(prof, { executablePath: exe, headless: !!process.env.HEADLESS, viewport: null, args: ['--password-store=basic', '--window-size=1280,900', '--window-position=0,0'] });
try {
  const pm = ctx.pages()[0] || await ctx.newPage();
  await pm.goto('chrome://password-manager/passwords');
  await pm.evaluate(async OLD => chrome.passwordsPrivate.addPassword({ url: 'http://localhost:8765', username: 'spike-user', password: OLD, note: '', useAccountStore: false }), OLD);
  pm.on('filechooser', fc => { out.filechooser_event = true; });
  const p = pm.evaluate(async () => { try { const r = await chrome.passwordsPrivate.importPasswords('DEVICE'); return { status: r.status, numberImported: r.numberImported, displayedEntries: (r.displayedEntries||[]).map(e => ({ status: e.status, id: e.id, username: e.username, url: e.url })), fileName: r.fileName }; } catch (e) { return 'err:' + e.message; } });
  await pm.waitForTimeout(2500);
  execFileSync('python3', [process.env.HOME + '/spike-work/shot.py', process.env.DISPLAY, process.env.HOME + '/spike-work/shots/import-dialog.png']);
  if (process.env.TYPE_PATH) execFileSync('python3', [process.env.HOME + '/spike-work/xtype.py', process.env.DISPLAY, csv]);
  out.import = await Promise.race([p, new Promise(r => setTimeout(() => r('timeout-no-result'), 15000))]);
  execFileSync('python3', [process.env.HOME + '/spike-work/shot.py', process.env.DISPLAY, process.env.HOME + '/spike-work/shots/import-after.png']);
  if (out.import && out.import.status === 'CONFLICTS' && process.env.CONTINUE) {
    out.continue = await pm.evaluate(async ids => { try { const r = await chrome.passwordsPrivate.continueImport(ids); return { status: r.status, numberImported: r.numberImported }; } catch (e) { return 'err:' + e.message; } }, out.import.displayedEntries.map(e => e.id));
  }
  await pm.waitForTimeout(1000);
} finally { await ctx.close(); fs.rmSync(csvDir, { recursive: true, force: true }); out.csv_unlinked = !fs.existsSync(csv); }
console.log(JSON.stringify(out));
