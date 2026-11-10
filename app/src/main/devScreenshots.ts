import { app, type BrowserWindow } from 'electron'
import { mkdirSync, writeFileSync } from 'fs'
import { join } from 'path'

/**
 * Development aid: when MINUTEMIND_SCREENSHOT_DIR is set, visit each page,
 * save a PNG of it to that folder, and quit. Lets the UI be checked visually
 * from scripts without a browser. Does nothing in normal use.
 */
const PAGES = [
  { file: 'meetings.png', button: 'Meetings' },
  { file: 'record.png', button: 'New recording' },
  { file: 'ask.png', button: 'Ask' },
  { file: 'settings.png', button: 'Settings' }
]

export async function captureScreenshotsIfRequested(
  win: BrowserWindow,
  backendReady: Promise<void>
): Promise<void> {
  const dir = process.env['MINUTEMIND_SCREENSHOT_DIR']
  if (!dir) return
  mkdirSync(dir, { recursive: true })

  await new Promise<void>((resolve) => win.webContents.once('did-finish-load', () => resolve()))
  await backendReady
  await waitForFonts(win)

  for (const page of PAGES) {
    await win.webContents.executeJavaScript(
      `[...document.querySelectorAll('aside button')].find(b => b.textContent.trim().endsWith(${JSON.stringify(page.button)}))?.click()`
    )
    await new Promise((r) => setTimeout(r, 300))
    // Wait for two animation frames so the capture reflects the new page.
    await win.webContents.executeJavaScript(
      'new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(() => r(true))))'
    )
    const image = await win.webContents.capturePage()
    writeFileSync(join(dir, page.file), image.toPNG())
    console.log(`[minutemind] screenshot ${page.file}`)
  }
  app.quit()
}

function waitForFonts(win: BrowserWindow): Promise<unknown> {
  return win.webContents.executeJavaScript('document.fonts.ready.then(() => true)')
}
