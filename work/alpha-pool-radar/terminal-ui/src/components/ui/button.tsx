import * as React from 'react';
import { cva, type VariantProps } from 'class-variance-authority';
import { cn } from '@/lib/utils';

const buttonVariants = cva(
  'inline-flex min-h-11 min-w-11 items-center justify-center gap-2 rounded-lg text-sm font-medium transition-[opacity,background-color,border-color,color,box-shadow] duration-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-ink)] focus-visible:ring-offset-2 disabled:pointer-events-none disabled:opacity-45 dark:focus-visible:ring-[var(--color-ink-dark)] dark:focus-visible:ring-offset-[var(--color-paper-dark)]',
  {
    variants: {
      variant: {
        default:
          'bg-[var(--color-ink)] text-[var(--color-paper)] hover:opacity-90 dark:bg-[var(--color-ink-dark)] dark:text-[var(--color-paper-dark)]',
        outline:
          'border border-[var(--color-wire)] bg-transparent hover:bg-[var(--color-paper)] dark:border-[var(--color-wire-dark)] dark:hover:bg-[var(--color-surface-dark)]',
        ghost: 'hover:bg-[var(--color-paper)] dark:hover:bg-[var(--color-surface-dark)]',
        destructive:
          'border border-[color-mix(in_srgb,var(--color-danger)_35%,var(--color-wire))] bg-[var(--color-danger-subtle)] text-[var(--color-danger)] hover:opacity-90 dark:border-[color-mix(in_srgb,var(--color-danger)_40%,var(--color-wire-dark))]',
      },
      size: {
        default: 'h-11 px-4 py-2',
        sm: 'h-9 min-h-9 min-w-9 rounded-md px-3 text-xs',
      },
    },
    defaultVariants: {
      variant: 'default',
      size: 'default',
    },
  }
);

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {}

export function Button({ className, variant, size, ...props }: ButtonProps) {
  return <button className={cn(buttonVariants({ variant, size, className }))} {...props} />;
}
