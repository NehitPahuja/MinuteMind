import { useEffect, useRef, useState } from 'react'
import type { Meeting, Transcript, TranscriptSegment } from '@shared/models'
import { useBackend, useTranscriptionJobs } from '../lib/contexts'
import { formatClock } from '../lib/format'

const SPEAKER: Record<TranscriptSegment['source'], string> = { mic: 'You', system: 'Others' }

interface TranscriptViewProps {
  meeting: Meeting
  /** Playback position, to highlight the line being heard. */
  positionMs: number
  onSeek: (ms: number) => void
  /** Called when a transcript starts, so the meeting list can refresh. */
  onRequested: () => void
}

export function TranscriptView({
  meeting,
  positionMs,
  onSeek,
  onRequested
}: TranscriptViewProps): React.JSX.Element {
  const { client } = useBackend()
  const jobs = useTranscriptionJobs()
  const [transcript, setTranscript] = useState<Transcript | null>(null)
  const [error, setError] = useState<string | null>(null)

  const job = jobs.current?.meeting_id === meeting.id ? jobs.current : null
  const queuedAt = jobs.queued.indexOf(meeting.id)
  // Refetch whenever this meeting's job starts, moves on or finishes.
  const jobKey = job ? 'running' : queuedAt >= 0 ? 'queued' : meeting.transcript_status

  useEffect(() => {
    if (!client) return
    let active = true
    client.get<Transcript>(`/meetings/${meeting.id}/transcript`).then(
      (t) => {
        if (active) setTranscript(t)
      },
      (err: Error) => {
        if (active) setError(err.message)
      }
    )
    return () => {
      active = false
    }
  }, [client, meeting.id, jobKey])

  const request = (): void => {
    if (!client) return
    setError(null)
    client
      .post(`/meetings/${meeting.id}/transcribe`)
      .then(onRequested, (err: Error) => setError(err.message))
  }

  const status = job ? 'running' : queuedAt >= 0 ? 'queued' : transcript?.status

  return (
    <section aria-label="Transcript" className="space-y-4 pt-2">
      <div className="flex items-baseline justify-between gap-4">
        <p className="eyebrow">Transcript</p>
        {status === 'done' && transcript && (
          <p className="font-mono text-[10px] uppercase tracking-wider text-paper-700">
            {transcript.model} · {transcript.device}
          </p>
        )}
      </div>

      {status === 'running' && job && <JobProgress stage={job.stage} progress={job.progress} />}

      {status === 'queued' && (
        <p className="text-sm text-paper-500">
          Waiting to transcribe
          {queuedAt > 0 ? `, ${queuedAt} ${queuedAt === 1 ? 'meeting' : 'meetings'} ahead` : ''}…
        </p>
      )}

      {(status === 'none' || status === undefined) && (
        <div className="flex items-center gap-4">
          <p className="text-sm text-paper-500">No transcript yet.</p>
          <ActionButton onClick={request}>Transcribe</ActionButton>
        </div>
      )}

      {status === 'failed' && (
        <div className="space-y-2">
          <p className="text-sm text-signal">
            Transcription failed{transcript?.error ? `: ${transcript.error}` : '.'}
          </p>
          <ActionButton onClick={request}>Try again</ActionButton>
        </div>
      )}

      {status === 'done' && transcript && (
        <>
          {transcript.segments.length === 0 ? (
            <p className="text-sm text-paper-500">No speech was found in this recording.</p>
          ) : (
            <Lines segments={transcript.segments} positionMs={positionMs} onSeek={onSeek} />
          )}
          <ActionButton onClick={request}>Transcribe again</ActionButton>
        </>
      )}

      {error && <p className="text-xs text-signal">{error}</p>}
    </section>
  )
}

function JobProgress({ stage, progress }: { stage: string; progress: number }): React.JSX.Element {
  const label =
    stage === 'downloading'
      ? 'Downloading the speech model (one time only, about 1.5 GB)'
      : stage === 'transcribing'
        ? 'Transcribing'
        : 'Preparing'
  const pct = Math.round(progress * 100)
  return (
    <div className="space-y-2" role="status">
      <div className="flex items-baseline justify-between text-sm">
        <span className="text-paper-300">{label}</span>
        <span className="font-mono text-xs tabular-nums text-paper-500">{pct}%</span>
      </div>
      <div className="h-0.5 w-full bg-line">
        <div className="h-full bg-paper-300 transition-[width]" style={{ width: `${pct}%` }} />
      </div>
    </div>
  )
}

interface LinesProps {
  segments: TranscriptSegment[]
  positionMs: number
  onSeek: (ms: number) => void
}

function Lines({ segments, positionMs, onSeek }: LinesProps): React.JSX.Element {
  // The line being heard: the last one that has started.
  let current = -1
  for (let i = 0; i < segments.length; i++) {
    if (segments[i].start_ms <= positionMs) current = i
    else break
  }
  const currentRef = useRef<HTMLButtonElement>(null)
  useEffect(() => {
    currentRef.current?.scrollIntoView({ block: 'nearest' })
  }, [current])

  return (
    <ol className="max-h-[28rem] overflow-y-auto border-y border-line">
      {segments.map((seg, i) => {
        const active = i === current && positionMs > 0
        const newSpeaker = i === 0 || segments[i - 1].source !== seg.source
        return (
          <li key={seg.id}>
            <button
              ref={i === current ? currentRef : undefined}
              type="button"
              onClick={() => onSeek(seg.start_ms)}
              className={`grid w-full grid-cols-[52px_64px_1fr] gap-4 px-2 py-2 text-left text-sm leading-relaxed transition-colors ${
                active ? 'bg-ink-800 text-paper-50' : 'text-paper-300 hover:bg-ink-850'
              }`}
            >
              <span className="pt-0.5 font-mono text-[11px] tabular-nums text-paper-500">
                {formatClock(seg.start_ms).replace(/^00:/, '')}
              </span>
              <span className="pt-0.5 font-mono text-[11px] uppercase tracking-wider text-paper-500">
                {newSpeaker ? SPEAKER[seg.source] : ''}
              </span>
              <span>{seg.text}</span>
            </button>
          </li>
        )
      })}
    </ol>
  )
}

function ActionButton({
  onClick,
  children
}: {
  onClick: () => void
  children: React.ReactNode
}): React.JSX.Element {
  return (
    <button
      type="button"
      onClick={onClick}
      className="font-mono text-[11px] uppercase tracking-wider text-paper-500 hover:text-paper-100"
    >
      {children}
    </button>
  )
}
