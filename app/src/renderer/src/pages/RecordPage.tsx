import { useCallback, useEffect, useState } from 'react'
import type { AudioDevice, AudioDevices, Meeting } from '@shared/models'
import { LevelMeter } from '../components/LevelMeter'
import { PageHeader } from '../components/PageHeader'
import { useBackend, useLevelHistory, useRecording } from '../lib/contexts'
import { formatClock } from '../lib/format'

const NONE = 'none'
// Record every output device at once, so call audio is captured whichever
// output it plays through (speakers, headphones, a Bluetooth headset...).
const ALL = 'all'

export function RecordPage({
  onStopped
}: {
  onStopped: (meetingId: number) => void
}): React.JSX.Element {
  const { client } = useBackend()
  const recording = useRecording()
  const history = useLevelHistory()
  const [devices, setDevices] = useState<AudioDevices | null>(null)
  const [deviceError, setDeviceError] = useState<string | null>(null)
  const [micId, setMicId] = useState<string>(NONE)
  const [systemId, setSystemId] = useState<string>(ALL)
  const [title, setTitle] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Fetch devices and pick sensible defaults. setState only runs in promise
  // callbacks, so this is safe to call from an effect.
  const loadDevices = useCallback(() => {
    if (!client) return
    client.get<AudioDevices>('/audio/devices').then(
      (list) => {
        setDeviceError(null)
        setDevices(list)
        setMicId((current) => (keep(current, list.inputs) ? current : pickDefault(list.inputs)))
        setSystemId((current) =>
          list.loopbacks.length === 0
            ? NONE
            : current === ALL || keep(current, list.loopbacks)
              ? current
              : ALL
        )
      },
      (err: Error) => setDeviceError(err.message)
    )
  }, [client])

  useEffect(() => {
    loadDevices()
  }, [loadDevices])

  const run = async (action: () => Promise<unknown>): Promise<void> => {
    setBusy(true)
    setError(null)
    try {
      await action()
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const start = (): Promise<void> =>
    run(() =>
      client!.post<Meeting>('/recording/start', {
        title: title.trim() || null,
        mic_device_id: micId === NONE ? null : Number(micId),
        system_device_id: systemId === NONE ? null : systemId === ALL ? ALL : Number(systemId)
      })
    )

  const stop = (): Promise<void> =>
    run(async () => {
      const meeting = await client!.post<Meeting>('/recording/stop')
      setTitle('')
      onStopped(meeting.id)
    })

  const active = recording.state !== 'idle'
  const paused = recording.state === 'paused'
  const meeting = recording.meeting
  const canStart = !!client && !busy && (micId !== NONE || systemId !== NONE)

  return (
    <>
      <PageHeader
        eyebrow={active ? (paused ? 'Paused' : 'Recording') : 'Capture'}
        title={active && meeting ? meeting.title : 'New recording'}
        lede={
          active
            ? 'Recording keeps going if you switch pages. Stop it here when the meeting ends.'
            : "Records your microphone and your PC's sound as separate tracks, so you and the other side of the call are always told apart."
        }
      />

      <section className="grid gap-6 lg:grid-cols-[1fr_300px]">
        <div className="flex flex-col items-center justify-center rounded-lg border border-line bg-ink-900 px-8 py-12">
          {!active && (
            <label className="mb-8 w-full max-w-sm">
              <span className="eyebrow mb-2 block">Title</span>
              <input
                value={title}
                onChange={(e) => setTitle(e.target.value)}
                maxLength={200}
                placeholder="Optional, e.g. Weekly planning"
                className="w-full border-b border-line-strong bg-transparent pb-2 text-lg text-paper-50 placeholder:text-paper-700 focus:border-paper-500 focus:outline-none"
              />
            </label>
          )}

          <button
            type="button"
            onClick={() => void (active ? stop() : start())}
            disabled={active ? busy : !canStart}
            aria-label={active ? 'Stop recording' : 'Start recording'}
            className={`grid h-24 w-24 place-items-center rounded-full border transition-colors disabled:opacity-40 ${
              active
                ? 'border-paper-500 bg-ink-800 hover:bg-ink-850'
                : 'border-line-strong bg-ink-850 hover:border-paper-500'
            }`}
          >
            {active ? (
              <span className="h-7 w-7 rounded-[3px] bg-paper-100" />
            ) : (
              <span className="h-9 w-9 rounded-full bg-signal" />
            )}
          </button>

          <p
            className={`mt-6 font-mono text-3xl tabular-nums ${active ? 'text-paper-50' : 'text-paper-500'}`}
            aria-live="off"
          >
            {formatClock(recording.elapsed_ms)}
          </p>
          <p className="mt-2 text-sm text-paper-500">
            {active
              ? paused
                ? 'Paused. Nothing is being recorded.'
                : 'Click the square to stop.'
              : 'Click to start recording.'}
          </p>

          {active && (
            <button
              type="button"
              disabled={busy}
              onClick={() =>
                void run(() => client!.post(paused ? '/recording/resume' : '/recording/pause'))
              }
              className="mt-6 rounded-md border border-line-strong px-4 py-1.5 font-mono text-xs uppercase tracking-wider text-paper-300 hover:border-paper-500 hover:text-paper-50 disabled:opacity-40"
            >
              {paused ? 'Resume' : 'Pause'}
            </button>
          )}

          {(error || recording.errors.length > 0) && (
            <p className="mt-6 max-w-md text-center text-sm text-signal" role="alert">
              {error ?? recording.errors.join(' ')}
            </p>
          )}
        </div>

        <aside className="space-y-4">
          {active ? (
            <>
              <LevelMeter
                label="You · microphone"
                device={meeting?.mic_device ?? null}
                history={history.mic}
              />
              <LevelMeter
                label="Others · PC sound"
                device={meeting?.system_device ?? null}
                history={history.system}
              />
            </>
          ) : (
            <div className="space-y-5 rounded-lg border border-line bg-ink-900 p-4">
              <DevicePicker
                label="You · microphone"
                devices={devices?.inputs}
                value={micId}
                onChange={setMicId}
              />
              <DevicePicker
                label="Others · PC sound"
                devices={devices?.loopbacks}
                value={systemId}
                onChange={setSystemId}
                allOption={
                  devices && devices.loopbacks.length > 0 ? 'All outputs (recommended)' : undefined
                }
              />
              <div className="flex items-center justify-between">
                <button
                  type="button"
                  onClick={loadDevices}
                  className="font-mono text-[11px] uppercase tracking-wider text-paper-500 hover:text-paper-100"
                >
                  Refresh devices
                </button>
                {deviceError && <span className="text-xs text-signal">{deviceError}</span>}
              </div>
            </div>
          )}
          <p className="px-1 text-xs leading-relaxed text-paper-500">
            PC sound captures what the other people on a Zoom, Meet or Teams call say. With All
            outputs it records every speaker and headphone at once, so nothing is missed if an app
            plays through a different device. Let everyone know you&apos;re recording. In many
            places it&apos;s required by law.
          </p>
        </aside>
      </section>
    </>
  )
}

function pickDefault(items: AudioDevice[]): string {
  return String((items.find((d) => d.is_default) ?? items[0])?.id ?? NONE)
}

function keep(current: string, items: AudioDevice[]): boolean {
  return current !== NONE && items.some((d) => String(d.id) === current)
}

interface DevicePickerProps {
  label: string
  devices: AudioDevice[] | undefined
  value: string
  onChange: (value: string) => void
  /** Label for an extra first option that records every device. */
  allOption?: string
}

function DevicePicker({
  label,
  devices,
  value,
  onChange,
  allOption
}: DevicePickerProps): React.JSX.Element {
  return (
    <label className="block">
      <span className="eyebrow mb-2 block">{label}</span>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        disabled={!devices}
        className="w-full rounded-md border border-line-strong bg-ink-850 px-2.5 py-2 text-sm text-paper-100 focus:border-paper-500 focus:outline-none"
      >
        {!devices && <option value={value}>Looking for devices…</option>}
        {devices && allOption && <option value={ALL}>{allOption}</option>}
        {devices?.map((d) => (
          <option key={d.id} value={String(d.id)}>
            {d.name}
            {d.is_default ? ' (default)' : ''}
          </option>
        ))}
        {devices && <option value={NONE}>Don&apos;t record</option>}
      </select>
    </label>
  )
}
