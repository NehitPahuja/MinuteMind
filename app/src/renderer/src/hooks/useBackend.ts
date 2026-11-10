import { useEffect, useState } from 'react'
import type { BackendStatus } from '@shared/backend'

/** Live status of the local Python service, pushed from the main process. */
export function useBackendStatus(): BackendStatus {
  const [status, setStatus] = useState<BackendStatus>({ state: 'starting' })

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

  return status
}
