import { useState, useEffect } from 'react'
import StatCard from '../components/StatCard'
import ProxyTable from '../components/ProxyTable'

interface PoolHealthProps {
    apiSecret: string
    showToast: (msg: string) => void
}

export default function PoolHealth({ apiSecret, showToast }: PoolHealthProps) {
    const [stats, setStats] = useState<any>(null)
    const [proxies, setProxies] = useState<any[]>([])
    const [page, setPage] = useState(1)
    const [total, setTotal] = useState(0)
    const [loading, setLoading] = useState(false)
    const [refreshing, setRefreshing] = useState(false)

    const fetchData = async () => {
        setLoading(true)
        try {
            const [statsR, poolR] = await Promise.all([
                fetch('/api/stats', { headers: { Authorization: `Bearer ${apiSecret}` } }),
                fetch(`/api/pool?page=${page}&per_page=20`, { headers: { Authorization: `Bearer ${apiSecret}` } }),
            ])
            setStats(await statsR.json())
            const poolData = await poolR.json()
            setProxies(poolData.proxies || [])
            setTotal(poolData.total || 0)
        } catch (err) {
            showToast('❌ Failed to load dashboard data')
        } finally {
            setLoading(false)
        }
    }

    useEffect(() => {
        fetchData()
        const interval = setInterval(fetchData, 10000)
        return () => clearInterval(interval)
    }, [apiSecret, page])

    const triggerRefresh = async () => {
        setRefreshing(true)
        try {
            const resp = await fetch('/api/pool/refresh', {
                method: 'POST',
                headers: { Authorization: `Bearer ${apiSecret}` },
            })
            if (resp.ok) {
                showToast('🔄 Pool refresh triggered')
                fetchData()
            }
        } catch {
            showToast('❌ Failed to trigger refresh')
        } finally {
            setRefreshing(false)
        }
    }

    const flushDead = async () => {
        try {
            await fetch('/api/pool/flush', {
                method: 'POST',
                headers: { Authorization: `Bearer ${apiSecret}` },
            })
            showToast('🗑️ Cleared all dead proxies')
            fetchData()
        } catch {
            showToast('❌ Failed to flush dead proxies')
        }
    }

    return (
        <div className="page-fade-in">
            <div className="page-header" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
                <div>
                    <h2>Pool Health Dashboard</h2>
                    <p>Global status of the proxy pool and rotation engine metrics</p>
                </div>
                <div style={{ display: 'flex', gap: 10 }}>
                    <button className="btn btn-secondary" onClick={flushDead}>Flush Dead</button>
                    <button className="btn btn-primary" onClick={triggerRefresh} disabled={refreshing}>
                        {refreshing ? <span className="spinner" /> : 'Refresh Pool Now'}
                    </button>
                </div>
            </div>

            <div className="stat-cards">
                <StatCard label="Total Proxies" value={stats?.total_proxies || 0} color="accent" />
                <StatCard label="Healthy" value={stats?.healthy || 0} color="green" />
                <StatCard label="Dead / Removed" value={stats?.dead || 0} color="red" />
                <StatCard label="Req/sec" value={stats?.req_per_sec || 0.0} color="yellow" />
                <StatCard label="Block Rate" value={`${stats?.block_rate || 0}%`} color="red" />
                <StatCard label="Avg Latency" value={`${stats?.avg_latency_ms || 0}ms`} color="green" />
            </div>

            <ProxyTable
                proxies={proxies}
                total={total}
                page={page}
                perPage={20}
                onPageChange={setPage}
            />
        </div>
    )
}
