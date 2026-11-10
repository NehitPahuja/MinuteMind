import { useState } from 'react'
import { Sidebar, type Page } from './components/Sidebar'
import { useBackendStatus } from './hooks/useBackend'
import { AskPage } from './pages/AskPage'
import { MeetingsPage } from './pages/MeetingsPage'
import { RecordPage } from './pages/RecordPage'
import { SettingsPage } from './pages/SettingsPage'

function App(): React.JSX.Element {
  const status = useBackendStatus()
  const [page, setPage] = useState<Page>('meetings')

  return (
    <div className="flex h-screen text-paper-100">
      <Sidebar page={page} onNavigate={setPage} status={status} />
      <main className="ruled flex-1 overflow-y-auto">
        <div className="mx-auto max-w-4xl px-12 py-12">
          {page === 'meetings' && <MeetingsPage onRecord={() => setPage('record')} />}
          {page === 'record' && <RecordPage />}
          {page === 'ask' && <AskPage />}
          {page === 'settings' && <SettingsPage />}
        </div>
      </main>
    </div>
  )
}

export default App
