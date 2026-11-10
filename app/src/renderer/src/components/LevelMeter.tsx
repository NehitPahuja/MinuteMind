interface LevelMeterProps {
  label: string
  device: string | null
  history: number[]
  bars?: number
}

/** A scrolling bar meter: newest reading on the right. */
export function LevelMeter({
  label,
  device,
  history,
  bars = 32
}: LevelMeterProps): React.JSX.Element {
  const values = [...Array(Math.max(0, bars - history.length)).fill(0), ...history.slice(-bars)]
  const current = history.at(-1) ?? 0
  const off = device === null

  return (
    <div className={`rounded-lg border border-line bg-ink-900 p-4 ${off ? 'opacity-50' : ''}`}>
      <div className="mb-1 flex items-baseline justify-between gap-3">
        <p className="eyebrow">{label}</p>
        <span
          className={`font-mono text-[10px] ${current > 0.05 ? 'text-sage' : 'text-paper-700'}`}
          aria-hidden="true"
        >
          {off ? 'OFF' : current > 0.05 ? 'SIGNAL' : 'QUIET'}
        </span>
      </div>
      <p className="mb-3 truncate text-xs text-paper-500" title={device ?? undefined}>
        {device ?? 'Not recording this source'}
      </p>
      <div
        className="flex h-10 items-end gap-[3px]"
        role="meter"
        aria-label={`${label} level`}
        aria-valuemin={0}
        aria-valuemax={1}
        aria-valuenow={Number(current.toFixed(2))}
      >
        {values.map((v, i) => (
          <span
            key={i}
            className={`w-full rounded-[1px] transition-[height] duration-75 ${
              v > 0.85 ? 'bg-signal' : v > 0.05 ? 'bg-paper-300' : 'bg-line'
            }`}
            style={{ height: `${Math.max(8, v * 100)}%` }}
          />
        ))}
      </div>
    </div>
  )
}
