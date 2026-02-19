import { useState, useCallback } from 'react'
import LiveTraffic from './views/LiveTraffic'
import PoolHealth from './views/PoolHealth'
import RotationRules from './views/RotationRules'

const API_SECRET = 'changeme'

const VIEWS = [
    { id: 'traffic', label: 'Live Traffic', icon: '⚡' },
    { id: 'pool', label: 'Pool Health', icon: '🛡️' },
    { id: 'rules', label: 'Rotation Rules', icon: '⚙️' },
] as const

type ViewId = typeof VIEWS[number]['id']

export default function App() {
    const [activeView, setActiveView] = useState<ViewId>('traffic')
    const [wsConnected, setWsConnected] = useState(false)
    const [toast, setToast] = useState<string | null>(null)

    const showToast = useCallback((msg: string) => {
        setToast(msg)
        setTimeout(() => setToast(null), 3000)
    }, [])

    return (
        <div className="app-layout">
            {/* Sidebar */}
            <aside className="sidebar">
                <div className="sidebar-header">
                    <div className="sidebar-logo">
                        <div className="ghost-icon">👻</div>
                        <div>
                            <h1>Gengar</h1>
                            <div className="version">v1.0.0</div>
                        </div>
                    </div>
                </div>

                <nav className="sidebar-nav">
                    {VIEWS.map(v => (
                        <div
                            key={v.id}
                            className={`nav-item ${activeView === v.id ? 'active' : ''}`}
                            onClick={() => setActiveView(v.id)}
                        >
                            <span className="icon">{v.icon}</span>
                            {v.label}
                        </div>
                    ))}
                </nav>

                <div className="sidebar-footer">
                    <div className="connection-status">
                        <div className={`connection-dot ${wsConnected ? '' : 'disconnected'}`} />
                        {wsConnected ? 'WebSocket live' : 'Disconnected'}
                    </div>
                </div>
            </aside>

            {/* Main Content */}
            <main className="main-content">
                {activeView === 'traffic' && (
                    <LiveTraffic
                        apiSecret={API_SECRET}
                        onWsStatus={setWsConnected}
                        showToast={showToast}
                    />
                )}
                {activeView === 'pool' && (
                    <PoolHealth apiSecret={API_SECRET} showToast={showToast} />
                )}
                {activeView === 'rules' && (
                    <RotationRules apiSecret={API_SECRET} showToast={showToast} />
                )}
            </main>

            {/* Toast */}
            {toast && <div className="toast">{toast}</div>}
        </div>
    )
}
