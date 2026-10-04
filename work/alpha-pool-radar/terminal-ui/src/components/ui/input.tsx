import * as React from 'react';
import { cn } from '@/lib/utils';

export type InputProps = React.InputHTMLAttributes<HTMLInputElement>;

export const Input = React.forwardRef<HTMLInputElement, InputProps>(
  ({ className, type = 'text', ...props }, ref) => (
    <input
      type={type}
      className={cn(
        'flex h-11 w-full min-w-0 rounded-lg border border-[var(--color-wire)] bg-[var(--color-surface)] px-3 py-2 font-mono text-sm text-[var(--color-ink)] outline-none transition-[border-color,box-shadow] duration-200',
        'placeholder:text-[var(--color-ink-muted)] focus-visible:border-[var(--color-ink)] focus-visible:ring-2 focus-visible:ring-[var(--color-ink)]/15',
        'disabled:cursor-not-allowed disabled:opacity-50',
        'dark:border-[var(--color-wire-dark)] dark:bg-[var(--color-surface-dark)] dark:text-[var(--color-ink-dark)] dark:focus-visible:border-[var(--color-ink-dark)] dark:focus-visible:ring-[var(--color-ink-dark)]/20',
        className
      )}
      ref={ref}
      {...props}
    />
  )
);
Input.displayName = 'Input';

export function FieldLabel({
  children,
  htmlFor,
  hint,
}: {
  children: React.ReactNode;
  htmlFor?: string;
  hint?: string;
}) {
  return (
    <div className="space-y-1">
      <label
        htmlFor={htmlFor}
        className="block text-xs font-medium uppercase tracking-[0.08em] text-[var(--color-ink-muted)] dark:text-[var(--color-ink-muted-dark)]"
      >
        {children}
      </label>
      {hint ? (
        <p className="text-[0.7rem] text-[var(--color-ink-muted)] dark:text-[var(--color-ink-muted-dark)]">
          {hint}
        </p>
      ) : null}
    </div>
  );
}

export function Field({
  label,
  hint,
  htmlFor,
  children,
}: {
  label: string;
  hint?: string;
  htmlFor?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="space-y-1.5">
      <FieldLabel htmlFor={htmlFor} hint={hint}>
        {label}
      </FieldLabel>
      {children}
    </div>
  );
}
