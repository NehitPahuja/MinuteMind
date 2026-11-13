// Shapes returned by the Python backend's HTTP API and WebSocket.

export type MeetingStatus = 'recording' | 'paused' | 'completed' | 'recovered' | 'failed'

export type TranscriptStatus = 'none' | 'queued' | 'running' | 'done' | 'failed'

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
  transcript_status: TranscriptStatus
  transcript_error: string | null
  transcript_model: string | null
  transcript_device: string | null
  speakers_status: 'none' | 'done' | 'failed'
  speakers_error: string | null
  minutes_status: MinutesStatus
  minutes_error: string | null
}

export type MinutesStatus = 'none' | 'queued' | 'running' | 'done' | 'failed'

export interface ActionItem {
  task: string
  owner: string | null
  due: string | null
}

export interface MinutesContent {
  summary: string
  decisions: string[]
  action_items: ActionItem[]
  /** start_ms is where the topic begins, when the model gave a usable time. */
  topics: { title: string; start_ms: number | null }[]
}

export interface MinutesReport {
  status: MinutesStatus
  error: string | null
  minutes: MinutesContent | null
  model: string | null
  device: string | null
  created_at: string | null
}

export interface TranscriptSegment {
  id: number
  /** 'mic' is you, 'system' is the other people on the call. */
  source: 'mic' | 'system'
  /** For PC sound: which person ('S1', 'S2', ...), once speakers are detected. */
  speaker: string | null
  start_ms: number
  end_ms: number
  text: string
}

export interface Speaker {
  label: string
  display_name: string
  lines: number
}

export interface Transcript {
  status: TranscriptStatus
  error: string | null
  model: string | null
  device: string | null
  speakers_status: 'none' | 'done' | 'failed'
  speakers_error: string | null
  speakers: Speaker[]
  segments: TranscriptSegment[]
}

export interface TranscriptionJobs {
  current: {
    meeting_id: number
    kind: 'full' | 'speakers' | 'minutes'
    stage: 'starting' | 'downloading' | 'transcribing' | 'speakers' | 'minutes-download' | 'minutes'
    progress: number
  } | null
  queued: number[]
  /** True while recording: final transcripts wait for the GPU. */
  paused?: boolean
}

export interface HardwareReport {
  info: {
    gpu_name: string | null
    vram_total_mb: number | null
    vram_free_mb: number | null
    cuda_available: boolean
    ram_total_mb: number
    cpu_threads: number
  }
  profile: { name: 'gpu' | 'small_gpu' | 'cpu'; device: string; label: string }
  model: { name: string; downloaded: boolean }
  llm: { name: string; local: boolean; downloaded: boolean }
}

export interface Settings {
  processing: 'auto' | 'gpu' | 'cpu'
  auto_transcribe: boolean
  live_transcript: boolean
  speaker_labels: boolean
  auto_minutes: boolean
}

export interface Note {
  id: number
  meeting_id: number
  /** Moment in the meeting the note refers to. */
  ts_ms: number
  text: string
  created_at: string
  updated_at: string
}

export interface LiveLine {
  id: number
  source: 'mic' | 'system'
  start_ms: number
  end_ms: number
  text: string
}

export type LiveState = 'off' | 'starting' | 'waiting' | 'loading' | 'listening' | 'error'

export interface LiveTranscript {
  meetingId: number | null
  state: LiveState
  detail: string | null
  /** Sorted by start time. */
  lines: LiveLine[]
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
