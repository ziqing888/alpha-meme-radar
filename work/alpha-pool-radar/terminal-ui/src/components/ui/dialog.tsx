import * as DialogPrimitive from '@radix-ui/react-dialog';
import { X } from 'lucide-react';
import * as React from 'react';
import { cn } from '@/lib/utils';

const Dialog = DialogPrimitive.Root;
const DialogTrigger = DialogPrimitive.Trigger;
const DialogPortal = DialogPrimitive.Portal;
const DialogClose = DialogPrimitive.Close;

const DialogOverlay = React.forwardRef<
  React.ComponentRef<typeof DialogPrimitive.Overlay>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Overlay>
>(({ className, ...props }, ref) => (
  <DialogPrimitive.Overlay
    ref={ref}
    className={cn(
      'fixed inset-0 z-[200] bg-black/55 backdrop-blur-[2px]',
      'data-[state=open]:animate-[dialog-overlay-in_200ms_ease-out_both]',
      'data-[state=closed]:animate-[dialog-overlay-out_150ms_ease-in_both]',
      className
    )}
    {...props}
  />
));
DialogOverlay.displayName = DialogPrimitive.Overlay.displayName;

const DialogContent = React.forwardRef<
  React.ComponentRef<typeof DialogPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Content> & {
    showClose?: boolean;
    returnFocusRef?: React.RefObject<HTMLElement | null>;
    fallbackFocusRef?: React.RefObject<HTMLElement | null>;
  }
>(({ className, children, showClose = true, returnFocusRef, fallbackFocusRef,
  onOpenAutoFocus, onCloseAutoFocus, ...props }, ref) => {
  const openerRef = React.useRef<HTMLElement | null>(null);
  return (
  <DialogPortal>
    <DialogOverlay />
    <DialogPrimitive.Content
      ref={ref}
      className="group fixed inset-0 z-[201] grid place-items-center p-4 outline-none"
      {...props}
      onOpenAutoFocus={(event) => {
        openerRef.current = returnFocusRef?.current ||
          (document.activeElement instanceof HTMLElement ? document.activeElement : null);
        onOpenAutoFocus?.(event);
      }}
      onCloseAutoFocus={(event) => {
        onCloseAutoFocus?.(event);
        if (event.defaultPrevented) return;
        // Controlled dialogs may have no Radix Trigger, or their row may disappear.
        for (const target of [openerRef.current, fallbackFocusRef?.current]) {
          if (!target?.isConnected || target.matches(':disabled') || target === document.body) continue;
          target.focus({ preventScroll: true });
          if (document.activeElement === target) {
            event.preventDefault();
            break;
          }
        }
      }}
    >
      <div
        className={cn(
          'relative flex w-full max-h-[min(90dvh,720px)] max-w-lg flex-col overflow-hidden rounded-2xl border border-[var(--color-wire)] bg-[var(--color-surface)] shadow-2xl dark:border-[var(--color-wire-dark)] dark:bg-[var(--color-surface-dark)]',
          'group-data-[state=open]:animate-[dialog-panel-in_220ms_cubic-bezier(0.16,1,0.3,1)_both]',
          'group-data-[state=closed]:animate-[dialog-panel-out_150ms_ease-in_both]',
          className
        )}
      >
        {children}
        {showClose ? (
          <DialogPrimitive.Close
            className="absolute right-4 top-4 grid size-9 shrink-0 place-items-center rounded-lg p-0 text-[var(--color-ink-muted)] transition-colors hover:bg-[var(--color-paper)] hover:text-[var(--color-ink)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-ink)] focus-visible:ring-offset-2 dark:text-[var(--color-ink-muted-dark)] dark:hover:bg-[var(--color-paper-dark)] dark:hover:text-[var(--color-ink-dark)] dark:focus-visible:ring-[var(--color-ink-dark)] dark:focus-visible:ring-offset-[var(--color-surface-dark)]"
            aria-label="Close"
          >
            <X className="size-4 shrink-0" strokeWidth={2} aria-hidden="true" />
          </DialogPrimitive.Close>
        ) : null}
      </div>
    </DialogPrimitive.Content>
  </DialogPortal>
  );
});
DialogContent.displayName = DialogPrimitive.Content.displayName;

function DialogHeader({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn('shrink-0 border-b border-[var(--color-wire)] px-4 py-4 pr-12 sm:px-5 sm:pr-14 dark:border-[var(--color-wire-dark)]', className)} {...props} />;
}

function DialogFooter({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn(
        'flex shrink-0 flex-wrap justify-end gap-2 border-t border-[var(--color-wire)] bg-[var(--color-surface)] px-4 py-4 sm:px-5 dark:border-[var(--color-wire-dark)] dark:bg-[var(--color-surface-dark)]',
        className
      )}
      {...props}
    />
  );
}

function DialogTitle({
  className,
  ...props
}: React.ComponentPropsWithoutRef<typeof DialogPrimitive.Title>) {
  return (
    <DialogPrimitive.Title
      className={cn(
        'text-lg font-semibold tracking-tight text-[var(--color-ink)] dark:text-[var(--color-ink-dark)]',
        className
      )}
      {...props}
    />
  );
}

function DialogDescription({
  className,
  ...props
}: React.ComponentPropsWithoutRef<typeof DialogPrimitive.Description>) {
  return (
    <DialogPrimitive.Description
      className={cn('mt-1 text-sm text-[var(--color-ink-muted)] dark:text-[var(--color-ink-muted-dark)]', className)}
      {...props}
    />
  );
}

export {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogOverlay,
  DialogPortal,
  DialogTitle,
  DialogTrigger,
};
