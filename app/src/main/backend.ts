import { spawn, execFile, type ChildProcess } from 'child_process'
import { randomBytes } from 'crypto'
import { existsSync } from 'fs'
import { join, resolve } from 'path'
import { app } from 'electron'
import { TOKEN_HEADER, type BackendConnection, type BackendStatus } from '../shared/backend'

const PORT_PREFIX = 'MINUTEMIND_PORT='
const STARTUP_TIMEOUT_MS = 90_000 // first `uv run` may need to sync the venv
const HEALTH_POLL_MS = 300

type Listener = (status: BackendStatus) => void

/**
 * Owns the Python service process: starts it, waits for it to be healthy,
 * reports status changes, and makes sure it is gone when the app quits.
 */
export class BackendManager {
  private child: ChildProcess | null = null
  private token = ''
  private port = 0
  private status: BackendStatus = { state: 'starting' }
  private listeners = new Set<Listener>()
  private stopping = false

  getStatus(): BackendStatus {
    return this.status
  }

  getConnection(): BackendConnection | null {
    if (this.status.state !== 'ready') return null
    return { baseUrl: `http://127.0.0.1:${this.port}`, token: this.token }
  }

  onStatus(listener: Listener): () => void {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }

  async start(): Promise<void> {
    this.stop()
    this.stopping = false
    this.setStatus({ state: 'starting' })
    this.token = randomBytes(32).toString('hex')

    let command: { file: string; args: string[]; cwd: string }
    try {
      command = resolveBackendCommand()
    } catch (err) {
      this.setStatus({ state: 'error', message: (err as Error).message })
      return
    }

    const child = spawn(
      command.file,
      [...command.args, '-m', 'minutemind', '--parent-pid', String(process.pid)],
      {
        cwd: command.cwd,
        env: { ...process.env, MINUTEMIND_TOKEN: this.token, PYTHONUNBUFFERED: '1' },
        windowsHide: true,
        stdio: ['ignore', 'pipe', 'pipe']
      }
    )
    this.child = child

    let stderrTail = ''
    child.stderr?.setEncoding('utf8')
    child.stderr?.on('data', (chunk: string) => {
      stderrTail = (stderrTail + chunk).slice(-2000)
      process.stderr.write(`[backend] ${chunk}`)
    })

    const portPromise = new Promise<number>((resolvePort, reject) => {
      let buffer = ''
      child.stdout?.setEncoding('utf8')
      child.stdout?.on('data', (chunk: string) => {
        buffer += chunk
        const line = buffer.split(/\r?\n/).find((l) => l.startsWith(PORT_PREFIX))
        if (line) resolvePort(Number(line.slice(PORT_PREFIX.length)))
      })
      child.once('error', reject)
      child.once('exit', (code) =>
        reject(new Error(`Backend exited with code ${code}. ${stderrTail.trim()}`))
      )
    })

    child.once('exit', (code) => {
      if (this.child === child) this.child = null
      if (!this.stopping && this.status.state === 'ready') {
        this.setStatus({ state: 'error', message: `Backend stopped unexpectedly (code ${code}).` })
      }
    })

    try {
      this.port = await withTimeout(
        portPromise,
        STARTUP_TIMEOUT_MS,
        'Backend did not report a port'
      )
      const version = await this.waitForHealth(STARTUP_TIMEOUT_MS)
      this.setStatus({ state: 'ready', port: this.port, version })
    } catch (err) {
      this.stop()
      this.setStatus({ state: 'error', message: (err as Error).message })
    }
  }

  /**
   * Ask the backend to finish any recording in progress (so its files are
   * closed and mixed properly), then stop the process. Never waits longer
   * than `timeoutMs`.
   */
  async shutdown(timeoutMs = 8000): Promise<void> {
    const connection = this.getConnection()
    if (connection) {
      try {
        await fetch(`${connection.baseUrl}/recording/stop`, {
          method: 'POST',
          headers: { [TOKEN_HEADER]: connection.token },
          signal: AbortSignal.timeout(timeoutMs)
        })
      } catch {
        // Nothing was recording, or the backend is unresponsive: stop it anyway.
      }
    }
    this.stop()
  }

  stop(): void {
    this.stopping = true
    const child = this.child
    this.child = null
    if (!child || child.pid === undefined || child.exitCode !== null) return
    if (process.platform === 'win32') {
      // Kill the whole process tree: `uv run` starts python as a grandchild.
      execFile('taskkill', ['/pid', String(child.pid), '/T', '/F'], () => undefined)
    } else {
      child.kill()
    }
  }

  private async waitForHealth(timeoutMs: number): Promise<string> {
    const deadline = Date.now() + timeoutMs
    let lastError = 'no response'
    while (Date.now() < deadline) {
      try {
        const res = await fetch(`http://127.0.0.1:${this.port}/health`, {
          headers: { [TOKEN_HEADER]: this.token }
        })
        if (res.ok) {
          const body = (await res.json()) as { version?: string }
          return body.version ?? 'unknown'
        }
        lastError = `HTTP ${res.status}`
      } catch (err) {
        lastError = (err as Error).message
      }
      await new Promise((r) => setTimeout(r, HEALTH_POLL_MS))
    }
    throw new Error(`Backend health check failed: ${lastError}`)
  }

  private setStatus(status: BackendStatus): void {
    this.status = status
    for (const listener of this.listeners) listener(status)
  }
}

/**
 * Development: run the backend from ../backend, preferring its venv python and
 * falling back to `uv run`. Packaged builds are handled in Phase 8.
 */
function resolveBackendCommand(): { file: string; args: string[]; cwd: string } {
  const backendDir = resolve(app.getAppPath(), '..', 'backend')
  if (!existsSync(join(backendDir, 'pyproject.toml'))) {
    throw new Error(`Backend project not found at ${backendDir}`)
  }
  const venvPython =
    process.platform === 'win32'
      ? join(backendDir, '.venv', 'Scripts', 'python.exe')
      : join(backendDir, '.venv', 'bin', 'python')
  if (existsSync(venvPython)) {
    return { file: venvPython, args: [], cwd: backendDir }
  }
  return { file: 'uv', args: ['run', '--project', backendDir, 'python'], cwd: backendDir }
}

function withTimeout<T>(promise: Promise<T>, ms: number, message: string): Promise<T> {
  return new Promise<T>((resolvePromise, reject) => {
    const timer = setTimeout(() => reject(new Error(message)), ms)
    promise.then(
      (value) => {
        clearTimeout(timer)
        resolvePromise(value)
      },
      (err) => {
        clearTimeout(timer)
        reject(err)
      }
    )
  })
}
