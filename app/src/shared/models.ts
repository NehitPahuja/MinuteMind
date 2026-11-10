// Shapes returned by the Python backend's HTTP API and WebSocket.

export type MeetingStatus = 'recording' | 'paused' | 'completed' | 'recovered' | 'failed'

export interface Meeting {
  id: number
  title: string
  created_at: string
  ended_at: string | null
  duration_ms: number
  status: MeetingStatus
  mic_device: string | null
  system_device: string | null
  has_mic: boolean
  has_system: boolean
}

export interface AudioDevice {
  id: number
  name: string
  channels: number
  rate: number
  is_default: boolean
}

export interface AudioDevices {
  inputs: AudioDevice[]
  loopbacks: AudioDevice[]
}

export type RecordingState = 'idle' | 'recording' | 'paused'

export interface RecordingStatus {
  state: RecordingState
  meeting: Meeting | null
  elapsed_ms: number
  levels: { mic: number; system: number } | null
  errors: string[]
}

export type AudioTrack = 'mix' | 'mic' | 'system'
