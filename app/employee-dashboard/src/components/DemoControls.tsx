import { useEffect, useState } from "react";

interface DemoStatusResponse {
  running: boolean;
}

const buttonClass = "rounded-xl border border-white/10 bg-white/5 px-4 py-2 text-sm font-semibold transition hover:bg-white/10 disabled:cursor-not-allowed disabled:opacity-40";

export function DemoControls() {
  const [running, setRunning] = useState<boolean | null>(null);
  const [message, setMessage] = useState<string>("");
  const [pending, setPending] = useState<string | null>(null);

  useEffect(() => {
    let mounted = true;
    fetch("/simulator/demo")
      .then(res => {
        if (!res.ok) {
          throw new Error("status unavailable");
        }
        return res.json();
      })
      .then((data: DemoStatusResponse) => {
        if (mounted) {
          setRunning(Boolean(data.running));
        }
      })
      .catch(() => {
        if (mounted) {
          setRunning(false);
        }
      });
    return () => {
      mounted = false;
    };
  }, []);

  async function call(endpoint: string, options?: RequestInit) {
    setMessage("");
    setPending(endpoint);
    try {
      const response = await fetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        ...options,
      });
      if (!response.ok) {
        throw new Error(`Request failed (${response.status})`);
      }
      const payload = await response.json().catch(() => ({}));
      return payload;
    } catch (error) {
      const fallback = error instanceof Error ? error.message : "Unable to update demo";
      setMessage(fallback);
      return null;
    } finally {
      setPending(null);
    }
  }

  const handleStart = async () => {
    const payload = await call("/simulator/demo/start");
    if (payload) {
      setRunning(true);
      setMessage("Demo running");
    }
  };

  const handleStop = async () => {
    const payload = await call("/simulator/demo/stop");
    if (payload) {
      setRunning(false);
      setMessage("Demo paused");
    }
  };

  const handleReset = async () => {
    const payload = await call("/simulator/reset");
    if (payload) {
      setMessage("Lane reset");
    }
  };

  const handleSpawn = async () => {
    const payload = await call("/simulator/spawn");
    if (payload) {
      setMessage("Added demo car");
    }
  };

  return (
    <section className="rounded-2xl border border-white/10 bg-white/5 p-4">
      <header className="flex items-center justify-between">
        <div>
          <p className="text-xs uppercase tracking-[0.4em] text-white/40">Demo Controls</p>
          <p className="text-lg font-semibold">Drive-Thru Fleet</p>
        </div>
        <span className={`text-xs font-semibold ${running ? "text-emerald-300" : "text-amber-300"}`}>
          {running === null ? "" : running ? "Auto-run enabled" : "Paused"}
        </span>
      </header>

      <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <button className={buttonClass} disabled={running || pending === "/simulator/demo/start"} onClick={handleStart}>
          Start Demo
        </button>
        <button className={buttonClass} disabled={!running || pending === "/simulator/demo/stop"} onClick={handleStop}>
          Pause Demo
        </button>
        <button className={buttonClass} disabled={pending === "/simulator/reset"} onClick={handleReset}>
          Reset Lane
        </button>
        <button className={buttonClass} disabled={pending === "/simulator/spawn"} onClick={handleSpawn}>
          Add Demo Car
        </button>
      </div>

      {message && <p className="mt-3 text-sm text-white/70">{message}</p>}
    </section>
  );
}
