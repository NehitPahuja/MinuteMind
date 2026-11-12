import { createContext, useContext } from 'react'
import type { BackendStatus } from '@shared/backend'
import type { RecordingStatus, TranscriptionJobs } from '@shared/models'
import type { BackendClient } from './client'

// -- backend connection -------------------------------------------------------

export interface BackendContextValue {
  status: BackendStatus
  /** null until the backend is ready (and again while it restarts). */
  client: BackendClient | null
}

export const BackendContext = createContext<BackendContextValue>({
  status: { state: 'starting' },
  client: null
})

export function useBackend(): BackendContextValue {
  return useContext(BackendContext)
}

// -- live recording -----------------------------------------------------------

export const LEVEL_HISTORY = 40

export interface LevelHistory {
  mic: number[]
  system: number[]
}

export interface RecordingContextValue {
  status: RecordingStatus
  /** Recent level readings for the meters, newest last. */
  history: LevelHistory
  /** Background transcription: the job running now and the queue. */
  transcription: TranscriptionJobs
}

export const NO_JOBS: TranscriptionJobs = { current: null, queued: [] }

export const IDLE_RECORDING: RecordingStatus = {
  state: 'idle',
  meeting: null,
  elapsed_ms: 0,
  levels: null,
  errors: []
}

export const EMPTY_HISTORY: LevelHistory = { mic: [], system: [] }

export const RecordingContext = createContext<RecordingContextValue>({
  status: IDLE_RECORDING,
  history: EMPTY_HISTORY,
  transcription: NO_JOBS
})

export function useRecording(): RecordingStatus {
  return useContext(RecordingContext).status
}

export function useLevelHistory(): LevelHistory {
  return useContext(RecordingContext).history
}

export function useTranscriptionJobs(): TranscriptionJobs {
  return useContext(RecordingContext).transcription
}
