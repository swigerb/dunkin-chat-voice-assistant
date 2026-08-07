import { DashboardMetrics } from "@/hooks/useDashboardSocket";

const metricCards = (
  metrics: DashboardMetrics | null
): { label: string; value: string; accent: string; sublabel?: string }[] => {
  if (!metrics) {
    return [
      { label: "Cars in queue", value: "--", accent: "text-white" },
      { label: "Avg wait", value: "--", accent: "text-white" },
      { label: "Orders/hr", value: "--", accent: "text-white" },
      { label: "Recognized", value: "--", accent: "text-white" }
    ];
  }
  return [
    { label: "Cars in queue", value: metrics.carsInQueue.toString(), accent: "text-dunkin-orange" },
    { label: "Avg wait", value: `${metrics.avgWaitSeconds}s`, accent: "text-dunkin-pink" },
    { label: "Orders/hr", value: metrics.ordersPerHour.toString(), accent: "text-emerald-400" },
    { label: "Recognized", value: `${metrics.recognizedPercent}%`, accent: "text-amber-300", sublabel: "CRM hits" }
  ];
};

export function MetricsPanel({ metrics }: { metrics: DashboardMetrics | null }) {
  return (
    <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
      {metricCards(metrics).map(card => (
        <div key={card.label} className="rounded-2xl border border-white/10 bg-white/5 p-4">
          <p className="text-xs uppercase tracking-widest text-white/60">{card.label}</p>
          <p className={`text-2xl font-semibold ${card.accent}`}>{card.value}</p>
          {card.sublabel && <p className="text-xs text-white/60">{card.sublabel}</p>}
        </div>
      ))}
    </div>
  );
}
