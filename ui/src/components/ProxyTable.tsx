
interface Proxy {
    ip: string
    port: number
    country?: string
    source?: string
    health_score?: number
    latency_ms?: number
    success_count?: number
    total_checks?: number
    last_checked?: number
    status?: string
}

interface ProxyTableProps {
    proxies: Proxy[]
    total: number
    page: number
    perPage: number
    onPageChange: (page: number) => void
}

export default function ProxyTable({ proxies, total, page, perPage, onPageChange }: ProxyTableProps) {
    const totalPages = Math.ceil(total / perPage)

    const successRate = (p: Proxy) => {
        const total = p.total_checks || 0
        const success = p.success_count || 0
        return total > 0 ? `${Math.round((success / total) * 100)}%` : '—'
    }

    const lastChecked = (p: Proxy) => {
        if (!p.last_checked) return '—'
        const diff = Date.now() / 1000 - p.last_checked
        if (diff < 60) return `${Math.round(diff)}s ago`
        if (diff < 3600) return `${Math.round(diff / 60)}m ago`
        return `${Math.round(diff / 3600)}h ago`
    }

    return (
        <div className="table-container">
            <div className="table-header">
                <h3>Proxy Pool ({total})</h3>
            </div>
            <div style={{ overflowX: 'auto' }}>
                <table>
                    <thead>
                        <tr>
                            <th>IP:Port</th>
                            <th>Country</th>
                            <th>Source</th>
                            <th>Health</th>
                            <th>Latency</th>
                            <th>Success Rate</th>
                            <th>Last Checked</th>
                            <th>Status</th>
                        </tr>
                    </thead>
                    <tbody>
                        {proxies.length === 0 ? (
                            <tr>
                                <td colSpan={8}>
                                    <div className="empty-state">
                                        <div className="icon">👻</div>
                                        <p>No proxies in pool yet</p>
                                    </div>
                                </td>
                            </tr>
                        ) : (
                            proxies.map((p: Proxy, i: number) => (
                                <tr key={`${p.ip}:${p.port}`}>
                                    <td>{p.ip}:{p.port}</td>
                                    <td>{p.country || '—'}</td>
                                    <td>{p.source || '—'}</td>
                                    <td>
                                        <span style={{ color: (p.health_score || 0) > 70 ? 'var(--green)' : (p.health_score || 0) > 40 ? 'var(--yellow)' : 'var(--red)' }}>
                                            {Math.round(p.health_score || 0)}%
                                        </span>
                                    </td>
                                    <td>{p.latency_ms ? `${Math.round(p.latency_ms)}ms` : '—'}</td>
                                    <td>{successRate(p)}</td>
                                    <td>{lastChecked(p)}</td>
                                    <td>
                                        <span className={`badge ${p.status === 'dead' ? 'dead' : 'healthy'}`}>
                                            {p.status || 'healthy'}
                                        </span>
                                    </td>
                                </tr>
                            ))
                        )}
                    </tbody>
                </table>
            </div>

            {totalPages > 1 && (
                <div className="pagination">
                    <button disabled={page <= 1} onClick={() => onPageChange(page - 1)}>
                        ← Prev
                    </button>
                    <span className="page-info">{page} / {totalPages}</span>
                    <button disabled={page >= totalPages} onClick={() => onPageChange(page + 1)}>
                        Next →
                    </button>
                </div>
            )}
        </div>
    )
}
