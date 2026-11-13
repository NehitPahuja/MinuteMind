import { useEffect, useState } from 'react'
import type { Meeting, MinutesContent, MinutesReport } from '@shared/models'
import { useBackend, useTranscriptionJobs } from '../lib/contexts'
import { formatClock } from '../lib/format'

interface MinutesViewProps {
  meeting: Meeting
  onSeek: (ms: number) => void
  /** Called when minutes are requested, so the meeting list can refresh. */
  onRequested: () => void
}

/** The meeting's minutes: summary, decisions, action items and topics. */
export function MinutesView({ meeting, onSeek, onRequested }: MinutesViewProps): React.JSX.Element {
  const { client } = useBackend()
  const jobs = useTranscriptionJobs()
  const [report, setReport] = useState<MinutesReport | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState<string | null>(null)

  const job = jobs.current?.meeting_id === meeting.id ? jobs.current : null
  const writing = job !== null && job.stage.startsWith('minutes')
  const queued =
    jobs.queued.includes(meeting.id) &&
    meeting.transcript_status === 'done' &&
    meeting.minutes_status === 'queued'
  // Refetch whenever this meeting's minutes start, finish or change state.
  const fetchKey = writing ? 'writing' : `${meeting.minutes_status}-${meeting.transcript_status}`

  useEffect(() => {
    if (!client) return
    let active = true
    client.get<MinutesReport>(`/meetings/${meeting.id}/minutes`).then(
      (r) => {
        if (active) setReport(r)
      },
      (err: Error) => {
        if (active) setError(err.message)
      }
    )
    return () => {
      active = false
    }
  }, [client, meeting.id, fetchKey])

  const request = (): void => {
    if (!client) return
    setError(null)
    client
      .post(`/meetings/${meeting.id}/minutes`)
      .then(onRequested, (err: Error) => setError(err.message))
  }

  const exportMarkdown = async (): Promise<void> => {
    if (!client) return
    setError(null)
    setSaved(null)
    try {
      const text = await client.getText(`/meetings/${meeting.id}/minutes.md`)
      const path = await window.api.files.saveText(`${fileName(meeting.title)}.md`, text)
      if (path) setSaved(path)
    } catch (err) {
      setError((err as Error).message)
    }
  }

  // The transcript comes first; until then there is nothing to show here.
  if (meeting.transcript_status !== 'done') return <></>

  const status = writing
    ? 'running'
    : queued
      ? 'queued'
      : (report?.status ?? meeting.minutes_status)
  const minutes = report?.minutes ?? null

  return (
    <section aria-label="Minutes" className="space-y-4 pt-2">
      <div className="flex items-baseline justify-between gap-4">
        <p className="eyebrow">Minutes</p>
        {minutes && report && (
          <p className="font-mono text-[10px] uppercase tracking-wider text-paper-700">
            {report.model} · {report.device}
          </p>
        )}
      </div>

      {writing && job && <MinutesProgress stage={job.stage} progress={job.progress} />}

      {status === 'queued' && (
        <p className="text-sm text-paper-500">
          {jobs.paused
            ? 'Waits until the recording in progress stops.'
            : 'Waiting to write minutes…'}
        </p>
      )}

      {status === 'failed' && !writing && (
        <p className="text-sm text-signal">
          Writing minutes failed{report?.error ? `: ${report.error}` : '.'}
        </p>
      )}

      {minutes && !writing ? (
        <MinutesBody minutes={minutes} onSeek={onSeek} />
      ) : (
        status === 'none' && (
          <p className="text-sm text-paper-500">
            No minutes yet. The local AI writes a summary, decisions and action items from the
            transcript.
          </p>
        )
      )}

      {!writing && status !== 'queued' && (
        <div className="flex flex-wrap items-center gap-5">
          <ActionButton onClick={request}>
            {minutes ? 'Write again' : status === 'failed' ? 'Try again' : 'Write minutes'}
          </ActionButton>
          {minutes && (
            <ActionButton onClick={() => void exportMarkdown()}>Export Markdown…</ActionButton>
          )}
          {saved && <span className="text-xs text-paper-500">Saved to {saved}</span>}
        </div>
      )}

      {error && <p className="text-xs text-signal">{error}</p>}
    </section>
  )
}

function MinutesBody({
  minutes,
  onSeek
}: {
  minutes: MinutesContent
  onSeek: (ms: number) => void
}): React.JSX.Element {
  return (
    <div className="space-y-5 rounded-md border border-line bg-ink-850/60 p-4">
      {minutes.summary && (
        <p className="text-[15px] leading-relaxed text-paper-100">{minutes.summary}</p>
      )}

      <Block title="Decisions" empty="No decisions were recorded.">
        {minutes.decisions.length > 0 && (
          <ul className="space-y-1.5">
            {minutes.decisions.map((d, i) => (
              <li key={i} className="flex gap-3 text-sm leading-relaxed text-paper-100">
                <span className="text-paper-500" aria-hidden="true">
                  —
                </span>
                <span>{d}</span>
              </li>
            ))}
          </ul>
        )}
      </Block>

      <Block title="Action items" empty="No action items.">
        {minutes.action_items.length > 0 && (
          <ul className="divide-y divide-line border-y border-line">
            {minutes.action_items.map((item, i) => (
              <li key={i} className="grid grid-cols-[1fr_auto] gap-4 py-2 text-sm">
                <span className="leading-relaxed text-paper-100">{item.task}</span>
                <span className="text-right font-mono text-[11px] uppercase tracking-wider text-paper-500">
                  {item.owner ?? 'No owner'}
                  {item.due && <span className="block normal-case text-paper-300">{item.due}</span>}
                </span>
              </li>
            ))}
          </ul>
        )}
      </Block>

      {minutes.topics.length > 0 && (
        <Block title="Topics">
          <ol className="space-y-1">
            {minutes.topics.map((t, i) => (
              <li key={i} className="grid grid-cols-[52px_1fr] gap-4 text-sm">
                {t.start_ms !== null ? (
                  <button
                    type="button"
                    onClick={() => onSeek(t.start_ms ?? 0)}
                    aria-label={`Play from ${formatClock(t.start_ms)}`}
                    className="text-left font-mono text-[11px] tabular-nums text-paper-500 hover:text-paper-100"
                  >
                    {formatClock(t.start_ms).replace(/^00:/, '')}
                  </button>
                ) : (
                  <span />
                )}
                <span className="text-paper-300">{t.title}</span>
              </li>
            ))}
          </ol>
        </Block>
      )}
    </div>
  )
}

function Block({
  title,
  empty,
  children
}: {
  title: string
  empty?: string
  children: React.ReactNode
}): React.JSX.Element {
  return (
    <div className="space-y-2">
      <p className="font-mono text-[11px] uppercase tracking-wider text-paper-500">{title}</p>
      {children || <p className="text-sm text-paper-700">{empty}</p>}
    </div>
  )
}

function MinutesProgress({
  stage,
  progress
}: {
  stage: string
  progress: number
}): React.JSX.Element {
  const label =
    stage === 'minutes-download'
      ? 'Downloading the language model (one time only, about 2 GB)'
      : 'Writing minutes'
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

/** A meeting title as a safe file name. */
function fileName(title: string): string {
  return title.replace(/[^\w .,()'&-]/g, '').trim() || 'minutes'
}
