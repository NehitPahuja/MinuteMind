import { useEffect, useRef, useState } from 'react'
import type { AudioTrack, Meeting } from '@shared/models'
import { AudioPlayer, type PlayerHandle } from '../components/AudioPlayer'
import { PageHeader } from '../components/PageHeader'
import { TranscriptView } from '../components/TranscriptView'
import { useBackend, useRecording, useTranscriptionJobs } from '../lib/contexts'
import { formatDuration, formatWhen } from '../lib/format'

const today = new Intl.DateTimeFormat('en-GB', {
  weekday: 'short',
  day: '2-digit',
  month: 'short',
  year: 'numeric'
}).format(new Date())

interface MeetingsPageProps {
  selectedId: number | null
  onSelect: (id: number | null) => void
  onRecord: () => void
}

export function MeetingsPage({
  selectedId,
  onSelect,
  onRecord
}: MeetingsPageProps): React.JSX.Element {
  const { client } = useBackend()
  const recording = useRecording()
  const jobs = useTranscriptionJobs()
  const [meetings, setMeetings] = useState<Meeting[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [refreshKey, setRefreshKey] = useState(0)

  // Reload when the backend connects, a recording starts or stops, or a
  // transcription job starts or finishes.
  const recordingState = recording.state === 'idle' ? 'idle' : 'active'
  const jobsKey = `${jobs.current?.meeting_id ?? ''}|${jobs.queued.join(',')}`
  useEffect(() => {
    if (!client) return
    let active = true
    client.get<Meeting[]>('/meetings').then(
      (list) => {
        if (!active) return
        setMeetings(list)
        setError(null)
      },
      (err: Error) => {
        if (active) setError(err.message)
      }
    )
    return () => {
      active = false
    }
  }, [client, recordingState, jobsKey, refreshKey])

  const update = (meeting: Meeting): void =>
    setMeetings((list) => list?.map((m) => (m.id === meeting.id ? meeting : m)) ?? null)

  const remove = (id: number): void => {
    setMeetings((list) => list?.filter((m) => m.id !== id) ?? null)
    if (selectedId === id) onSelect(null)
  }

  return (
    <>
      <PageHeader
        eyebrow="Library"
        title="Your meetings"
        lede="Every recording lands here and is transcribed automatically. Click any line of a transcript to hear it."
        aside={<p className="font-mono text-xs uppercase tracking-wider text-paper-500">{today}</p>}
      />

      {error && (
        <p className="mb-6 text-sm text-signal" role="alert">
          {error}
        </p>
      )}

      {meetings === null ? (
        <p className="font-mono text-xs uppercase tracking-wider text-paper-500">Loading…</p>
      ) : meetings.length === 0 ? (
        <EmptyState onRecord={onRecord} />
      ) : (
        <ol className="border-t border-line">
          {meetings.map((m) => (
            <MeetingRow
              key={m.id}
              meeting={m}
              open={m.id === selectedId}
              live={recording.meeting?.id === m.id}
              onToggle={() => onSelect(m.id === selectedId ? null : m.id)}
              onChanged={update}
              onDeleted={() => remove(m.id)}
              onRefresh={() => setRefreshKey((k) => k + 1)}
            />
          ))}
        </ol>
      )}
    </>
  )
}

function EmptyState({ onRecord }: { onRecord: () => void }): React.JSX.Element {
  return (
    <section className="rounded-lg border border-dashed border-line-strong bg-ink-900/70 px-8 py-14 text-center">
      <p className="display text-3xl italic text-paper-100">Nothing recorded yet.</p>
      <p className="mx-auto mt-3 max-w-md text-sm leading-relaxed text-paper-500">
        Start a recording before your next call. MinuteMind captures your microphone and the other
        side of the call, all on this PC.
      </p>
      <button
        type="button"
        onClick={onRecord}
        className="mt-7 inline-flex items-center gap-2 rounded-md bg-paper-100 px-4 py-2 text-sm font-medium text-ink-950 transition-colors hover:bg-paper-50"
      >
        <span className="h-2 w-2 rounded-full bg-signal" aria-hidden="true" />
        New recording
      </button>
    </section>
  )
}

const STATUS_NOTE: Partial<Record<Meeting['status'], { text: string; className: string }>> = {
  recording: { text: 'Recording', className: 'text-signal' },
  paused: { text: 'Paused', className: 'text-paper-300' },
  recovered: { text: 'Recovered after a crash', className: 'text-paper-300' },
  failed: { text: 'Failed', className: 'text-signal' }
}

function transcriptNote(
  meeting: Meeting,
  jobs: ReturnType<typeof useTranscriptionJobs>
): { text: string; className: string } | undefined {
  const job = jobs.current?.meeting_id === meeting.id ? jobs.current : null
  if (job) {
    const pct = Math.round(job.progress * 100)
    const text =
      job.stage === 'downloading'
        ? `Downloading speech model ${pct}%`
        : job.stage === 'speakers'
          ? `Telling speakers apart ${pct}%`
          : `Transcribing ${pct}%`
    return { text, className: 'text-paper-300' }
  }
  if (jobs.queued.includes(meeting.id)) {
    return { text: 'Waiting to transcribe', className: 'text-paper-500' }
  }
  if (meeting.transcript_status === 'failed') {
    return { text: 'Transcript failed', className: 'text-signal' }
  }
  return undefined
}

interface MeetingRowProps {
  meeting: Meeting
  open: boolean
  live: boolean
  onToggle: () => void
  onChanged: (meeting: Meeting) => void
  onDeleted: () => void
  onRefresh: () => void
}

function MeetingRow({
  meeting,
  open,
  live,
  onToggle,
  onChanged,
  onDeleted,
  onRefresh
}: MeetingRowProps): React.JSX.Element {
  const when = formatWhen(meeting.created_at)
  const jobs = useTranscriptionJobs()
  const note = STATUS_NOTE[meeting.status] ?? transcriptNote(meeting, jobs)

  return (
    <li className={`border-b border-line ${open ? 'bg-ink-900/80' : ''}`}>
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={open}
        className="grid w-full grid-cols-[110px_1fr_auto] items-baseline gap-6 px-3 py-4 text-left hover:bg-ink-900/60"
      >
        <span className="font-mono text-xs uppercase tracking-wide text-paper-500">
          {when.day}
          <span className="block text-paper-700">{when.time}</span>
        </span>
        <span className="min-w-0">
          <span className="display block truncate text-xl text-paper-50">{meeting.title}</span>
          {note && (
            <span
              className={`mt-1 block font-mono text-[11px] uppercase tracking-wider ${note.className}`}
            >
              {note.text}
            </span>
          )}
        </span>
        <span className="font-mono text-xs tabular-nums text-paper-300">
          {live ? '—' : formatDuration(meeting.duration_ms)}
        </span>
      </button>

      {open && !live && (
        <MeetingDetail
          meeting={meeting}
          onChanged={onChanged}
          onDeleted={onDeleted}
          onRefresh={onRefresh}
        />
      )}
      {open && live && (
        <p className="px-3 pb-5 pl-[134px] text-sm text-paper-500">
          Still recording. Playback is available once it stops.
        </p>
      )}
    </li>
  )
}

const TRACKS: { id: AudioTrack; label: string }[] = [
  { id: 'mix', label: 'Everyone' },
  { id: 'mic', label: 'You' },
  { id: 'system', label: 'Others' }
]

interface MeetingDetailProps {
  meeting: Meeting
  onChanged: (meeting: Meeting) => void
  onDeleted: () => void
  onRefresh: () => void
}

function MeetingDetail({
  meeting,
  onChanged,
  onDeleted,
  onRefresh
}: MeetingDetailProps): React.JSX.Element {
  const player = useRef<PlayerHandle>(null)
  const [positionMs, setPositionMs] = useState(0)
  const { client } = useBackend()
  const [track, setTrack] = useState<AudioTrack>('mix')
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(meeting.title)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const available: Record<AudioTrack, boolean> = {
    mix: meeting.has_mic || meeting.has_system,
    mic: meeting.has_mic,
    system: meeting.has_system
  }

  const saveTitle = async (): Promise<void> => {
    const title = draft.trim()
    setEditing(false)
    if (!client || !title || title === meeting.title) return
    try {
      onChanged(await client.patch<Meeting>(`/meetings/${meeting.id}`, { title }))
    } catch (err) {
      setError((err as Error).message)
    }
  }

  const deleteMeeting = async (): Promise<void> => {
    if (!client) return
    try {
      await client.delete(`/meetings/${meeting.id}`)
      onDeleted()
    } catch (err) {
      setError((err as Error).message)
    }
  }

  return (
    <div className="space-y-4 px-3 pb-6 pl-[134px]">
      {editing ? (
        <input
          autoFocus
          value={draft}
          maxLength={200}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={() => void saveTitle()}
          onKeyDown={(e) => {
            if (e.key === 'Enter') void saveTitle()
            if (e.key === 'Escape') {
              setDraft(meeting.title)
              setEditing(false)
            }
          }}
          aria-label="Meeting title"
          className="w-full border-b border-paper-500 bg-transparent pb-1 text-lg text-paper-50 focus:outline-none"
        />
      ) : null}

      {available.mix ? (
        <>
          <div className="flex gap-1" role="tablist" aria-label="Track">
            {TRACKS.map((t) => (
              <button
                key={t.id}
                type="button"
                role="tab"
                aria-selected={track === t.id}
                disabled={!available[t.id]}
                onClick={() => setTrack(t.id)}
                className={`rounded-sm px-2.5 py-1 font-mono text-[11px] uppercase tracking-wider transition-colors disabled:opacity-30 ${
                  track === t.id
                    ? 'bg-ink-800 text-paper-50'
                    : 'text-paper-500 hover:text-paper-100'
                }`}
              >
                {t.label}
              </button>
            ))}
          </div>
          {client && (
            <AudioPlayer
              ref={player}
              src={client.audioUrl(meeting.id, track)}
              durationMs={meeting.duration_ms}
              onTime={setPositionMs}
            />
          )}
          <p className="text-xs text-paper-700">
            {[
              meeting.mic_device && `You: ${meeting.mic_device}`,
              meeting.system_device && `Others: ${meeting.system_device}`
            ]
              .filter(Boolean)
              .join(' · ')}
          </p>
          <TranscriptView
            meeting={meeting}
            positionMs={positionMs}
            onSeek={(ms) => player.current?.playFrom(ms)}
            onRequested={onRefresh}
          />
        </>
      ) : (
        <p className="text-sm text-paper-500">No audio was saved for this meeting.</p>
      )}

      <div className="flex items-center gap-5 pt-1">
        <button
          type="button"
          onClick={() => {
            setDraft(meeting.title)
            setEditing(true)
          }}
          className="font-mono text-[11px] uppercase tracking-wider text-paper-500 hover:text-paper-100"
        >
          Rename
        </button>
        {confirmDelete ? (
          <span className="flex items-center gap-3">
            <span className="text-xs text-paper-300">Delete this meeting and its audio?</span>
            <button
              type="button"
              onClick={() => void deleteMeeting()}
              className="font-mono text-[11px] uppercase tracking-wider text-signal hover:text-paper-50"
            >
              Delete
            </button>
            <button
              type="button"
              onClick={() => setConfirmDelete(false)}
              className="font-mono text-[11px] uppercase tracking-wider text-paper-500 hover:text-paper-100"
            >
              Keep
            </button>
          </span>
        ) : (
          <button
            type="button"
            onClick={() => setConfirmDelete(true)}
            className="font-mono text-[11px] uppercase tracking-wider text-paper-500 hover:text-signal"
          >
            Delete…
          </button>
        )}
        {error && <span className="text-xs text-signal">{error}</span>}
      </div>
    </div>
  )
}
