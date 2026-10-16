"""Real DeepSeek Flash -> normal durable Runtime server for Chinese browser cases."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import threading
import uuid

from app.runtime.llm_planner import OpenAICompatiblePlanner, _digest
from tests.browser.runtime_browser_budget import DurableCostBudget
from tests.live.real_export_format_smoke import require_real_provider


def verify_source_manifest(manifest: dict[str, str]) -> None:
    if not isinstance(manifest, dict) or not manifest:
        raise ValueError('a nonempty absolute source_manifest is required')
    for name, expected in manifest.items():
        path = Path(name)
        if not path.is_absolute():
            raise ValueError('source_manifest paths must be absolute')
        try:
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            raise RuntimeError(f'source drift before provider request: {path}') from exc
        if actual != expected:
            raise RuntimeError(f'source drift before provider request: {path}')


class DurableRecordingPlanner(OpenAICompatiblePlanner):
    """Meter the existing provider boundary; no success substitute is installed."""

    def configure_evidence(self, configuration: dict) -> None:
        require_real_provider(api_key=self.api_key, base_url=self.base_url, model=self.model)
        # One transient transport/status retry shares the existing two-HTTP
        # planning budget. Multi-HTTP usage keeps the full prepaid bound.
        self.max_retries = 1
        self.max_tokens = 4096
        self.max_http_calls = 2
        self.source_manifest = configuration['source_manifest']
        verify_source_manifest(self.source_manifest)
        self.cohort = str(Path(configuration['output']).resolve())
        self.evidence_path = Path(self.cohort) / 'provider.json'
        self.evidence_lock = threading.Lock()
        self.budget = DurableCostBudget(
            configuration['budget_db'], max_usd=configuration['max_cost_usd'],
            input_usd_per_million=configuration['input_usd_per_million'],
            output_usd_per_million=configuration['output_usd_per_million'],
            cache_hit_usd_per_million=configuration['cache_hit_usd_per_million'],
            max_planner_calls=configuration['max_planner_calls'],
        )

    def _write_evidence(self) -> None:
        with self.evidence_lock:
            evidence = {
                **self.budget.summary(), 'budget_db': str(self.budget.path),
                'provider': self.provider, 'model': self.model,
                'responses': self.budget.responses(self.cohort),
            }
            pending = self.evidence_path.with_name(f'provider.{uuid.uuid4().hex}.pending')
            try:
                with pending.open('w', encoding='utf-8') as handle:
                    json.dump(evidence, handle, ensure_ascii=False, indent=2, allow_nan=False)
                    handle.flush()
                    os.fsync(handle.fileno())
                pending.replace(self.evidence_path)
            finally:
                if pending.exists():
                    pending.unlink()

    def _call(self, messages):
        require_real_provider(api_key=self.api_key, base_url=self.base_url, model=self.model)
        if self.max_retries != 1 or self.max_tokens != 4096 or self.max_http_calls != 2:
            raise RuntimeError('live provider limits changed before provider request')
        verify_source_manifest(self.source_manifest)
        reservation = self.budget.reserve(
            messages, max_tokens=self.max_tokens, response_format=self._response_format(messages),
            cohort=self.cohort,
        )
        try:
            raw, latency_ms = super()._call(messages)
        except BaseException as exc:
            self.budget.retain(reservation['reservation_id'], error_code=getattr(exc, 'code', 'PROVIDER_CALL_FAILED'))
            self._write_evidence()
            raise
        usage = raw.get('usage') if isinstance(raw.get('usage'), dict) else {}
        self.budget.settle(reservation['reservation_id'], usage=usage,
                           response_hash=_digest(raw), latency_ms=latency_ms)
        self._write_evidence()
        return raw, latency_ms


def create_app():
    if os.environ.get('GISMIND_LIVE_LLM') != '1':
        raise RuntimeError('explicit GISMIND_LIVE_LLM=1 opt-in required')
    from app.main import create_app as create_runtime_app
    from app.runtime import ArtifactStore, RuntimeDB, RuntimeStore, RuntimeSupervisor

    configuration = json.loads(Path(os.environ['GISMIND_BROWSER_CONFIG']).read_text(encoding='utf-8'))
    root = Path(configuration['output'])
    if not root.is_absolute():
        raise ValueError('cohort output path must be absolute')
    root.mkdir(parents=True, exist_ok=True)
    planner = DurableRecordingPlanner.from_settings()
    planner.configure_evidence(configuration)
    runtime = RuntimeStore(RuntimeDB(root / 'runtime.db'))
    runtime.initialize()
    artifacts = ArtifactStore(root / 'cas', runtime)
    supervisor = RuntimeSupervisor(runtime, artifacts, planner=planner, isolate=True,
                                   timeout_seconds=configuration['timeout_seconds'])
    return create_runtime_app(runtime_store=runtime, artifact_store=artifacts,
                              runtime_supervisor=supervisor)


create_live_app = create_app
