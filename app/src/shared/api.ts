import type { BackendConnection, BackendStatus } from './backend'

/** The API the preload script exposes to the renderer as `window.api`. */
export interface MinuteMindApi {
  backend: {
    getStatus(): Promise<BackendStatus>
    getConnection(): Promise<BackendConnection | null>
    restart(): Promise<void>
    onStatus(listener: (status: BackendStatus) => void): () => void
  }
}
