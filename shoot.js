// End-to-end check through the public URL: run each fixture in a headless browser
// and screenshot the result (plus the containment moment mid-run) into shots/.
// Usage: node shoot.js [base-url] [fixture ...]
const path = require('path');
const { chromium } = require('/opt/agent/browser/node_modules/playwright-core');
const base = process.argv[2] || 'https://vps.taild101c7.ts.net:10000/restorer/';
const fixtures = process.argv.slice(3).length ? process.argv.slice(3) : ['matrix', 'recoverable', 'corrupted', 'compromised', 'drift'];

(async () => {
  const browser = await chromium.launch({ headless: true, executablePath: process.env.CHROME, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  for (const fx of fixtures) {
    await page.goto(`${base}?fixture=${fx}`);
    await page.waitForSelector('#run:not([disabled])');
    await page.click('#run');
    if (fx === 'compromised') {
      await page.waitForSelector('text=NETWORK: DENIED', { timeout: 60000 });
      await page.waitForTimeout(1800);
      await page.screenshot({ path: path.join(__dirname, 'shots', `${fx}-live.png`) });
    }
    if (fx === 'matrix') {
      await page.waitForSelector('.mc.healing', { timeout: 60000 });
      await page.screenshot({ path: path.join(__dirname, 'shots', `${fx}-healing.png`) });
      await page.waitForSelector('#b-cap.on', { timeout: 60000 });
      await page.$eval('#matrix', (e) => e.scrollIntoView());
      await page.waitForTimeout(400);
      await page.screenshot({ path: path.join(__dirname, 'shots', `${fx}-containment.png`) });
    }
    if (fx === 'drift') {
      await page.waitForSelector('#n-nightly-09.incompatible', { timeout: 120000 });
      await page.waitForTimeout(300);
      await page.screenshot({ path: path.join(__dirname, 'shots', `${fx}-live.png`) });
    }
    await page.waitForSelector('#verdict.ok, #verdict.bad', { timeout: 180000 });
    await page.waitForTimeout(800);
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.screenshot({ path: path.join(__dirname, 'shots', `${fx}.png`), fullPage: true });
    console.log(fx, await page.textContent('#verdict h1'));
  }
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
