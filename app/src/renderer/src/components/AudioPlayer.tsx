import { useEffect, useImperativeHandle, useRef, useState } from 'react'
import { formatClock } from '../lib/format'

const SPEEDS = [1, 1.25, 1.5, 2]

/** Lets other components (the transcript) control playback. */
export interface PlayerHandle {
  /** Jump to a moment and start playing. */
  playFrom: (ms: number) => void
}

interface AudioPlayerProps {
  src: string
  /** Known length from the database, shown before the file's metadata loads. */
  durationMs: number
  /** Called as playback moves, with the position in milliseconds. */
  onTime?: (ms: number) => void
  ref?: React.Ref<PlayerHandle>
}

/** Play/pause, a scrubber, elapsed/total time and a speed toggle. */
export function AudioPlayer({ src, durationMs, onTime, ref }: AudioPlayerProps): React.JSX.Element {
  const audio = useRef<HTMLAudioElement>(null)
  const [playing, setPlaying] = useState(false)
  const [position, setPosition] = useState(0)
  const [duration, setDuration] = useState(durationMs / 1000)
  const [speed, setSpeed] = useState(1)
  const [error, setError] = useState<string | null>(null)

  // Keep the chosen speed when the track (src) changes.
  useEffect(() => {
    if (audio.current) audio.current.playbackRate = speed
  }, [speed, src])

  const toggle = (): void => {
    const el = audio.current
    if (!el) return
    if (el.paused) void el.play().catch((err: Error) => setError(err.message))
    else el.pause()
  }

  const seek = (seconds: number): void => {
    if (audio.current) audio.current.currentTime = seconds
    setPosition(seconds)
    onTime?.(seconds * 1000)
  }

  useImperativeHandle(ref, () => ({
    playFrom: (ms: number) => {
      const el = audio.current
      if (!el) return
      el.currentTime = ms / 1000
      setPosition(ms / 1000)
      onTime?.(ms)
      void el.play().catch((err: Error) => setError(err.message))
    }
  }))

  const progress = duration > 0 ? (position / duration) * 100 : 0

  return (
    <div className="flex items-center gap-4 rounded-md border border-line bg-ink-850 px-3 py-2.5">
      <audio
        ref={audio}
        src={src}
        preload="metadata"
        onPlay={() => setPlaying(true)}
        onPause={() => setPlaying(false)}
        onEnded={() => setPlaying(false)}
        onTimeUpdate={(e) => {
          setPosition(e.currentTarget.currentTime)
          onTime?.(e.currentTarget.currentTime * 1000)
        }}
        onLoadedMetadata={(e) => {
          if (Number.isFinite(e.currentTarget.duration)) setDuration(e.currentTarget.duration)
          setError(null)
        }}
        onError={() => setError("Couldn't load this audio.")}
      />

      <button
        type="button"
        onClick={toggle}
        aria-label={playing ? 'Pause' : 'Play'}
        className="grid h-9 w-9 shrink-0 place-items-center rounded-full bg-paper-100 text-ink-950 transition-colors hover:bg-paper-50"
      >
        {playing ? (
          <svg viewBox="0 0 12 12" className="h-3 w-3" aria-hidden="true">
            <rect x="2" y="1.5" width="2.8" height="9" rx="0.6" fill="currentColor" />
            <rect x="7.2" y="1.5" width="2.8" height="9" rx="0.6" fill="currentColor" />
          </svg>
        ) : (
          <svg viewBox="0 0 12 12" className="ml-0.5 h-3 w-3" aria-hidden="true">
            <path
              d="M3 1.6v8.8a.6.6 0 0 0 .9.5l7-4.4a.6.6 0 0 0 0-1L3.9 1.1a.6.6 0 0 0-.9.5Z"
              fill="currentColor"
            />
          </svg>
        )}
      </button>

      <span className="w-[68px] shrink-0 font-mono text-xs tabular-nums text-paper-300">
        {formatClock(position * 1000).replace(/^00:/, '')}
      </span>

      <input
        type="range"
        min={0}
        max={duration || 0}
        step={0.1}
        value={position}
        onChange={(e) => seek(Number(e.target.value))}
        aria-label="Position"
        className="scrubber flex-1"
        style={{ '--progress': `${progress}%` } as React.CSSProperties}
      />

      <span className="w-[68px] shrink-0 text-right font-mono text-xs tabular-nums text-paper-500">
        {formatClock(duration * 1000).replace(/^00:/, '')}
      </span>

      <button
        type="button"
        onClick={() => setSpeed(SPEEDS[(SPEEDS.indexOf(speed) + 1) % SPEEDS.length])}
        aria-label={`Playback speed ${speed}x`}
        className="w-12 shrink-0 rounded-sm border border-line py-0.5 font-mono text-[11px] text-paper-300 hover:border-line-strong hover:text-paper-50"
      >
        {speed}×
      </button>

      {error && <span className="text-xs text-signal">{error}</span>}
    </div>
  )
}
