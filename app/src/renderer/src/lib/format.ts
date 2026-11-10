/** 3725000 -> "01:02:05" */
export function formatClock(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000))
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  return [h, m, s].map((n) => String(n).padStart(2, '0')).join(':')
}

/** 3725000 -> "1 h 2 min", 95000 -> "1 min 35 s", 8000 -> "8 s" */
export function formatDuration(ms: number): string {
  const total = Math.round(ms / 1000)
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  if (h > 0) return `${h} h ${m} min`
  if (m > 0) return `${m} min ${s} s`
  return `${s} s`
}

const dayFormat = new Intl.DateTimeFormat('en-GB', {
  weekday: 'short',
  day: '2-digit',
  month: 'short'
})
const timeFormat = new Intl.DateTimeFormat('en-GB', { hour: '2-digit', minute: '2-digit' })

/** ISO timestamp -> { day: "Wed, 11 Nov", time: "14:05" } in local time. */
export function formatWhen(iso: string): { day: string; time: string } {
  const date = new Date(iso)
  return { day: dayFormat.format(date), time: timeFormat.format(date) }
}
