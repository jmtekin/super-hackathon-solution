// Console v4 screenshots: each view at 1440 and 390, the certificate, a quarantined cell. Runs on crib-pc.
// node shoot_v4.js <base url ending in /> <out dir>
const { chromium } = require('playwright-core');
const [base, dir] = process.argv.slice(2);
(async () => {
  const browser = await chromium.launch({ executablePath: 'C:/Users/jonas/AppData/Local/ms-playwright/chromium-1161/chrome-win/chrome.exe' });
  const shots = [
    ['backups', '?view=backups', null],
    ['backups-cert', '?view=backups#cert', null],
    ['drill', '?view=drill', null],
    ['drill-healed', '?view=drill', 'healed'],
    ['drill-cert', '?view=drill#cert', null],
  ];
  for (const [w, h, dpr] of [[1440, 900, 1], [390, 844, 2]]) {
    for (const [name, q, pick] of shots) {
      const p = await browser.newPage({ viewport: { width: w, height: h }, deviceScaleFactor: dpr });
      p.on('pageerror', e => console.log(name, w, 'pageerror', e.message));
      p.on('console', m => m.type() === 'error' && console.log(name, w, 'console', m.text()));
      await p.goto(base + q, { waitUntil: 'domcontentloaded' });
      await p.waitForFunction(() => document.querySelector('#headline').textContent !== 'Loading', null, { timeout: 15000 }).catch(() => {});
      await p.waitForTimeout(1500);
      if (pick) {
        const cell = p.locator('.cell', { hasText: pick }).first();
        if (await cell.count()) { await cell.click(); await p.waitForTimeout(800); }
      }
      const sw = await p.evaluate(() => document.documentElement.scrollWidth);
      console.log(name, w, 'scrollWidth', sw, 'note:', await p.locator('#note').textContent());
      await p.screenshot({ path: `${dir}/${name}-${w}.png`, fullPage: true });
      await p.close();
    }
  }
  // Live drill: press Run drill through the public page, shoot mid-run and when it is sealed.
  if (process.env.LIVE) {
    const p = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    p.on('pageerror', e => console.log('live pageerror', e.message));
    await p.goto(base + '?view=drill');
    await p.waitForSelector('#run');
    await p.click('#run');
    await p.waitForTimeout(7000);
    await p.screenshot({ path: `${dir}/drill-live-1440.png`, fullPage: true });
    await p.waitForFunction(() => !document.querySelector('#run')?.disabled, null, { timeout: 90000 });
    await p.waitForTimeout(1500);
    await p.screenshot({ path: `${dir}/drill-sealed-1440.png`, fullPage: true });
    console.log('live headline:', await p.locator('#headline').textContent());
    await p.close();
  }
  await browser.close();
})();
