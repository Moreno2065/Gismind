import { useState } from 'react';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it } from 'vitest';
import { ThemeToggle } from '@/components/ThemeToggle';
import type { Theme } from '@/hooks/useTheme';

afterEach(cleanup);

it('keeps both theme options named when their compact labels are hidden or selected', () => {
  function Subject() {
    const [theme, setTheme] = useState<Theme>('dark');
    return <ThemeToggle theme={theme} onChange={setTheme} />;
  }
  render(<Subject />);
  expect(screen.getByRole('radio', { name: 'DARK' }).getAttribute('aria-checked')).toBe('true');
  fireEvent.click(screen.getByRole('radio', { name: 'LIGHT' }));
  expect(screen.getByRole('radio', { name: 'LIGHT' }).getAttribute('aria-checked')).toBe('true');
  expect(screen.getByRole('radio', { name: 'DARK' }).getAttribute('aria-checked')).toBe('false');
});
