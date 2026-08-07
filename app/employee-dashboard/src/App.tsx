import { useMemo, useState } from "react";
import useDashboardSocket from "@/hooks/useDashboardSocket";
import { Lane } from "@/components/Lane";
import { MetricsPanel } from "@/components/MetricsPanel";
import { CrmPanel } from "@/components/CrmPanel";
import { OrderFeed } from "@/components/OrderFeed";
import { DemoControls } from "@/components/DemoControls";

export default function App() {
  const { cars, metrics, orders, connected, completeOrder } = useDashboardSocket();
  const [selectedCarId, setSelectedCarId] = useState<string | null>(null);

  const selectedCar = useMemo(() => {
    if (selectedCarId) {
      return cars.find(car => car.carId === selectedCarId) ?? null;
    }
    return cars[0] ?? null;
  }, [cars, selectedCarId]);

  return (
    <div className="min-h-screen bg-slate-950 px-6 py-8 text-white">
      <header className="mb-8 flex flex-wrap items-center justify-between gap-4">
        <div>
          <p className="text-xs uppercase tracking-[0.5em] text-white/40">Dunkin Hybrid Edge</p>
          <h1 className="text-3xl font-semibold">Drive-Thru Command Deck</h1>
        </div>
        <span
          className={`rounded-full px-4 py-1 text-xs font-semibold ${connected ? "bg-emerald-500/20 text-emerald-300" : "bg-rose-500/10 text-rose-200"}`}
        >
          {connected ? "Live" : "Disconnected"}
        </span>
      </header>

      <div className="space-y-6">
        <DemoControls />
        <MetricsPanel metrics={metrics} />
      </div>

      <div className="mt-6 grid gap-6 lg:grid-cols-[2fr_1fr]">
        <div className="space-y-6">
          <Lane cars={cars} selectedCarId={selectedCar?.carId} onSelect={car => setSelectedCarId(car.carId)} onComplete={completeOrder} />
          <OrderFeed orders={orders} />
        </div>
        <CrmPanel car={selectedCar} />
      </div>
    </div>
  );
}
