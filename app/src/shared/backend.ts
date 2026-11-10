// Types shared by the main, preload and renderer processes.

export type BackendStatus =
  | { state: 'starting' }
  | { state: 'ready'; port: number; version: string }
  | { state: 'error'; message: string }

export interface BackendConnection {
  baseUrl: string
  token: string
}

export const TOKEN_HEADER = 'X-MinuteMind-Token'
