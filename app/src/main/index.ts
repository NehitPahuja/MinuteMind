import { app, shell, BrowserWindow, dialog, ipcMain } from 'electron'
import { writeFile } from 'fs/promises'
import { basename, join } from 'path'
import { electronApp, optimizer, is } from '@electron-toolkit/utils'
import icon from '../../resources/icon.png?asset'
import { BackendManager } from './backend'
import { captureScreenshotsIfRequested } from './devScreenshots'

const backend = new BackendManager()

function createWindow(): BrowserWindow {
  const mainWindow = new BrowserWindow({
    width: 1200,
    height: 780,
    minWidth: 900,
    minHeight: 600,
    show: false,
    title: 'MinuteMind',
    backgroundColor: '#000000',
    autoHideMenuBar: true,
    ...(process.platform === 'linux' ? { icon } : {}),
    webPreferences: {
      preload: join(__dirname, '../preload/index.js'),
      sandbox: false
    }
  })

  mainWindow.on('ready-to-show', () => mainWindow.show())

  mainWindow.webContents.setWindowOpenHandler((details) => {
    shell.openExternal(details.url)
    return { action: 'deny' }
  })

  if (is.dev && process.env['ELECTRON_RENDERER_URL']) {
    mainWindow.loadURL(process.env['ELECTRON_RENDERER_URL'])
  } else {
    mainWindow.loadFile(join(__dirname, '../renderer/index.html'))
  }
  return mainWindow
}

app.whenReady().then(() => {
  electronApp.setAppUserModelId('com.minutemind.app')

  app.on('browser-window-created', (_, window) => {
    optimizer.watchWindowShortcuts(window)
  })

  ipcMain.handle('backend:get-status', () => backend.getStatus())
  ipcMain.handle('backend:get-connection', () => backend.getConnection())
  ipcMain.handle('backend:restart', () => backend.start())
  ipcMain.handle('files:save-text', (event, suggestedName: unknown, content: unknown) =>
    saveText(BrowserWindow.fromWebContents(event.sender), suggestedName, content)
  )

  backend.onStatus((status) => {
    console.log(`[minutemind] backend ${JSON.stringify(status)}`)
    for (const win of BrowserWindow.getAllWindows()) {
      win.webContents.send('backend:status', status)
    }
  })

  const win = createWindow()
  const backendReady = new Promise<void>((resolve) => {
    const off = backend.onStatus((s) => {
      if (s.state !== 'starting') {
        off()
        resolve()
      }
    })
  })
  void backend.start()
  void captureScreenshotsIfRequested(win, backendReady)

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow()
  })
})

/** Show a save dialog for a Markdown file and write it. Returns the path, or null. */
async function saveText(
  win: BrowserWindow | null,
  suggestedName: unknown,
  content: unknown
): Promise<string | null> {
  if (typeof suggestedName !== 'string' || typeof content !== 'string') return null
  // Only a file name is accepted from the page, never a folder.
  const name =
    basename(suggestedName)
      .replace(/[^\w .,()'&-]/g, '')
      .trim() || 'minutes.md'
  const options = {
    defaultPath: join(app.getPath('documents'), name),
    filters: [{ name: 'Markdown', extensions: ['md'] }]
  }
  const result = win
    ? await dialog.showSaveDialog(win, options)
    : await dialog.showSaveDialog(options)
  if (result.canceled || !result.filePath) return null
  await writeFile(result.filePath, content, 'utf8')
  return result.filePath
}

let quitting = false
app.on('before-quit', (event) => {
  if (quitting) return
  event.preventDefault()
  quitting = true
  void backend.shutdown().finally(() => app.quit())
})

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit()
})
