import { useEffect, useRef, useState } from 'react'
import type { AskEvent, AskSource, Meeting } from '@shared/models'
import { PageHeader } from '../components/PageHeader'
import { useBackend, useRecording } from '../lib/contexts'
import { formatClock, formatWhen } from '../lib/format'

const EXAMPLES = [
  'What did we decide about the launch date?',
  'Who owns the onboarding videos, and when are they due?',
  'What number was quoted for hosting?'
]

const STATUS_TEXT: Record<string, string> = {
  searching: 'Searching your meetings…',
  queued: 'Waiting for the previous question…',
  waiting: 'Waiting for a transcription to finish…',
  loading: 'Loading the AI model…',
  answering: 'Answering…'
}

interface SearchStatus {
  current: number | null
  queued: number[]
  embeddings: 'unknown' | 'ready' | 'unavailable'
  embeddings_error: string | null
}

interface Exchange {
  id: number
  question: string
  scope: string
  status: string | null
  text: string
  sources: AskSource[]
  cited: number[]
  notFound: boolean
  done: boolean
  error: string | null
}

interface AskPageProps {
  /** Open a meeting at a moment (a citation was clicked). */
  onOpen: (meetingId: number, ms: number | null) => void
}

export function AskPage({ onOpen }: AskPageProps): React.JSX.Element {
  const { client } = useBackend()
  const recording = useRecording()
  const [meetings, setMeetings] = useState<Meeting[]>([])
  const [scope, setScope] = useState('all')
  const [question, setQuestion] = useState('')
  const [exchanges, setExchanges] = useState<Exchange[]>([])
  const [indexing, setIndexing] = useState<SearchStatus | null>(null)
  const abort = useRef<AbortController | null>(null)
  const nextId = useRef(1)

  const busy = exchanges.some((e) => !e.done)
  const recordingNow = recording.state !== 'idle'

  useEffect(() => {
    if (!client) return
    let active = true
    client.get<Meeting[]>('/meetings').then(
      (list) => {
        if (active) setMeetings(list.filter((m) => m.transcript_status === 'done'))
      },
      () => undefined
    )
    client.get<SearchStatus>('/search/status').then(
      (s) => {
        if (active) setIndexing(s)
      },
      () => undefined
    )
    return () => {
      active = false
      abort.current?.abort()
    }
  }, [client])

  const update = (id: number, change: (e: Exchange) => Partial<Exchange>): void =>
    setExchanges((list) => list.map((e) => (e.id === id ? { ...e, ...change(e) } : e)))

  const ask = async (text: string): Promise<void> => {
    const q = text.trim()
    if (!client || !q || busy) return
    const id = nextId.current++
    const scopeLabel =
      scope === 'all'
        ? 'All meetings'
        : (meetings.find((m) => String(m.id) === scope)?.title ?? 'One meeting')
    setExchanges((list) => [
      {
        id,
        question: q,
        scope: scopeLabel,
        status: 'searching',
        text: '',
        sources: [],
        cited: [],
        notFound: false,
        done: false,
        error: null
      },
      ...list
    ])
    setQuestion('')
    const controller = new AbortController()
    abort.current = controller
    try {
      await client.stream<AskEvent>(
        '/ask',
        { question: q, meeting_id: scope === 'all' ? null : Number(scope) },
        (event) => {
          if (event.type === 'sources') update(id, () => ({ sources: event.sources }))
          else if (event.type === 'status') update(id, () => ({ status: event.state }))
          else if (event.type === 'token') update(id, (e) => ({ text: e.text + event.text }))
          else if (event.type === 'done')
            update(id, () => ({
              text: event.answer,
              sources: event.sources,
              cited: event.cited,
              notFound: event.not_found,
              status: null,
              done: true
            }))
          else if (event.type === 'error')
            update(id, () => ({ error: event.message, status: null, done: true }))
        },
        controller.signal
      )
      update(id, (e) => (e.done ? {} : { done: true, status: null }))
    } catch (err) {
      const stopped = (err as Error).name === 'AbortError'
      update(id, () => ({
        done: true,
        status: null,
        error: stopped ? 'Stopped.' : (err as Error).message
      }))
    }
  }

  return (
    <>
      <PageHeader
        eyebrow="Recall"
        title={
          <>
            Ask your <span className="italic">meetings</span>
          </>
        }
        lede="Answers come only from what was said and typed in your meetings, with sources you can click to hear the moment."
      />

      <form
        onSubmit={(e) => {
          e.preventDefault()
          void ask(question)
        }}
        className="rounded-lg border border-line bg-ink-900"
      >
        <div className="flex items-center gap-3 px-4 py-3.5">
          <span className="font-mono text-paper-700" aria-hidden="true">
            ?
          </span>
          <input
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            disabled={!client || recordingNow}
            maxLength={1000}
            placeholder="Ask anything about your recordings…"
            aria-label="Question"
            className="flex-1 bg-transparent text-[15px] text-paper-100 placeholder:text-paper-500 focus:outline-none disabled:opacity-50"
          />
          {busy ? (
            <button
              type="button"
              onClick={() => abort.current?.abort()}
              className="rounded-md border border-line-strong px-3 py-1.5 font-mono text-[11px] uppercase tracking-wider text-paper-300 hover:text-paper-50"
            >
              Stop
            </button>
          ) : (
            <button
              type="submit"
              disabled={!client || recordingNow || !question.trim()}
              className="rounded-md bg-paper-100 px-3 py-1.5 text-sm font-medium text-ink-950 transition-colors hover:bg-paper-50 disabled:opacity-30"
            >
              Ask
            </button>
          )}
        </div>
        <div className="flex items-center gap-3 border-t border-line px-4 py-2">
          <label htmlFor="ask-scope" className="eyebrow">
            Search in
          </label>
          <select
            id="ask-scope"
            value={scope}
            onChange={(e) => setScope(e.target.value)}
            className="max-w-xs truncate rounded-sm bg-ink-900 py-0.5 text-sm text-paper-100 focus:outline-none"
          >
            <option value="all">All meetings</option>
            {meetings.map((m) => (
              <option key={m.id} value={String(m.id)}>
                {m.title} · {formatWhen(m.created_at).day}
              </option>
            ))}
          </select>
        </div>
      </form>

      <IndexNote recordingNow={recordingNow} status={indexing} />

      {exchanges.length === 0 ? (
        <>
          <p className="eyebrow mb-3 mt-10">For example</p>
          <ul className="space-y-1">
            {EXAMPLES.map((q) => (
              <li key={q}>
                <button
                  type="button"
                  onClick={() => setQuestion(q)}
                  className="display w-full border-b border-line py-3 text-left text-xl italic text-paper-300 hover:text-paper-50"
                >
                  {q}
                </button>
              </li>
            ))}
          </ul>
        </>
      ) : (
        <ol className="mt-10 space-y-12">
          {exchanges.map((e) => (
            <ExchangeView key={e.id} exchange={e} onOpen={onOpen} />
          ))}
        </ol>
      )}
    </>
  )
}

