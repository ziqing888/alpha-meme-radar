import { useRef, useState } from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { expect, it } from 'vitest';
import { Dialog, DialogContent, DialogDescription, DialogTitle, DialogTrigger } from '@/components/ui/dialog';

it('preserves native Radix trigger focus restoration', async () => {
  const user = userEvent.setup();
  render(<Dialog>
    <DialogTrigger>Native opener</DialogTrigger>
    <DialogContent><DialogTitle>Test</DialogTitle><DialogDescription>Test</DialogDescription></DialogContent>
  </Dialog>);
  const opener = screen.getByRole('button', { name: 'Native opener' });
  await user.click(opener);
  await user.click(screen.getByRole('button', { name: 'Close' }));
  await waitFor(() => expect(document.activeElement).toBe(opener));
});

function Controlled({ custom = false }: { custom?: boolean }) {
  const [open, setOpen] = useState(false);
  const fallback = useRef<HTMLButtonElement>(null);
  return <>
    <button onClick={() => setOpen(true)}>First opener</button>
    <button onClick={() => setOpen(true)}>Second opener</button>
    <button ref={fallback}>Custom target</button>
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogContent onCloseAutoFocus={custom ? event => {
        event.preventDefault();
        fallback.current?.focus();
      } : undefined}>
        <DialogTitle>Test</DialogTitle><DialogDescription>Test</DialogDescription>
      </DialogContent>
    </Dialog>
  </>;
}

it('captures the current opener on every opening, not only the first', async () => {
  const user = userEvent.setup();
  render(<Controlled />);
  for (const name of ['First opener', 'Second opener']) {
    const opener = screen.getByRole('button', { name });
    await user.click(opener);
    await user.keyboard('{Escape}');
    await waitFor(() => expect(document.activeElement).toBe(opener));
  }
});

it('honors a caller-provided close autofocus override', async () => {
  const user = userEvent.setup();
  render(<Controlled custom />);
  const target = screen.getByRole('button', { name: 'Custom target' });
  await user.click(screen.getByRole('button', { name: 'First opener' }));
  await user.keyboard('{Escape}');
  await waitFor(() => expect(document.activeElement).toBe(target));
});
