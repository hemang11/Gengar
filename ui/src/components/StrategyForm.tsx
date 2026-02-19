import { useState, useEffect, ChangeEvent } from 'react'

interface StrategyFormProps {
    apiSecret: string
    showToast: (msg: string) => void
}

const STRATEGIES = [
    { value: 'per-request', label: 'Per-Request (new proxy every request)' },
    { value: 'per-session', label: 'Per-Session (sticky per session ID)' },
    { value: 'time-based', label: 'Time-Based (rotate every N seconds)' },
    { value: 'on-block', label: 'On-Block (rotate only on block detected)' },
    { value: 'round-robin', label: 'Round-Robin (cycle in order)' },
]

export default function StrategyForm({ apiSecret, showToast }: StrategyFormProps) {
    const [strategy, setStrategy] = useState('per-request')
    const [sessionTtl, setSessionTtl] = useState(300)
    const [rotationInterval, setRotationInterval] = useState(30)
    const [saving, setSaving] = useState(false)

    useEffect(() => {
        fetch('/api/rotation-rules', {
            headers: { Authorization: `Bearer ${apiSecret}` },
        })
            .then(r => r.json())
            .then(data => {
                setStrategy(data.strategy || 'per-request')
                setSessionTtl(data.session_ttl || 300)
                setRotationInterval(data.rotation_interval || 30)
            })
            .catch(() => { })
    }, [apiSecret])

    const save = async () => {
        setSaving(true)
        try {
            const body: Record<string, unknown> = { strategy }
            if (strategy === 'per-session') body.session_ttl = sessionTtl
            if (strategy === 'time-based') body.rotation_interval = rotationInterval

            const resp = await fetch('/api/rotation-rules', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    Authorization: `Bearer ${apiSecret}`,
                },
                body: JSON.stringify(body),
            })
            if (resp.ok) {
                showToast('✅ Strategy updated successfully')
            } else {
                showToast('❌ Failed to update strategy')
            }
        } catch {
            showToast('❌ Network error')
        } finally {
            setSaving(false)
        }
    }

    return (
        <div>
            <div className="form-group">
                <label>Rotation Strategy</label>
                <select
                    value={strategy}
                    onChange={(e: ChangeEvent<HTMLSelectElement>) => setStrategy(e.target.value)}
                >
                    {STRATEGIES.map(s => (
                        <option key={s.value} value={s.value}>{s.label}</option>
                    ))}
                </select>
            </div>

            {strategy === 'per-session' && (
                <div className="form-group">
                    <label>Session TTL (seconds)</label>
                    <input
                        type="number"
                        value={sessionTtl}
                        onChange={(e: ChangeEvent<HTMLInputElement>) => setSessionTtl(parseInt(e.target.value) || 0)}
                        min={1}
                    />
                </div>
            )}

            {strategy === 'time-based' && (
                <div className="form-group">
                    <label>Rotation Interval (seconds)</label>
                    <input
                        type="number"
                        value={rotationInterval}
                        onChange={(e: ChangeEvent<HTMLInputElement>) => setRotationInterval(parseInt(e.target.value) || 0)}
                        min={1}
                    />
                </div>
            )}

            <button className="btn btn-primary" onClick={save} disabled={saving}>
                {saving ? <span className="spinner" /> : null}
                Save Strategy
            </button>
        </div>
    )
}
