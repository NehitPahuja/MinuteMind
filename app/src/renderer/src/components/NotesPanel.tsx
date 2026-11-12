import { useEffect, useState } from 'react'
import type { Note } from '@shared/models'
import { useBackend } from '../lib/contexts'
import { formatClock } from '../lib/format'

/** Notes typed during a recording; each is stamped with the recording time. */
export function NotesPanel({ meetingId }: { meetingId: number }): React.JSX.Element {
  const { client } = useBackend()
  const [notes, setNotes] = useState<Note[]>([])
  const [draft, setDraft] = useState('')
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!client) return
    let active = true
    client.get<Note[]>(`/meetings/${meetingId}/notes`).then(
      (list) => {
        if (active) setNotes(list)
      },
      (err: Error) => {
        if (active) setError(err.message)
      }
    )
    return () => {
      active = false
    }
  }, [client, meetingId])

  const add = (): void => {
    const text = draft.trim()
    if (!client || !text) return
    setDraft('')
    client.post<Note>(`/meetings/${meetingId}/notes`, { text }).then(
      (note) => {
        setNotes((list) => [...list, note].sort((a, b) => a.ts_ms - b.ts_ms || a.id - b.id))
        setError(null)
      },
      (err: Error) => {
        setDraft(text)
        setError(err.message)
      }
    )
  }

  const remove = (id: number): void => {
    if (!client) return
    client.delete(`/notes/${id}`).then(
      () => setNotes((list) => list.filter((n) => n.id !== id)),
      (err: Error) => setError(err.message)
    )
  }

  return (
    <div className="flex min-h-64 flex-col rounded-lg border border-line bg-ink-900 p-4">
      <p className="eyebrow mb-3">Notes</p>

      <textarea
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault()
            add()
          }
        }}
        rows={2}
        maxLength={5000}
        placeholder="Type a note, press Enter"
        aria-label="New note"
        className="w-full resize-none rounded-md border border-line-strong bg-ink-850 px-2.5 py-2 text-sm text-paper-100 placeholder:text-paper-700 focus:border-paper-500 focus:outline-none"
      />
      <p className="mt-1.5 text-[11px] text-paper-700">
        Stamped with the current time. Shift+Enter for a new line.
      </p>

      {error && <p className="mt-2 text-xs text-signal">{error}</p>}

      <ol className="mt-3 flex-1 space-y-2 overflow-y-auto">
        {notes.map((note) => (
          <li key={note.id} className="group grid grid-cols-[44px_1fr_auto] gap-2 text-sm">
            <span className="pt-0.5 font-mono text-[11px] tabular-nums text-paper-500">
              {formatClock(note.ts_ms).replace(/^00:/, '')}
            </span>
            <span className="whitespace-pre-wrap leading-relaxed text-paper-100">{note.text}</span>
            <button
              type="button"
              onClick={() => remove(note.id)}
              aria-label="Delete note"
              className="h-5 px-1 font-mono text-[11px] text-paper-700 opacity-0 hover:text-paper-100 focus:opacity-100 group-hover:opacity-100"
            >
              ×
            </button>
          </li>
        ))}
      </ol>
    </div>
  )
}
