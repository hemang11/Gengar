interface RequestRowProps {
    req: {
        ts: number
        method?: string
        url?: string
        target_domain?: string
        proxy_ip?: string
        status?: number
        latency_ms?: number
        blocked?: boolean
        attempt?: number
        strategy?: string
        error?: string
        response_headers?: Record<string, string>
    }
    onClick?: () => void
}

export default function RequestRow({ req, onClick }: RequestRowProps) {
    const time = new Date(req.ts * 1000).toLocaleTimeString()
    const latency = req.latency_ms ? `${Math.round(req.latency_ms)}ms` : '—'
    const status = req.status || 0

    let rowClass = ''
    if (req.blocked) {
        rowClass = 'row-blocked'
    } else if (req.error) {
        rowClass = 'row-error'
    } else if (req.latency_ms && req.latency_ms > 2000) {
        rowClass = 'row-slow'
    } else if (status >= 200 && status < 400) {
        rowClass = 'row-success'
    }

    let statusBadge = ''
    if (req.blocked) {
        statusBadge = 'blocked'
    } else if (status >= 200 && status < 400) {
        statusBadge = 'success'
    } else if (req.latency_ms && req.latency_ms > 2000) {
        statusBadge = 'slow'
    }

    return (
        <tr className={rowClass} onClick={onClick}>
            <td>{time}</td>
            <td>{req.target_domain || '—'}</td>
            <td>{req.proxy_ip || '—'}</td>
            <td>
                <span className={`badge ${statusBadge}`}>{status || '—'}</span>
            </td>
            <td>{latency}</td>
            <td>
                {req.blocked ? (
                    <span className="badge blocked">BLOCKED</span>
                ) : (
                    '—'
                )}
            </td>
        </tr>
    )
}
