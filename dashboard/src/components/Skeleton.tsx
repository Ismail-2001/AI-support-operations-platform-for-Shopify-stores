export function Skeleton({ className = "" }: { className?: string }) {
  return (
    <div className={`animate-pulse bg-ink-900/[0.06] dark:bg-white/[0.06] rounded ${className}`} />
  );
}

export function TicketListSkeleton() {
  return (
    <div className="space-y-3">
      {Array.from({ length: 5 }).map((_, i) => (
        <div key={i} className="bg-surface dark:bg-surface-dark border border-line dark:border-line-dark rounded-xl2 p-4">
          <div className="flex items-center gap-3 mb-2">
            <Skeleton className="h-4 w-32" />
            <Skeleton className="h-5 w-16 rounded-md" />
            <Skeleton className="h-5 w-20 rounded-md" />
          </div>
          <Skeleton className="h-4 w-64 mb-3" />
          <div className="flex items-center gap-4">
            <Skeleton className="h-2 w-28 rounded-full" />
            <Skeleton className="h-3 w-12" />
          </div>
        </div>
      ))}
    </div>
  );
}

export function TicketDetailSkeleton() {
  return (
    <div className="grid grid-cols-[1fr_360px] gap-6">
      <div>
        <Skeleton className="h-4 w-24 mb-2" />
        <Skeleton className="h-8 w-96 mb-4" />
        <div className="flex gap-2 mb-6">
          <Skeleton className="h-5 w-16 rounded-md" />
          <Skeleton className="h-5 w-20 rounded-md" />
        </div>
        <div className="space-y-4">
          {Array.from({ length: 3 }).map((_, i) => (
            <div key={i} className="border border-line dark:border-line-dark rounded-xl2 p-4">
              <Skeleton className="h-4 w-20 mb-3" />
              <Skeleton className="h-4 w-full mb-2" />
              <Skeleton className="h-4 w-3/4" />
            </div>
          ))}
        </div>
      </div>
      <div className="space-y-4">
        <div className="border border-line dark:border-line-dark rounded-xl2 p-5">
          <Skeleton className="h-4 w-24 mb-4" />
          <Skeleton className="h-3 w-full rounded-full mb-2" />
          <Skeleton className="h-4 w-full mt-4" />
          <Skeleton className="h-4 w-2/3 mt-2" />
        </div>
        <div className="border border-line dark:border-line-dark rounded-xl2 p-5">
          <Skeleton className="h-4 w-16 mb-3" />
          <Skeleton className="h-32 w-full rounded-lg mb-3" />
          <Skeleton className="h-10 w-full rounded-lg" />
        </div>
      </div>
    </div>
  );
}

export function AnalyticsSkeleton() {
  return (
    <div>
      <Skeleton className="h-8 w-32 mb-6" />
      <div className="grid grid-cols-4 gap-4 mb-8">
        {Array.from({ length: 4 }).map((_, i) => (
          <div key={i} className="border border-line dark:border-line-dark rounded-xl2 p-5">
            <Skeleton className="h-3 w-20 mb-2" />
            <Skeleton className="h-8 w-16" />
          </div>
        ))}
      </div>
      <div className="border border-line dark:border-line-dark rounded-xl2 p-5 mb-6">
        <Skeleton className="h-5 w-40 mb-4" />
        <Skeleton className="h-48 w-full" />
      </div>
    </div>
  );
}