function IndexNote({
  recordingNow,
  status
}: {
  recordingNow: boolean
  status: SearchStatus | null
}): React.JSX.Element | null {
  let text: string | null = null
  if (recordingNow) text = 'Ask is available once the recording stops.'
  else if (status && (status.current !== null || status.queued.length > 0)) {
    const n = status.queued.length + (status.current !== null ? 1 : 0)
    text = `Indexing ${n} meeting${n === 1 ? '' : 's'}. Answers may miss ${n === 1 ? 'it' : 'them'} until that finishes.`
  } else if (status?.embeddings === 'unavailable')
    text = "The search model couldn't be downloaded, so only exact words are matched for now."
  if (!text) return null
  return (
    <p className="mt-3 font-mono text-[11px] uppercase tracking-wider text-paper-500">{text}</p>
  )
}

function ExchangeView({
  exchange: e,
  onOpen
}: {
  exchange: Exchange
  onOpen: (meetingId: number, ms: number | null) => void
}): React.JSX.Element {
  const byNumber = new Map(e.sources.map((s) => [s.n, s]))
  const open = (s: AskSource): void => onOpen(s.meeting_id, s.at_ms)
  const cited = e.sources.filter((s) => e.cited.includes(s.n))
  const others = e.sources.filter((s) => !e.cited.includes(s.n))

  return (
    <li>
      <p className="eyebrow mb-2">{e.scope}</p>
      <h2 className="display text-2xl text-paper-50">{e.question}</h2>

      <div className="mt-4" aria-live="polite">
        {e.error ? (
          <p className="text-sm text-signal" role="alert">
            {e.error}
          </p>
        ) : e.notFound ? (
          <p className="display text-xl italic text-paper-300">
            Not found in the recordings.
            <span className="mt-1 block font-sans text-sm not-italic text-paper-500">
              {e.scope === 'All meetings'
                ? 'Nothing said or typed in your meetings answers that. Try other words.'
                : 'Nothing said or typed in this meeting answers that. Try other words, or search all meetings.'}
            </span>
          </p>
        ) : (
          <p className="text-[17px] leading-relaxed text-paper-100">
            <AnswerText
              text={e.text}
              sources={byNumber}
              onCite={(n) => {
                const s = byNumber.get(n)
                if (s) open(s)
              }}
            />
          </p>
        )}
        {e.status && (
          <p className="mt-3 font-mono text-[11px] uppercase tracking-wider text-paper-500">
            {STATUS_TEXT[e.status] ?? e.status}
          </p>
        )}
      </div>

      {e.done && !e.error && !e.notFound && cited.length > 0 && (
        <SourceList title="Sources" sources={cited} onOpen={open} />
      )}
      {e.done && !e.error && others.length > 0 && (
        <details className="mt-4">
          <summary className="eyebrow cursor-pointer hover:text-paper-100">
            {e.notFound ? 'Closest passages searched' : 'Also searched'} ({others.length})
          </summary>
          <SourceList sources={others} onOpen={open} />
        </details>
      )}
    </li>
  )
}

