import * as React from 'react';
import { cn } from '@/lib/utils';

export function Badge({
  className,
  variant = 'default',
  ...props
}: React.HTMLAttributes<HTMLSpanElement> & {
  variant?: 'default' | 'outline' | 'live' | 'warn';
}) {
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-[0.68rem] font-medium uppercase tracking-[0.08em]',
        variant === 'default' &&
          'bg-[var(--color-ink)] text-[var(--color-paper)] dark:bg-[var(--color-ink-dark)] dark:text-[var(--color-paper-dark)]',
        variant === 'outline' &&
          'border border-[var(--color-wire)] text-[var(--color-ink-muted)] dark:border-[var(--color-wire-dark)] dark:text-[var(--color-ink-muted-dark)]',
        variant === 'live' &&
          'border border-[color-mix(in_srgb,var(--color-ok)_35%,var(--color-wire))] bg-[var(--color-ok-subtle)] text-[var(--color-ok)] dark:border-[color-mix(in_srgb,var(--color-ok)_40%,var(--color-wire-dark))]',
        variant === 'warn' &&
          'border border-[color-mix(in_srgb,var(--color-warn)_35%,var(--color-wire))] bg-[var(--color-warn-subtle)] text-[var(--color-warn)] dark:border-[color-mix(in_srgb,var(--color-warn)_40%,var(--color-wire-dark))]',
        className
      )}
      {...props}
    />
  );
}
