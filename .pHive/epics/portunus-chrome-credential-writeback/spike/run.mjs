// usage: node run.mjs <profile> <mode> [--no-seed]
// modes: page-nogesture | cdp-gesture | click | ext-isolated | ext-main | ext-main-native | hostile-ext-main | hostile-ext-isolated | hostile-ext-main-native
import { chromium } from '/home/node/spike-deps/node_modules/playwright-core/index.mjs';
import crypto from 'node:crypto';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
const exe = process.env.HOME + '/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome';
const [prof, mode, ...flags] = process.argv.slice(2);
const ORIGIN = 'http://localhost:8765';
const USER = 'spike-user';
const d = s => crypto.createHash('sha256').update(s).digest('hex').slice(0, 16);
const OLD = 'dummy-old-' + crypto.randomBytes(6).toString('hex');
const NEW = 'dummy-new-' + crypto.randomBytes(6).toString('hex');
const ext = process.env.HOME + '/spike-work/ext';
const out = { mode, old_sha: d(OLD), new_sha: d(NEW) };
const ctx = await chromium.launchPersistentContext(prof, { executablePath: exe, headless: !!process.env.HEADLESS, viewport: null,
  ignoreDefaultArgs: ['--disable-extensions', '--enable-automation', '--password-store=basic', '--disable-component-extensions-with-background-pages'],
  args: ['--password-store=basic', `--disable-extensions-except=${ext}`, `--load-extension=${ext}`, '--window-size=1280,900', '--window-position=0,0'] });
const pm = ctx.pages()[0] || await ctx.newPage();
await pm.goto('chrome://password-manager/passwords');
if (!flags.includes('--no-seed')) {
  const seedUrl = process.env.SEED_URL || ORIGIN;
  out.seed = await pm.evaluate(async ([u, n, p, acct]) => { try { await chrome.passwordsPrivate.addPassword({ url: u, username: n, password: p, note: '', useAccountStore: acct }); return 'ok'; } catch (e) { return 'err:' + e.message; } }, [seedUrl, USER, OLD, false]);
  if (process.env.SEED_ACCOUNT) out.seed_account = await pm.evaluate(async ([u, n, p]) => { try { await chrome.passwordsPrivate.addPassword({ url: u, username: n, password: p, note: '', useAccountStore: true }); return 'ok'; } catch (e) { return 'err:' + e.message; } }, [seedUrl, USER, OLD + '-acct']);
  out.list_after_seed = await pm.evaluate(async () => { await new Promise(r => setTimeout(r, 500)); return (await chrome.passwordsPrivate.getSavedPasswordList()).map(x => ({ signon: x.urls && x.urls.signonRealm, link: x.urls && x.urls.link, user: x.username, storedIn: x.storedIn })); });
}
const internals = await ctx.newPage();
await internals.goto('chrome://password-manager-internals');
const page = await ctx.newPage();
const path = mode.startsWith('hostile') ? '/hostile.html' : '/index.html';
await page.goto(ORIGIN + path);
await page.bringToFront();
let sw = ctx.serviceWorkers()[0] || await ctx.waitForEvent('serviceworker', { timeout: 10000 }).catch(() => null);
const tabId = sw ? (await sw.evaluate(() => self.spikeTabs())).find(t => t.url.startsWith(ORIGIN))?.id : null;
const pending = { id: USER, password: NEW };
try {
  if (mode === 'page-nogesture') {
    out.call = await page.evaluate(async c => { const a = navigator.userActivation.isActive; try { await navigator.credentials.store(new PasswordCredential(c)); return { ok: true, hasActivation: a }; } catch (e) { return { ok: false, err: e.name, hasActivation: a }; } }, pending);
  } else if (mode === 'cdp-gesture' || mode === 'cdp-nogesture') {
    const s = await ctx.newCDPSession(page);
    const r = await s.send('Runtime.evaluate', { userGesture: mode === 'cdp-gesture', awaitPromise: true, returnByValue: true,
      expression: `(async()=>{const a=navigator.userActivation.isActive;try{await navigator.credentials.store(new PasswordCredential(${JSON.stringify(pending)}));return {ok:true,hasActivation:a}}catch(e){return {ok:false,err:e.name,hasActivation:a}}})()` });
    out.call = r.result.value;
  } else if (mode === 'click') {
    await page.evaluate(c => { window.__spikePending = c; }, pending);
    await page.click('#go'); await page.waitForFunction(() => document.title.startsWith('stored'), null, { timeout: 5000 }).catch(() => {});
    out.call = await page.title();
  } else if (mode.includes('ext-')) {
    const world = mode.includes('isolated') ? 'ISOLATED' : 'MAIN';
    const m = mode.endsWith('native') ? 'native-captured' : 'plain';
    out.call = await sw.evaluate(([t, w, i, p, m]) => self.spikeStore(t, w, i, p, m), [tabId, world, USER, NEW, m]);
  }
} catch (e) { out.call = 'harness-err:' + e.message.split('\n')[0]; }
await page.waitForTimeout(2500);
if (process.env.ACCEPT_TRY) {
  // try to accept a pending bubble using only CDP (no OS-level input): Enter / Space / Tab+Enter dispatched via Input domain
  const s2 = await ctx.newCDPSession(page);
  for (const key of [['Enter', 13, '\r'], [' ', 32, ' '], ['Tab', 9, ''], ['Enter', 13, '\r']]) {
    await s2.send('Input.dispatchKeyEvent', { type: 'keyDown', key: key[0], windowsVirtualKeyCode: key[1], text: key[2] || undefined });
    await s2.send('Input.dispatchKeyEvent', { type: 'keyUp', key: key[0], windowsVirtualKeyCode: key[1] });
  }
  out.accept_try = 'sent Enter/Space/Tab/Enter via CDP Input.dispatchKeyEvent';
  await page.waitForTimeout(1500);
}
if (mode.startsWith('hostile')) out.captured = await page.evaluate(() => window.__captured);
fs.mkdirSync(process.env.HOME + '/spike-work/shots', { recursive: true });
const shot = `${process.env.HOME}/spike-work/shots/${mode}.png`;
try { execFileSync('python3', [process.env.HOME + '/spike-work/shot.py', process.env.DISPLAY, shot]); out.shot = shot; } catch (e) { out.shot = 'err'; }
const log = await internals.evaluate(() => document.body.innerText);
// keep only decision lines, scrub nothing secret (internals never prints password values, but grep anyway)
out.internals = log.split('\n').filter(l => /save|update|prompt|bubble|PasswordFormManager|credential|store|pending|blocklist|reason|decision/i.test(l)).filter(l => !l.includes(NEW) && !l.includes(OLD)).slice(0, 60);
out.leak_check_internals = log.includes(NEW) || log.includes(OLD) ? 'VALUE-IN-INTERNALS' : 'clean';
if (process.env.HOLD) { fs.writeFileSync(process.env.HOME + '/spike-work/hold.json', JSON.stringify(out)); await new Promise(r => setTimeout(r, +process.env.HOLD)); }
await ctx.close();
console.log(JSON.stringify(out, null, 1));
