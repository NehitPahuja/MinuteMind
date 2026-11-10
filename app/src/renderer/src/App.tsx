import { useState } from 'react'
import { Sidebar, type Page } from './components/Sidebar'
import { BackendProvider } from './lib/BackendProvider'
import { RecordingProvider } from './lib/RecordingProvider'
import { AskPage } from './pages/AskPage'
import { MeetingsPage } from './pages/MeetingsPage'
import { RecordPage } from './pages/RecordPage'
import { SettingsPage } from './pages/SettingsPage'

function Shell(): React.JSX.Element {
  const [page, setPage] = useState<Page>('meetings')
  const [selectedMeeting, setSelectedMeeting] = useState<number | null>(null)

  return (
    <div className="flex h-screen text-paper-100">
      <Sidebar page={page} onNavigate={setPage} />
      <main className="ruled flex-1 overflow-y-auto">
        <div className="mx-auto max-w-4xl px-12 py-12">
          {page === 'meetings' && (
            <MeetingsPage
              selectedId={selectedMeeting}
              onSelect={setSelectedMeeting}
              onRecord={() => setPage('record')}
            />
          )}
          {page === 'record' && (
            <RecordPage
              onStopped={(id) => {
                setSelectedMeeting(id)
                setPage('meetings')
              }}
            />
          )}
          {page === 'ask' && <AskPage />}
          {page === 'settings' && <SettingsPage />}
        </div>
      </main>
    </div>
  )
}

function App(): React.JSX.Element {
  return (
    <BackendProvider>
      <RecordingProvider>
        <Shell />
      </RecordingProvider>
    </BackendProvider>
  )
}

export default App
