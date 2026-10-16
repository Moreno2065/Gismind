"""Actual Uvicorn app factory: production lifespan/worker, metered real provider only."""
from __future__ import annotations

import json
import os
from pathlib import Path

from tests.live.real_export_format_smoke import CostBudget, MeteredRealPlanner, require_real_provider


class RecordingPlanner(MeteredRealPlanner):
    evidence_path: Path

    def _call(self, messages):
        try:
            return super()._call(messages)
        finally:
            evidence = {'responses': self.observed_responses, 'reserved_usd': self.budget.reserved_usd,
                        'planner_calls_reserved': self.budget.planner_calls, 'max_cost_usd': self.budget.max_usd,
                        'provider': self.provider, 'model': self.model}
            pending = self.evidence_path.with_suffix('.pending')
            pending.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
            pending.replace(self.evidence_path)


def create_live_app():
    from app.main import create_app
    from app.runtime import ArtifactStore, RuntimeDB, RuntimeStore, RuntimeSupervisor

    assert os.environ.get('GISMIND_LIVE_LLM') == '1', 'explicit live opt-in required'
    configuration = json.loads(Path(os.environ['GISMIND_BROWSER_CONFIG']).read_text(encoding='utf-8'))
    root = Path(configuration['output'])
    planner = RecordingPlanner.from_settings()
    require_real_provider(api_key=planner.api_key, base_url=planner.base_url, model=planner.model)
    planner.max_retries = 0
    planner.max_tokens = 4096
    planner.observed_responses = []
    planner.evidence_path = root / 'provider.json'
    planner.budget = CostBudget(max_usd=configuration['max_cost_usd'], input_usd_per_million=configuration['input_usd_per_million'],
                               output_usd_per_million=configuration['output_usd_per_million'], max_planner_calls=configuration['max_planner_calls'])
    runtime = RuntimeStore(RuntimeDB(root / 'runtime.db'))
    runtime.initialize()
    artifacts = ArtifactStore(root / 'cas', runtime)
    supervisor = RuntimeSupervisor(runtime, artifacts, planner=planner, isolate=True,
                                   timeout_seconds=configuration['timeout_seconds'])
    # Production lifespan starts/stops the normal durable supervisor thread.
    return create_app(runtime_store=runtime, artifact_store=artifacts, runtime_supervisor=supervisor)
