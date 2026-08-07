import { DriveThruCarState } from "@/hooks/useDashboardSocket";

function SectionTitle({ title }: { title: string }) {
  return <p className="text-xs uppercase tracking-[0.3em] text-white/50">{title}</p>;
}

export function CrmPanel({ car }: { car: DriveThruCarState | null }) {
  if (!car || !car.crmSummary) {
    return (
      <div className="rounded-3xl border border-white/5 bg-white/5 p-5 text-white/70">
        <p className="font-semibold">Select a car to view CRM insights</p>
      </div>
    );
  }

  const summary = car.crmSummary as Record<string, unknown>;
  const usual = (summary.usualOrder as Array<Record<string, unknown>>)?.map(renderItem).join(" · ") || "No saved order";
  const favorites = (summary.favoriteItems as Array<Record<string, unknown>>)?.map(renderItem).join(" · ") || "--";

  return (
    <div className="rounded-3xl border border-white/5 bg-white/5 p-5 text-white">
      <div className="flex items-start justify-between">
        <div>
          <p className="text-sm text-white/60">{summary.rewardsStatus as string} Rewards</p>
          <h2 className="text-2xl font-semibold">{summary.name as string}</h2>
        </div>
        <div className="text-right">
          <p className="text-xs uppercase tracking-[0.3em] text-white/40">Loyalty</p>
          <p className="text-lg font-semibold text-dunkin-orange">
            {summary.loyaltyScore as number}/{summary.loyaltyGoal as number}
          </p>
        </div>
      </div>
      <div className="mt-4 space-y-3 text-sm">
        <div>
          <SectionTitle title="Usual" />
          <p>{usual}</p>
        </div>
        <div>
          <SectionTitle title="Favorites" />
          <p>{favorites}</p>
        </div>
        {(summary.suggestedSales as string[])?.length ? (
          <div>
            <SectionTitle title="Crew hints" />
            <ul className="list-disc pl-4 text-white/80">
              {(summary.suggestedSales as string[]).slice(0, 2).map((hint: string) => (
                <li key={hint}>{hint}</li>
              ))}
            </ul>
          </div>
        ) : null}
      </div>
    </div>
  );
}

function renderItem(item: Record<string, unknown>) {
  if (!item) return "";
  const size = item.size ? `${item.size} ` : "";
  const qty = (item.quantity as number) > 1 ? `${item.quantity}x ` : "";
  return `${qty}${size}${item.item}`.trim();
}