/** "00:00:45", "Minutes · 00:00:45" (the line behind a minutes item) or "Minutes". */
function where(s: AskSource): string {
  const time = s.at_ms === null ? null : formatClock(s.at_ms)
  if (s.kind !== 'minutes') return time ?? ''
  return time ? `Minutes · ${time}` : 'Minutes'
}

/** The answer, with [n] turned into buttons that open the source. */
function AnswerText({
  text,
  sources,
  onCite
}: {
  text: string
  sources: Map<number, AskSource>
  onCite: (n: number) => void
}): React.JSX.Element {
  const parts = text.split(/(\[\d+\])/g)
  return (
    <>
      {parts.map((part, i) => {
        const match = /^\[(\d+)\]$/.exec(part)
        if (!match) return <span key={i}>{part}</span>
        const n = Number(match[1])
        const s = sources.get(n)
        if (!s) return null
        return (
          <button
            key={i}
            type="button"
            onClick={() => onCite(n)}
            title={`${s.meeting_title}, ${where(s)}`}
            className="mx-0.5 inline-flex -translate-y-0.5 items-center rounded-sm border border-line-strong px-1.5 font-mono text-[11px] text-paper-300 hover:border-paper-500 hover:text-paper-50"
          >
            {n}
            {s.at_ms === null ? '' : ` · ${formatClock(s.at_ms)}`}
          </button>
        )
      })}
    </>
  )
}

function SourceList({
  title,
  sources,
  onOpen
}: {
  title?: string
  sources: AskSource[]
  onOpen: (s: AskSource) => void
}): React.JSX.Element {
  return (
    <div className="mt-6">
      {title && <p className="eyebrow mb-2">{title}</p>}
      <ul className="border-t border-line">
        {sources.map((s) => (
          <li key={s.n} className="border-b border-line">
            <button
              type="button"
              onClick={() => onOpen(s)}
              className="grid w-full grid-cols-[28px_1fr] gap-3 px-1 py-3 text-left hover:bg-ink-900/60"
            >
              <span className="font-mono text-xs text-paper-500">[{s.n}]</span>
              <span className="min-w-0">
                <span className="block font-mono text-[11px] uppercase tracking-wider text-paper-500">
                  {s.meeting_title} · {formatWhen(s.meeting_created_at).day} · {where(s)}
                </span>
                {s.quote ? (
                  <span className="display mt-1 block text-[17px] italic leading-relaxed text-paper-100">
                    “{s.quote}”
                  </span>
                ) : (
                  <span className="mt-1 line-clamp-3 whitespace-pre-line text-sm leading-relaxed text-paper-300">
                    {s.text}
                  </span>
                )}
              </span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  )
}
