import { contextBridge, ipcRenderer } from 'electron'
import type { BackendConnection, BackendStatus } from '../shared/backend'
import type { MinuteMindApi } from '../shared/api'

const api: MinuteMindApi = {
  backend: {
    getStatus: (): Promise<BackendStatus> => ipcRenderer.invoke('backend:get-status'),
    getConnection: (): Promise<BackendConnection | null> =>
      ipcRenderer.invoke('backend:get-connection'),
    restart: (): Promise<void> => ipcRenderer.invoke('backend:restart'),
    onStatus: (listener: (status: BackendStatus) => void): (() => void) => {
      const handler = (_: Electron.IpcRendererEvent, status: BackendStatus): void =>
        listener(status)
      ipcRenderer.on('backend:status', handler)
      return () => ipcRenderer.removeListener('backend:status', handler)
    }
  }
}

if (process.contextIsolated) {
  contextBridge.exposeInMainWorld('api', api)
} else {
  // @ts-ignore (defined in index.d.ts)
  window.api = api
}
