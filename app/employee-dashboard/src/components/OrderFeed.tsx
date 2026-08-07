import dayjs from "dayjs";
import { OrderEvent } from "@/hooks/useDashboardSocket";

export function OrderFeed({ orders }: { orders: OrderEvent[] }) {
  return (
    <div className="rounded-3xl border border-white/5 bg-white/5 p-5 text-white">
      <div className="flex items-center justify-between">
        <h3 className="text-lg font-semibold">Live Orders</h3>
        <span className="text-xs text-white/60">{orders.length} events</span>
      </div>
      <div className="mt-4 space-y-3 text-sm">
        {orders.length === 0 && <p className="text-white/60">No orders yet.</p>}
        {orders.map(order => (
          <div key={order.timestamp + order.sessionId} className="rounded-2xl border border-white/10 bg-white/5 p-3">
            <p className="text-xs uppercase tracking-widest text-white/50">{dayjs(order.timestamp).format("h:mm:ss A")}</p>
            <p className="text-base font-semibold">{(order.orderSummary?.items as unknown[] | undefined)?.length ?? 0} items • ${String(order.orderSummary?.finalTotal ?? "--")}</p>
            <p className="text-xs text-white/60">Car {order.carId}</p>
          </div>
        ))}
      </div>
    </div>
  );
}
