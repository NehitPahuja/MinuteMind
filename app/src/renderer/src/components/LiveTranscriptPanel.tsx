import { useEffect, useRef } from 'react'
import type { LiveTranscript } from '@shared/models'
import { formatClock } from '../lib/format'

const STATE_TEXT: Record<LiveTranscript['state'], string> = {
  off: 'Live transcript is off. You can turn it on in Settings.',
  starting: 'Starting…',
  waiting: 'Finishing another transcript first…',
  loading: 'Loading the live model…',
  listening: 'Listening. Lines appear about 10 seconds after they are spoken.',
  error: "The live transcript couldn't start. Recording is unaffected."
}

const SPEAKER = { mic: 'You', system: 'Others' } as const

/** Rough transcript of the meeting in progress, newest at the bottom. */
export function LiveTranscriptPanel({ live }: { live: LiveTranscript }): React.JSX.Element {
  const list = useRef<HTMLOListElement>(null)
  const stickToBottom = useRef(true)

  // Follow new lines unless the user has scrolled up to read.
  useEffect(() => {
    const el = list.current
    if (el && stickToBottom.current) el.scrollTop = el.scrollHeight
  }, [live.lines.length])

  return (
    <div className="flex min-h-64 flex-col rounded-lg border border-line bg-ink-900 p-4">
      <div className="mb-3 flex items-baseline justify-between gap-4">
        <p className="eyebrow">Live transcript</p>
        <p className="font-mono text-[10px] uppercase tracking-wider text-paper-700">
          Preview · final version after you stop
        </p>
      </div>

      {live.lines.length === 0 ? (
        <p className="text-sm text-paper-500" role="status">
          {live.detail ?? STATE_TEXT[live.state]}
        </p>
      ) : (
        <ol
          ref={list}
          onScroll={(e) => {
            const el = e.currentTarget
            stickToBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24
          }}
          className="max-h-80 flex-1 space-y-2 overflow-y-auto pr-1"
          aria-live="polite"
        >
          {live.lines.map((line, i) => {
            const newSpeaker = i === 0 || live.lines[i - 1].source !== line.source
            return (
              <li key={line.id} className="grid grid-cols-[44px_56px_1fr] gap-3 text-sm">
                <span className="pt-0.5 font-mono text-[11px] tabular-nums text-paper-700">
                  {formatClock(line.start_ms).replace(/^00:/, '')}
                </span>
                <span className="pt-0.5 font-mono text-[11px] uppercase tracking-wider text-paper-500">
                  {newSpeaker ? SPEAKER[line.source] : ''}
                </span>
                <span className="leading-relaxed text-paper-300">{line.text}</span>
              </li>
            )
          })}
        </ol>
      )}
    </div>
  )
}
