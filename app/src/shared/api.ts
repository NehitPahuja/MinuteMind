import type { BackendConnection, BackendStatus } from './backend'

/** The API the preload script exposes to the renderer as `window.api`. */
export interface MinuteMindApi {
  backend: {
    getStatus(): Promise<BackendStatus>
    getConnection(): Promise<BackendConnection | null>
    restart(): Promise<void>
    onStatus(listener: (status: BackendStatus) => void): () => void
  }
  files: {
    /** Ask where to save a text file, then write it. Returns the path, or null if cancelled. */
    saveText(suggestedName: string, content: string): Promise<string | null>
  }
}
