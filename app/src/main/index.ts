import { app, shell, BrowserWindow, ipcMain } from 'electron'
import { join } from 'path'
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
    backgroundColor: '#12100d',
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

app.on('before-quit', () => backend.stop())

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit()
})
