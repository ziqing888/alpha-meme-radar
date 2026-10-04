import * as React from 'react';
import { cn } from '@/lib/utils';

export function Card({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn(
        'rounded-xl border border-[var(--color-wire)] bg-[var(--color-surface)] shadow-sm dark:border-[var(--color-wire-dark)] dark:bg-[var(--color-surface-dark)]',
        className
      )}
      {...props}
    />
  );
}

export function CardHeader({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn('flex flex-col gap-1.5 p-4 pb-2 sm:p-5', className)} {...props} />;
}

export function CardTitle({ className, ...props }: React.HTMLAttributes<HTMLHeadingElement>) {
  return (
    <h3
      className={cn(
        'text-[0.7rem] font-semibold uppercase tracking-[0.14em] text-[var(--color-ink-muted)] dark:text-[var(--color-ink-muted-dark)]',
        className
      )}
      {...props}
    />
  );
}

export function CardDescription({ className, ...props }: React.HTMLAttributes<HTMLParagraphElement>) {
  return (
    <p className={cn('text-sm text-[var(--color-ink-muted)] dark:text-[var(--color-ink-muted-dark)]', className)} {...props} />
  );
}

export function CardContent({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn('p-4 pt-2 sm:p-5', className)} {...props} />;
}
