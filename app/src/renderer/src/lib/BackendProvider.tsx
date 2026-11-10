import { useEffect, useMemo, useState } from 'react'
import type { BackendConnection, BackendStatus } from '@shared/backend'
import { BackendClient } from './client'
import { BackendContext } from './contexts'

export function BackendProvider({ children }: { children: React.ReactNode }): React.JSX.Element {
  const [status, setStatus] = useState<BackendStatus>({ state: 'starting' })
  const [connection, setConnection] = useState<{ port: number; value: BackendConnection } | null>(
    null
  )

  useEffect(() => {
    let active = true
    void window.api.backend.getStatus().then((s) => {
      if (active) setStatus(s)
    })
    const unsubscribe = window.api.backend.onStatus(setStatus)
    return () => {
      active = false
      unsubscribe()
    }
  }, [])

  // A restarted backend gets a new port and token, so fetch them on every "ready".
  const readyPort = status.state === 'ready' ? status.port : null
  useEffect(() => {
    if (readyPort === null) return
    let active = true
    void window.api.backend.getConnection().then((c) => {
      if (active && c) setConnection({ port: readyPort, value: c })
    })
    return () => {
      active = false
    }
  }, [readyPort])

  // Only hand out a client for the backend that is ready right now.
  const current = connection && connection.port === readyPort ? connection.value : null
  const client = useMemo(() => (current ? new BackendClient(current) : null), [current])
  const value = useMemo(() => ({ status, client }), [status, client])
  return <BackendContext.Provider value={value}>{children}</BackendContext.Provider>
}
