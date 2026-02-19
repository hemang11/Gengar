interface StatCardProps {
    label: string
    value: string | number
    color?: 'green' | 'red' | 'yellow' | 'accent'
}

export default function StatCard({ label, value, color }: StatCardProps) {
    return (
        <div className="stat-card">
            <div className="label">{label}</div>
            <div className={`value ${color || ''}`}>{value}</div>
        </div>
    )
}
