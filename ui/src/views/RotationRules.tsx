import { useState, useEffect, ChangeEvent } from 'react'
import StrategyForm from '../components/StrategyForm'

interface RotationRulesProps {
    apiSecret: string
    showToast: (msg: string) => void
}

export default function RotationRules({ apiSecret, showToast }: RotationRulesProps) {
    const [overrides, setOverrides] = useState<any[]>([])
    const [newDomain, setNewDomain] = useState('')
    const [newStrategy, setNewStrategy] = useState('on-block')
    const [adding, setAdding] = useState(false)

    const fetchOverrides = async () => {
        try {
            const resp = await fetch('/api/domain-overrides', {
                headers: { Authorization: `Bearer ${apiSecret}` },
            })
            const data = await resp.json()
            setOverrides(data.overrides || [])
        } catch { }
    }

    useEffect(() => {
        fetchOverrides()
    }, [apiSecret])

    const addOverride = async () => {
        if (!newDomain) return
        setAdding(true)
        try {
            const resp = await fetch('/api/domain-overrides', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    Authorization: `Bearer ${apiSecret}`,
                },
                body: JSON.stringify({ domain: newDomain, strategy: newStrategy }),
            })
            if (resp.ok) {
                showToast(`✅ Override added for ${newDomain}`)
                setNewDomain('')
                fetchOverrides()
            }
        } finally {
            setAdding(false)
        }
    }

    const deleteOverride = async (domain: string) => {
        await fetch(`/api/domain-overrides/${domain}`, {
            method: 'DELETE',
            headers: { Authorization: `Bearer ${apiSecret}` },
        })
        fetchOverrides()
    }

    return (
        <div className="page-fade-in">
            <div className="page-header">
                <h2>Rotation Rules</h2>
                <p>Configure how proxies are assigned and managed globally or per domain</p>
            </div>

            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 32 }}>
                {/* Global Strategy */}
                <section>
                    <div className="table-header" style={{ marginBottom: 16, borderBottom: 'none', padding: 0 }}>
                        <h3>Global Configuration</h3>
                    </div>
                    <div className="stat-card" style={{ padding: 24, background: 'var(--bg-secondary)' }}>
                        <StrategyForm apiSecret={apiSecret} showToast={showToast} />
                    </div>
                </section>

                {/* Domain Overrides */}
                <section>
                    <div className="table-header" style={{ marginBottom: 16, borderBottom: 'none', padding: 0 }}>
                        <h3>Per-Domain Overrides</h3>
                    </div>
                    <div className="stat-card" style={{ padding: 24, background: 'var(--bg-secondary)' }}>
                        <div style={{ display: 'flex', gap: 10, marginBottom: 20 }}>
                            <input
                                type="text"
                                placeholder="amazon.com"
                                className="input"
                                value={newDomain}
                                onChange={(e: ChangeEvent<HTMLInputElement>) => setNewDomain(e.target.value)}
                            />
                            <select
                                style={{ width: 140 }}
                                value={newStrategy}
                                onChange={(e: ChangeEvent<HTMLSelectElement>) => setNewStrategy(e.target.value)}
                            >
                                <option value="per-request">request</option>
                                <option value="per-session">session</option>
                                <option value="on-block">on-block</option>
                                <option value="round-robin">round-robin</option>
                            </select>
                            <button className="btn btn-secondary" onClick={addOverride} disabled={adding}>
                                Add
                            </button>
                        </div>

                        <div style={{ maxHeight: 300, overflowY: 'auto' }}>
                            {overrides.length === 0 ? (
                                <div className="empty-state" style={{ padding: '20px 0' }}>
                                    <p>No active overrides</p>
                                </div>
                            ) : (
                                overrides.map((o: any) => (
                                    <div key={o.domain} className="override-row">
                                        <span className="domain">{o.domain}</span>
                                        <span className="strategy-tag">{o.strategy}</span>
                                        <button className="btn btn-danger btn-sm" onClick={() => deleteOverride(o.domain)}>
                                            Remove
                                        </button>
                                    </div>
                                ))
                            )}
                        </div>
                    </div>
                </section>
            </div>

            <div style={{ marginTop: 32, padding: 20, background: 'var(--accent-glow)', borderRadius: 10, border: '1px solid rgba(124, 58, 237, 0.2)' }}>
                <h4 style={{ fontSize: 13, marginBottom: 8, color: 'var(--accent-light)' }}>ℹ️ Strategy Help</h4>
                <ul style={{ fontSize: 12, color: 'var(--text-secondary)', paddingLeft: 20 }}>
                    <li style={{ marginBottom: 4 }}><b>Per-Request:</b> Highest rotation, but easier to detect by some sites.</li>
                    <li style={{ marginBottom: 4 }}><b>Per-Session:</b> Best for scraping workflows that require logging in.</li>
                    <li style={{ marginBottom: 4 }}><b>Time-Based:</b> Balance between session stability and IP rotation.</li>
                    <li><b>On-Block:</b> Maximizes IP life; only rotates when actually necessary.</li>
                </ul>
            </div>
        </div>
    )
}
