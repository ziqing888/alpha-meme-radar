import * as React from 'react';
import { cn } from '@/lib/utils';

type TabsContextValue = {
  value: string;
  setValue: (v: string) => void;
  baseId: string;
};

const TabsContext = React.createContext<TabsContextValue | null>(null);

export function Tabs({
  value,
  onValueChange,
  className,
  children,
  id = 'main-tabs',
}: {
  value: string;
  onValueChange: (v: string) => void;
  className?: string;
  children: React.ReactNode;
  id?: string;
}) {
  return (
    <TabsContext.Provider value={{ value, setValue: onValueChange, baseId: id }}>
      <div className={cn('space-y-5', className)}>{children}</div>
    </TabsContext.Provider>
  );
}

export function TabsList({ className, children, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      role="tablist"
      aria-label="Dashboard sections"
      className={cn(
        'flex w-full max-w-full items-center gap-1 overflow-x-auto rounded-xl border border-[var(--color-wire)] bg-[var(--color-surface)] p-1 [-ms-overflow-style:none] [scrollbar-width:none] dark:border-[var(--color-wire-dark)] dark:bg-[var(--color-surface-dark)] [&::-webkit-scrollbar]:hidden',
        className
      )}
      {...props}
    >
      {children}
    </div>
  );
}

export function TabsTrigger({ value, children }: { value: string; children: React.ReactNode }) {
  const ctx = React.useContext(TabsContext);
  if (!ctx) return null;
  const active = ctx.value === value;
  const tabId = `${ctx.baseId}-tab-${value}`;
  const panelId = `${ctx.baseId}-panel-${value}`;

  return (
    <button
      type="button"
      role="tab"
      id={tabId}
      aria-selected={active}
      aria-controls={panelId}
      tabIndex={active ? 0 : -1}
      onClick={() => ctx.setValue(value)}
      onKeyDown={(event) => {
        const buttons = Array.from(event.currentTarget.closest('[role="tablist"]')?.querySelectorAll<HTMLButtonElement>('[role="tab"]') || []);
        const index = buttons.indexOf(event.currentTarget);
        if (index === -1) return;

        if (event.key === 'ArrowRight') {
          event.preventDefault();
          buttons[(index + 1) % buttons.length]?.click();
          buttons[(index + 1) % buttons.length]?.focus();
        }
        if (event.key === 'ArrowLeft') {
          event.preventDefault();
          buttons[(index - 1 + buttons.length) % buttons.length]?.click();
          buttons[(index - 1 + buttons.length) % buttons.length]?.focus();
        }
      }}
      className={cn(
        'inline-flex min-h-9 shrink-0 items-center justify-center rounded-lg px-3 text-xs font-medium transition-[background-color,color,box-shadow] duration-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-ink)] focus-visible:ring-offset-2 sm:min-h-10 sm:min-w-[5.5rem] sm:px-4 sm:text-sm dark:focus-visible:ring-[var(--color-ink-dark)]',
        active
          ? 'bg-[var(--color-ink)] text-[var(--color-paper)] shadow-sm dark:bg-[var(--color-ink-dark)] dark:text-[var(--color-paper-dark)]'
          : 'text-[var(--color-ink-muted)] hover:bg-[var(--color-paper)] hover:text-[var(--color-ink)] dark:text-[var(--color-ink-muted-dark)] dark:hover:bg-[var(--color-wire-dark)] dark:hover:text-[var(--color-ink-dark)]'
      )}
    >
      {children}
    </button>
  );
}

export function TabsContent({ value, children }: { value: string; children: React.ReactNode }) {
  const ctx = React.useContext(TabsContext);
  if (!ctx || ctx.value !== value) return null;

  return (
    <div
      role="tabpanel"
      id={`${ctx.baseId}-panel-${value}`}
      aria-labelledby={`${ctx.baseId}-tab-${value}`}
      tabIndex={0}
      className="animate-rise-in outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-ink)] focus-visible:ring-offset-2 dark:focus-visible:ring-[var(--color-ink-dark)]"
    >
      {children}
    </div>
  );
}
