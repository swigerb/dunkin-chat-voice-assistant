import { useEffect, useMemo, useState } from "react";

export type DriveThruCarState = {
  carId: string;
  status: string;
  macAddress?: string | null;
  sessionId?: string | null;
  crmCustomerId?: string | null;
  crmSummary?: Record<string, unknown> | null;
  orderTotal?: number | null;
  waitSeconds: number;
  waitColor: string;
};

export type DashboardMetrics = {
  carsInQueue: number;
  avgWaitSeconds: number;
  ordersPerHour: number;
  recognizedPercent: number;
  timestamp?: string;
};

export type OrderEvent = {
  carId: string;
  sessionId: string;
  orderSummary: Record<string, unknown>;
  timestamp: string;
};

export default function useDashboardSocket() {
  const [cars, setCars] = useState<DriveThruCarState[]>([]);
  const [metrics, setMetrics] = useState<DashboardMetrics | null>(null);
  const [orders, setOrders] = useState<OrderEvent[]>([]);
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    const protocol = window.location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${protocol}://${window.location.host}/dashboard`);

    ws.onopen = () => setConnected(true);
    ws.onclose = () => setConnected(false);

    ws.onmessage = event => {
      const data = JSON.parse(event.data);
      switch (data.type) {
        case "dashboard.snapshot":
        case "lane.snapshot":
        case "session.assigned":
        case "car.arrived":
        case "car.crm_updated":
        case "car.complete":
        case "lane.reset":
          if (data.cars) {
            setCars(data.cars as DriveThruCarState[]);
          }
          if (data.metrics) {
            setMetrics(data.metrics as DashboardMetrics);
          }
          break;
        case "dashboard.order_update":
          setOrders(prev => {
            const next: OrderEvent[] = [
              {
                carId: data.carId,
                sessionId: data.sessionId,
                orderSummary: data.orderSummary ?? {},
                timestamp: data.timestamp ?? new Date().toISOString()
              },
              ...prev
            ];
            return next.slice(0, 12);
          });
          break;
      }
    };

    return () => {
      ws.close();
    };
  }, []);

  const activeCars = useMemo(() => cars.filter(car => car.status !== "complete"), [cars]);

  const completeOrder = async (carId: string) => {
    await fetch("/simulator/complete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ carId })
    });
  };

  return {
    cars: activeCars,
    metrics,
    orders,
    connected,
    completeOrder
  };
}
