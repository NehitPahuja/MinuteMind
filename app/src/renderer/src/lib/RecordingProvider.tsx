import { useEffect, useMemo, useState } from 'react'
import type {
  LiveLine,
  LiveState as LiveTranscriptState,
  LiveTranscript,
  RecordingStatus,
  TranscriptionJobs
} from '@shared/models'
import {
  EMPTY_HISTORY,
  IDLE_RECORDING,
  LEVEL_HISTORY,
  NO_JOBS,
  NO_LIVE,
  RecordingContext,
  useBackend,
  type LevelHistory
} from './contexts'

interface LiveState {
  status: RecordingStatus
  history: LevelHistory
  transcription: TranscriptionJobs
  live: LiveTranscript
}

const INITIAL: LiveState = {
  status: IDLE_RECORDING,
  history: EMPTY_HISTORY,
  transcription: NO_JOBS,
  live: NO_LIVE
}

interface LiveMessage {
  meeting_id: number | null
  state: LiveTranscriptState
  detail: string | null
  reset: boolean
  lines: LiveLine[]
}

/**
 * Keeps one WebSocket open to the backend and shares the live recording
 * status (state, timer, level history) with the whole app, so the sidebar and
 * the Record page stay in sync even when you switch pages mid-recording.
 */
export function RecordingProvider({ children }: { children: React.ReactNode }): React.JSX.Element {
  const { client } = useBackend()
  const [live, setLive] = useState<LiveState>(INITIAL)

  useEffect(() => {
    if (!client) return
    let ws: WebSocket | null = null
    let retry: ReturnType<typeof setTimeout> | undefined
    let closed = false

    const connect = (): void => {
      ws = new WebSocket(client.wsUrl('/ws'))
      ws.onmessage = (event) => {
        const { type, ...payload } = JSON.parse(event.data as string) as { type: string }
        if (type === 'recording') {
          setLive((prev) => next(prev, payload as RecordingStatus))
        } else if (type === 'live') {
          const msg = payload as LiveMessage
          setLive((prev) => {
            const same =
              !msg.reset &&
              msg.lines.length === 0 &&
              prev.live.state === msg.state &&
              prev.live.detail === msg.detail
            if (same) return prev
            const base = msg.reset ? [] : prev.live.lines
            const lines = msg.lines.length
              ? [...base, ...msg.lines].sort((a, b) => a.start_ms - b.start_ms || a.id - b.id)
              : base
            return {
              ...prev,
              live: { meetingId: msg.meeting_id, state: msg.state, detail: msg.detail, lines }
            }
          })
        } else if (type === 'transcription') {
          const jobs = payload as TranscriptionJobs
          // Sent many times a second; only re-render when something changed.
          setLive((prev) =>
            JSON.stringify(prev.transcription) === JSON.stringify(jobs)
              ? prev
              : { ...prev, transcription: jobs }
          )
        }
      }
      ws.onclose = () => {
        if (!closed) retry = setTimeout(connect, 1000)
      }
    }
    connect()

    return () => {
      closed = true
      clearTimeout(retry)
      ws?.close()
    }
  }, [client])

  // While the backend is down, report idle rather than a stale recording.
  const value = useMemo(() => (client ? live : INITIAL), [client, live])
  return <RecordingContext.Provider value={value}>{children}</RecordingContext.Provider>
}

function next(prev: LiveState, status: RecordingStatus): LiveState {
  const newMeeting = status.meeting?.id !== prev.status.meeting?.id
  if (!status.levels) return { ...prev, status, history: EMPTY_HISTORY }
  const base = newMeeting ? EMPTY_HISTORY : prev.history
  return {
    ...prev,
    status,
    history: {
      mic: [...base.mic, status.levels.mic].slice(-LEVEL_HISTORY),
      system: [...base.system, status.levels.system].slice(-LEVEL_HISTORY)
    }
  }
}
