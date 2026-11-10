import { TOKEN_HEADER, type BackendConnection } from '@shared/backend'
import type { AudioTrack } from '@shared/models'

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string
  ) {
    super(message)
  }
}

/** Thin client for the local Python service. */
export class BackendClient {
  constructor(private readonly connection: BackendConnection) {}

  get<T>(path: string): Promise<T> {
    return this.request<T>('GET', path)
  }

  post<T>(path: string, body?: unknown): Promise<T> {
    return this.request<T>('POST', path, body)
  }

  patch<T>(path: string, body: unknown): Promise<T> {
    return this.request<T>('PATCH', path, body)
  }

  delete(path: string): Promise<void> {
    return this.request<void>('DELETE', path)
  }

  /** URL an <audio> element can load directly (token in the query string). */
  audioUrl(meetingId: number, track: AudioTrack): string {
    return `${this.connection.baseUrl}/meetings/${meetingId}/audio/${track}?token=${this.connection.token}`
  }

  wsUrl(path: string): string {
    return `${this.connection.baseUrl.replace(/^http/, 'ws')}${path}?token=${this.connection.token}`
  }

  private async request<T>(method: string, path: string, body?: unknown): Promise<T> {
    const res = await fetch(`${this.connection.baseUrl}${path}`, {
      method,
      headers: {
        [TOKEN_HEADER]: this.connection.token,
        ...(body === undefined ? {} : { 'Content-Type': 'application/json' })
      },
      body: body === undefined ? undefined : JSON.stringify(body)
    })
    if (!res.ok) {
      let message = `Request failed (${res.status})`
      try {
        const data = (await res.json()) as { detail?: unknown }
        if (typeof data.detail === 'string') message = data.detail
      } catch {
        // Response had no JSON body.
      }
      throw new ApiError(res.status, message)
    }
    if (res.status === 204) return undefined as T
    return (await res.json()) as T
  }
}
