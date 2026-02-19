import { useState, useEffect, useRef, ChangeEvent } from 'react'
import RequestRow from '../components/RequestRow'

interface LiveTrafficProps {
    apiSecret: string
    onWsStatus: (connected: boolean) => void
    showToast: (msg: string) => void
}

export default function LiveTraffic({ apiSecret, onWsStatus }: LiveTrafficProps) {
    const [requests, setRequests] = useState<any[]>([])
    const [filter, setFilter] = useState('')
    const [blockedOnly, setBlockedOnly] = useState(false)
    const [selectedReq, setSelectedReq] = useState<any | null>(null)
    const wsRef = useRef<WebSocket | null>(null)

    useEffect(() => {
        // Initial fetch of last 100 requests
        fetch('/api/requests?count=100', {
            headers: { Authorization: `Bearer ${apiSecret}` },
        })
            .then(r => r.json())
            .then(data => setRequests(data.requests || []))
            .catch(() => { })

        // Connect WebSocket
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
        const wsUrl = `${protocol}//${window.location.host}/ws/live`
        const ws = new WebSocket(wsUrl)
        wsRef.current = ws

        ws.onopen = () => onWsStatus(true)
        ws.onclose = () => onWsStatus(false)
        ws.onmessage = (event) => {
            try {
                const req = JSON.parse(event.data)
                setRequests((prev: any[]) => [req, ...prev].slice(0, 500))
            } catch (err) { }
        }

        return () => {
            ws.close()
        }
    }, [apiSecret, onWsStatus])

    const filtered = requests.filter((r: any) => {
        if (blockedOnly && !r.blocked) return false
        if (filter) {
            const search = filter.toLowerCase()
            return (
                r.url?.toLowerCase().includes(search) ||
                r.target_domain?.toLowerCase().includes(search) ||
                r.proxy_ip?.toLowerCase().includes(search)
            )
        }
        return true
    })

    return (
        <div className="page-fade-in">
            <div className="page-header">
                <h2>Live Traffic</h2>
                <p>Real-time stream of proxied requests through Gengar</p>
            </div>

            <div className="filter-bar">
                <input
                    type="text"
                    placeholder="Filter by domain or URL..."
                    className="input"
                    value={filter}
                    onChange={(e: ChangeEvent<HTMLInputElement>) => setFilter(e.target.value)}
                />
                <label className="toggle-label">
                    <input
                        type="checkbox"
                        checked={blockedOnly}
                        onChange={(e: ChangeEvent<HTMLInputElement>) => setBlockedOnly(e.target.checked)}
                    />
                    Blocked only
                </label>
                <button className="btn btn-secondary btn-sm" onClick={() => setRequests([])}>
                    Clear Log
                </button>
            </div>

            <div className="table-container">
                <div style={{ maxHeight: 'calc(100vh - 300px)', overflowY: 'auto' }}>
                    <table>
                        <thead>
                            <tr>
                                <th>Time</th>
                                <th>Domain</th>
                                <th>Proxy IP</th>
                                <th>Status</th>
                                <th>Latency</th>
                                <th>Blocked</th>
                            </tr>
                        </thead>
                        <tbody>
                            {filtered.length === 0 ? (
                                <tr>
                                    <td colSpan={6}>
                                        <div className="empty-state">
                                            <div className="icon">📡</div>
                                            <p>Waiting for traffic...</p>
                                        </div>
                                    </td>
                                </tr>
                            ) : (
                                filtered.map((req: any, i: number) => (
                                    <RequestRow
                                        key={req.ts + i}
                                        req={req}
                                        onClick={() => setSelectedReq(req)}
                                    />
                                ))
                            )}
                        </tbody>
                    </table>
                </div>
            </div>

            {/* Slide-in Detail Panel */}
            <div className={`detail-panel ${selectedReq ? 'open' : ''}`}>
                <div className="detail-panel-header">
                    <h3>Request Details</h3>
                    <button className="btn btn-secondary btn-sm" onClick={() => setSelectedReq(null)}>Close</button>
                </div>
                {selectedReq && (
                    <div className="detail-panel-content">
                        <div className="detail-row">
                            <span className="key">Method</span>
                            <span className="val">{selectedReq.method}</span>
                        </div>
                        <div className="detail-row">
                            <span className="key">Status</span>
                            <span className={`val ${selectedReq.status >= 400 ? 'red' : 'green'}`}>
                                {selectedReq.status}
                            </span>
                        </div>
                        <div className="detail-row">
                            <span className="key">URL</span>
                            <span className="val">{selectedReq.url}</span>
                        </div>
                        <div className="detail-row">
                            <span className="key">Proxy</span>
                            <span className="val">{selectedReq.proxy_ip}</span>
                        </div>
                        <div className="detail-row">
                            <span className="key">Latency</span>
                            <span className="val">{selectedReq.latency_ms}ms</span>
                        </div>
                        <div className="detail-row">
                            <span className="key">Strategy</span>
                            <span className="val">{selectedReq.strategy}</span>
                        </div>
                        <div className="detail-row">
                            <span className="key">Attempt</span>
                            <span className="val">{selectedReq.attempt}</span>
                        </div>
                        {selectedReq.error && (
                            <div className="detail-row">
                                <span className="key">Error</span>
                                <span className="val red">{selectedReq.error}</span>
                            </div>
                        )}
                        <div style={{ marginTop: 24 }}>
                            <h4 style={{ fontSize: 11, color: 'var(--text-muted)', textTransform: 'uppercase', marginBottom: 8 }}>Response Headers</h4>
                            <div style={{ background: 'var(--bg-input)', padding: 12, borderRadius: 6, fontSize: 11, fontFamily: 'var(--font-mono)', overflowX: 'auto' }}>
                                {Object.entries(selectedReq.response_headers || {}).map(([k, v]) => (
                                    <div key={k} style={{ marginBottom: 4 }}>
                                        <span style={{ color: 'var(--accent-light)' }}>{k}:</span> {String(v)}
                                    </div>
                                ))}
                            </div>
                        </div>
                    </div>
                )}
            </div>
        </div>
    )
}
