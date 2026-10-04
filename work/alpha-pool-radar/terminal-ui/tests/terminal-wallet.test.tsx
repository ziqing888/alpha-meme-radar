import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { expect, it, vi } from 'vitest';
import WalletPanel from '@/components/terminal-wallet';
import { terminalApi } from '@/lib/terminal-api';

it('R12 restores focus to wallet import without submitting or entering a key', async () => {
  const post = vi.spyOn(terminalApi, 'post').mockRejectedValue(new Error('Unexpected mutation'));
  const user = userEvent.setup();
  render(<WalletPanel wallet={null} balances={[]} onSaved={() => {}} />);
  const opener = screen.getByRole('button', { name: '导入钱包' });
  await user.click(opener);
  await user.keyboard('{Escape}');
  await waitFor(() => expect(document.activeElement).toBe(opener));
  expect(post).not.toHaveBeenCalled();
});
