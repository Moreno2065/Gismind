import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import App from '@/App';
import { Sidebar } from '@/components/Sidebar';

vi.mock('@/components/ChatPanel', () => ({ ChatPanel: () => <div>会话内容</div> }));
vi.mock('@/components/ThemeToggle', () => ({ ThemeToggle: () => <button>主题</button> }));
vi.mock('@/hooks/useTheme', () => ({ useTheme: () => ({ theme: 'dark', setTheme: vi.fn() }) }));
vi.mock('@/hooks/useSessions', () => ({ useSessions: () => ({ sessions: [], activeId: 'session', loading: false, error: null, activate: vi.fn(), rename: vi.fn(), remove: vi.fn(), create: vi.fn(), refresh: vi.fn() }) }));
vi.mock('@/api/client', () => ({ getHealth: async () => ({ status: 'ok', version: '1.0', checks: {} }) }));

beforeEach(() => { localStorage.clear(); Object.defineProperty(window, 'innerWidth', { value: 1280, configurable: true }); });
afterEach(cleanup);

describe('responsive workspace shell', () => {
  it('does not leave focusable controls mounted when the sidebar is collapsed', () => {
    const view = render(<Sidebar open={false} onToggle={vi.fn()} sessions={[]} activeId={null} loading={false} error={null} onActivate={vi.fn()} onRename={vi.fn()} onDelete={vi.fn()} onCreate={vi.fn()} />);
    expect(view.container.querySelectorAll('button, input, [tabindex="0"]').length).toBe(0);
  });

  it('reserves desktop space for the sidebar and releases it when collapsed', async () => {
    render(<App />);
    await screen.findByText('会话内容');
    const workspace = screen.getByRole('main').parentElement!;
    expect(getComputedStyle(workspace).marginLeft).toBe('260px');
    fireEvent.click(screen.getByRole('button', { name: '收起侧栏' }));
    expect(getComputedStyle(workspace).marginLeft).toBe('0px');
    expect(screen.queryByRole('complementary')).toBeNull();
  });

  it('starts narrow screens collapsed despite a saved desktop preference and closes the mobile overlay', async () => {
    Object.defineProperty(window, 'innerWidth', { value: 480, configurable: true });
    localStorage.setItem('gismind.sidebar_open', '1');
    render(<App />);
    await screen.findByText('会话内容');
    expect(screen.queryByRole('complementary')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '展开侧栏' }));
    expect(screen.getByRole('complementary')).toBeTruthy();
    expect(getComputedStyle(screen.getByRole('main', { hidden: true }).parentElement!).marginLeft).toBe('0px');
    fireEvent.click(screen.getByRole('button', { name: '关闭会话侧栏遮罩' }));
    expect(screen.queryByRole('complementary')).toBeNull();
    expect(localStorage.getItem('gismind.sidebar_open')).toBe('1');
  });

  it('shows Runtime branding and real session controls without unsupported shortcut hints', async () => {
    render(<App />);
    await screen.findByText('会话内容');
    expect(screen.getAllByText('GIS RUNTIME').length).toBeGreaterThan(0);
    expect(screen.getByRole('button', { name: /新建会话/ })).toBeTruthy();
    expect(screen.queryByText(/⌘|archived|dispatch/i)).toBeNull();
  });
});
