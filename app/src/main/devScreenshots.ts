import { app, type BrowserWindow } from 'electron'
import { mkdirSync, writeFileSync } from 'fs'
import { join } from 'path'

/**
 * Development aid: when MINUTEMIND_SCREENSHOT_DIR is set, visit each page,
 * save a PNG of it to that folder, and quit. Lets the UI be checked visually
 * from scripts without a browser. Does nothing in normal use.
 *
 * With MINUTEMIND_SCREENSHOT_RECORD=<seconds> it also runs a real recording of
 * that length through the UI and captures the live and finished states. Point
 * MINUTEMIND_DATA_DIR at a scratch folder so test meetings don't land in your
 * real library. Add MINUTEMIND_SCREENSHOT_QUIT_WHILE_RECORDING=1 to quit the
 * app mid-recording instead of pressing stop.
 *
 * With MINUTEMIND_SCREENSHOT_TRANSCRIPT=<seconds> it opens the newest meeting,
 * waits up to that long for its transcript, and clicks the second line.
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
  await run(win, 'document.fonts.ready.then(() => true)')
  await sleep(800) // let pages fetch their first data

  const shoot = async (file: string): Promise<void> => {
    await sleep(300)
    // Wait for two animation frames so the capture reflects the latest render.
    await run(
      win,
      'new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(() => r(true))))'
    )
    writeFileSync(join(dir, file), (await win.webContents.capturePage()).toPNG())
    console.log(`[minutemind] screenshot ${file}`)
  }

  for (const page of PAGES) {
    await clickSidebar(win, page.button)
    await shoot(page.file)
  }

  const transcriptWait = Number(process.env['MINUTEMIND_SCREENSHOT_TRANSCRIPT'] ?? 0)
  if (transcriptWait > 0) {
    // Open the newest meeting, wait for its transcript, then click a line.
    await clickSidebar(win, 'Meetings')
    await sleep(800)
    await run(win, `document.querySelector('main ol > li > button')?.click()`)
    const deadline = Date.now() + transcriptWait * 1000
    let lines = 0
    while (Date.now() < deadline) {
      // Spoken lines are buttons directly inside the list item; notes are not.
      lines = Number(
        await run(
          win,
          `document.querySelectorAll('section[aria-label="Transcript"] ol li > button').length`
        )
      )
      if (lines > 0) break
      await sleep(1000)
    }
    console.log(`[minutemind] transcript lines: ${lines}`)
    await shoot('transcript.png')
    await run(
      win,
      `document.querySelectorAll('section[aria-label="Transcript"] ol li > button')[1]?.click()`
    )
    await sleep(1500)
    const position = await run(win, `document.querySelector('audio')?.currentTime.toFixed(2)`)
    console.log(`[minutemind] clicked line 2, player at ${position}s`)
    await shoot('transcript-playing.png')
  }

  const recordSeconds = Number(process.env['MINUTEMIND_SCREENSHOT_RECORD'] ?? 0)
  if (recordSeconds > 0) {
    await clickSidebar(win, 'New recording')
    await sleep(800) // device list
    await run(
      win,
      `(() => { const i = document.querySelector('input[placeholder^="Optional"]');
        const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
        set.call(i, 'Screenshot test'); i.dispatchEvent(new Event('input', { bubbles: true })); })()`
    )
    await clickLabel(win, 'Start recording')
    // Type a note a few seconds in, as a user would.
    await sleep(Math.min(5, recordSeconds / 2) * 1000)
    await run(
      win,
      `(() => { const t = document.querySelector('textarea[aria-label="New note"]');
        if (!t) return;
        const set = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set;
        set.call(t, 'Follow up on the hosting quote'); t.dispatchEvent(new Event('input', { bubbles: true }));
        t.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true })); })()`
    )
    await sleep((recordSeconds - Math.min(5, recordSeconds / 2)) * 1000)
    const liveLines = await run(win, `document.querySelectorAll('[aria-live="polite"] li').length`)
    console.log(`[minutemind] live transcript lines: ${liveLines}`)
    await shoot('recording-live.png')
    if (process.env['MINUTEMIND_SCREENSHOT_QUIT_WHILE_RECORDING']) {
      // Quit mid-recording: the app must stop and save the meeting on its way out.
      app.quit()
      return
    }
    await clickLabel(win, 'Stop recording')
    await sleep(1500)
    await shoot('recording-done.png')
    await clickLabel(win, 'Play')
    await sleep(1500)
    const position = await run(
      win,
      `document.querySelector('audio') && document.querySelector('audio').currentTime.toFixed(2)`
    )
    console.log(`[minutemind] playback position after 1.5 s: ${position}`)
    await shoot('playback.png')
  }
  app.quit()
}

function run(win: BrowserWindow, code: string): Promise<unknown> {
  return win.webContents.executeJavaScript(code)
}

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms))
}

function clickSidebar(win: BrowserWindow, text: string): Promise<unknown> {
  return run(
    win,
    `[...document.querySelectorAll('aside button')].find(b => b.textContent.trim().endsWith(${JSON.stringify(text)}))?.click()`
  )
}

function clickLabel(win: BrowserWindow, label: string): Promise<unknown> {
  return run(win, `document.querySelector('[aria-label=${JSON.stringify(label)}]')?.click()`)
}
