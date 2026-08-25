/**
 * Real browser wiring E2E for judge.awaiting_input → resume.
 *
 * Chromium → Vite proxy → FastAPI e2e server (DeterministicLLM only) →
 * production LangGraph → real Redis/SQLite → POST /api/chat/{session}/resume.
 *
 * Forbidden: page.route / route.fulfill / MSW / fake API handlers.
 * Observation only via page.waitForRequest / waitForResponse / page.on('response').
 */
import { test, expect, type Request, type Response } from '@playwright/test';

const CLARIFY_PROMPT = '把我的 DEM 按分级阈值进行栅格重分类';
const USER_ANSWER = '阈值 10、20 对应分类值 1、2、3';

function extractSubAgentRunIdFromSse(sseText: string): string {
  const awaitingFrames = sseText
    .split(/\r?\n\r?\n/)
    .filter((block) => block.includes('judge.awaiting_input'));
  for (const frame of awaitingFrames) {
    const dataLine = frame
      .split(/\r?\n/)
      .find((line) => line.startsWith('data:'));
    if (!dataLine) continue;
    try {
      const payload = JSON.parse(dataLine.slice(5).trim()) as {
        pending_task?: { sub_agent_run_id?: string };
      };
      const rid = payload.pending_task?.sub_agent_run_id;
      if (rid) return rid;
    } catch {
      // keep scanning
    }
  }
  return '';
}

test.describe('awaiting-input real wiring', () => {
  test('banner survives done; resume posts sub_agent_run_id + answer', async ({ page }, testInfo) => {
    // A unique real user isolates Redis session metadata and SQLite checkpoints
    // across reruns.  Reusing one fixed user can resurrect a completed run and
    // make the deterministic awaiting-input scenario silently skip its pause.
    const userId = `e2e_awaiting_${testInfo.workerIndex}_${Date.now()}`;
    await page.addInitScript((id: string) => {
      localStorage.clear();
      localStorage.setItem('gismind.user_id', id);
    }, userId);

    // Collect chat SSE bodies via passive page.on — more reliable than a single
    // waitForResponse when the stream completes before assertions run.
    let chatSseText = '';
    const onResponse = async (res: Response) => {
      const req = res.request();
      if (
        req.method() === 'POST' &&
        res.url().includes('/api/chat') &&
        !res.url().includes('/resume')
      ) {
        try {
          chatSseText = await res.text();
        } catch {
          // body may already be consumed by the page; ignore
        }
      }
    };
    page.on('response', onResponse);

    await page.goto('/');

    const textarea = page.locator('textarea').first();
    await expect(textarea).toBeVisible({ timeout: 30_000 });

    await textarea.fill(CLARIFY_PROMPT);
    await page.getByRole('button', { name: '发送' }).click();

    // Awaiting banner from judge.awaiting_input (independent of isStreaming).
    const banner = page.getByRole('status').filter({ hasText: '等待你的回答' });
    await expect(banner).toBeVisible({ timeout: 90_000 });
    await expect(banner).toContainText(/参数|values|输入/);

    // After stream ends, banner must still be present (done must not clear it).
    await expect(page.getByRole('button', { name: '回答并继续' })).toBeVisible({
      timeout: 30_000,
    });
    await expect(banner).toBeVisible();
    await expect(textarea).toHaveAttribute('placeholder', '请输入补充信息');
    await expect(
      page.locator('.trace-timeline').last(),
      'execution timeline must remain visible after the SSE run stops',
    ).toBeVisible();
    await expect(
      page.getByText('规划来源 · Root LLM'),
      'the real run.plan SSE payload must expose that this plan came from the root model',
    ).toBeVisible();

    // Prefer SSE-derived run id when the body was captured.
    const sseRunId = chatSseText ? extractSubAgentRunIdFromSse(chatSseText) : '';
    if (chatSseText) {
      expect(sseRunId, 'SSE judge.awaiting_input must carry sub_agent_run_id').toBeTruthy();
    }

    // Observe the real resume request (no interception).
    const resumeRequestPromise = page.waitForRequest(
      (req: Request) =>
        req.method() === 'POST' &&
        /\/api\/chat\/[^/]+\/resume(?:\?|$)/.test(req.url()),
      { timeout: 30_000 },
    );
    const resumeResponsePromise = page.waitForResponse(
      (res) =>
        res.request().method() === 'POST' &&
        /\/api\/chat\/[^/]+\/resume(?:\?|$)/.test(res.url()),
      { timeout: 90_000 },
    );

    await textarea.fill(USER_ANSWER);
    await page.getByRole('button', { name: '回答并继续' }).click();

    const resumeReq = await resumeRequestPromise;
    expect(resumeReq.method()).toBe('POST');
    const resumeUrl = resumeReq.url();
    expect(resumeUrl).toMatch(/\/api\/chat\/[^/]+\/resume/);

    const body = resumeReq.postDataJSON() as {
      sub_agent_run_id?: string;
      answer?: string;
    };
    expect(body.sub_agent_run_id, 'resume body must include sub_agent_run_id').toBeTruthy();
    expect(body.answer).toBe(USER_ANSWER);
    if (sseRunId) {
      expect(body.sub_agent_run_id).toBe(sseRunId);
    }

    const resumeRes = await resumeResponsePromise;
    expect(resumeRes.ok()).toBeTruthy();
    const resumeJson = (await resumeRes.json()) as {
      status: string;
      session_id: string;
      sub_agent_run_id?: string;
    };
    expect(resumeJson.status).toBe('resumed');
    expect(resumeJson.sub_agent_run_id).toBe(body.sub_agent_run_id);

    // UI: awaiting banner cleared after successful resume.
    await expect(banner).toHaveCount(0, { timeout: 30_000 });
    await expect(page.getByRole('button', { name: '发送' })).toBeVisible();

    // Formal API confirmation: pending is consumed (not_found).
    const sessionId = decodeURIComponent(
      resumeUrl.match(/\/api\/chat\/([^/]+)\/resume/)?.[1] ?? '',
    );
    expect(sessionId).toBeTruthy();
    const probe = await page.request.post(`/api/chat/${sessionId}/resume`, {
      data: {
        sub_agent_run_id: body.sub_agent_run_id,
        answer: 'probe',
      },
      headers: {
        'X-User-Id': userId,
        'Content-Type': 'application/json',
      },
    });
    expect(probe.ok()).toBeTruthy();
    const probeJson = (await probe.json()) as { status: string };
    expect(probeJson.status).toBe('not_found');

    page.off('response', onResponse);
  });
});
