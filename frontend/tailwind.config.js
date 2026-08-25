/** @type {import('tailwindcss').Config} */
// Gismind 色板完全由 CSS 变量驱动（在 index.css 中按 :root / :root.light 分主题定义）。
// 这里只把 hex 占位换成 var() 引用，保持 token 名称与透明度修饰符不变。

function v(name) {
  return `rgb(var(${name}) / <alpha-value>)`;
}

export default {
  // 仅用于语义判定（不依赖 dark: 前缀）；本仓库靠 CSS 变量自动切换主题
  darkMode: ['class', '.light-mode-dark'],
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        // Cartographic Intelligence palette
        ink: {
          950: v('--c-ink-950'), // page background
          900: v('--c-ink-900'), // raised surfaces
          800: v('--c-ink-800'), // panels / assistant bubbles
          700: v('--c-ink-700'), // borders
          600: v('--c-ink-600'), // hover
          500: v('--c-ink-500'), // muted text
          400: v('--c-ink-400'), // secondary text
          300: v('--c-ink-300'), // tertiary
          200: v('--c-ink-200'), // primary text
          100: v('--c-ink-100'), // headings
          50:  v('--c-ink-50'),  // extreme
        },
        amber: {
          // 高德数据焦点色
          DEFAULT: v('--c-amber'),
          glow:    v('--c-amber-glow'),
          deep:    v('--c-amber-deep'),
        },
        osm: {
          // OSM 数据源 — 视觉上柔和
          DEFAULT: v('--c-osm'),
          glow:    v('--c-osm-glow'),
        },
        signal: {
          // 状态语义色
          thinking: v('--c-signal-thinking'),
          fetching: v('--c-signal-fetching'),
          error:    v('--c-signal-error'),
          done:     v('--c-signal-done'),
          voting:    v('--c-signal-voting'),
          reviewing: v('--c-signal-reviewing'),
          reflecting: v('--c-signal-reflecting'),
        },
        // 主题切换控件专用语义
        paper:   v('--c-paper'),
        canvas:  v('--c-canvas'),
        ridge:   v('--c-ridge'),
      },
      fontFamily: {
        display: ['Georgia', 'Cambria', '"Noto Serif SC"', '"Songti SC"', '"SimSun"', 'serif'],
        sans: ['system-ui', '-apple-system', 'BlinkMacSystemFont', '"Segoe UI"', 'Roboto', '"Helvetica Neue"', 'Arial', '"Noto Sans SC"', '"PingFang SC"', '"Microsoft YaHei"', 'sans-serif'],
        mono: ['ui-monospace', 'SFMono-Regular', '"SF Mono"', 'Menlo', 'Consolas', '"Liberation Mono"', 'monospace'],
      },
      boxShadow: {
        bubble: '0 1px 0 0 rgb(var(--c-bubble-rim) / <alpha-value>) inset, 0 8px 32px -12px rgb(var(--c-bubble-shadow) / <alpha-value>)',
        glow: '0 0 0 1px rgb(var(--c-glow) / <alpha-value>), 0 0 24px -4px rgb(var(--c-glow) / <alpha-value>)',
      },
      animation: {
        'fade-in-up': 'fadeInUp 0.5s cubic-bezier(0.16, 1, 0.3, 1) both',
        'fade-in': 'fadeIn 0.4s ease-out both',
        'pulse-soft': 'pulseSoft 2s ease-in-out infinite',
        'blink': 'blink 1.1s steps(2) infinite',
        'shimmer': 'shimmer 1.8s linear infinite',
        // 主题切换控件专用
        'theme-icon-in': 'themeIconIn 0.5s cubic-bezier(0.16, 1, 0.3, 1) both',
        'theme-orbit': 'themeOrbit 14s linear infinite',
      },
      keyframes: {
        fadeInUp: {
          '0%': { opacity: '0', transform: 'translateY(8px)' },
          '100%': { opacity: '1', transform: 'translateY(0)' },
        },
        fadeIn: {
          '0%': { opacity: '0' },
          '100%': { opacity: '1' },
        },
        pulseSoft: {
          '0%, 100%': { opacity: '0.6' },
          '50%': { opacity: '1' },
        },
        blink: {
          '0%, 100%': { opacity: '1' },
          '50%': { opacity: '0' },
        },
        shimmer: {
          '0%': { backgroundPosition: '-200% 0' },
          '100%': { backgroundPosition: '200% 0' },
        },
        themeIconIn: {
          '0%': { opacity: '0', transform: 'rotate(-45deg) scale(0.65)' },
          '100%': { opacity: '1', transform: 'rotate(0deg) scale(1)' },
        },
        themeOrbit: {
          '0%': { transform: 'rotate(0deg)' },
          '100%': { transform: 'rotate(360deg)' },
        },
      },
    },
  },
  plugins: [],
};
