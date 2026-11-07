"""Explicit plumber for kernel organs and held discrepancies."""
from .records import RecordStore
from .organs import ingest, eat, mutate, watcher
from .organs._kernel import require, digest


class Plumber:
    def __init__(self, workspace):
        self.records = RecordStore(workspace)

    def ingest(self, root, paths):
        return ingest.ingest(self.records, root, paths)

    def eat_request(self, ingestion, question):
        return eat.prepare(self.records, ingestion, question)

    def eat(self, request, proposal):
        return eat.eat(self.records, request, proposal)

    def mutate(self, parent, mutagen):
        return mutate.mutate(self.records, parent, mutagen)

    def watch(self, handles):
        return watcher.observe(self.records, handles)

    def hold(self, observation):
        record = self.records.relay('bezoar', observation, owner='watcher', kind='observation')
        report = record['payload']
        require(report.get('schema') == 'watcher-observation/v1'
                and type(report.get('issues')) is list and len(report['issues']) <= 1024
                and type(report.get('targets')) is list
                and report.get('repair_performed') is False and report.get('authority') == 'NONE',
                'Invalid Watcher observation')
        require(report.get('status') == ('ATTENTION' if report['issues'] else 'INTACT'),
                'Watcher observation status mismatch')
        findings = []
        for index, issue in enumerate(report['issues']):
            require(type(issue) is dict and issue.get('code') in (
                'COVERAGE_LIMIT', 'RECORD_UNAVAILABLE', 'CAPTURE_INVALID', 'SOURCE_UNAVAILABLE', 'SOURCE_DRIFT'),
                'Unknown Watcher finding')
            findings.append({'id': 'runtime-' + digest([observation, index]), 'state': 'HELD',
                             'finding': issue, 'clears_when': 'New evidence and explicit Auditor review; '
                             'a later clean observation never changes this record'})
        payload = {'schema': 'bezoar-routing/v1', 'observation': observation,
                   'held_findings': findings, 'targets': report['targets'],
                   'next_role': 'AUDITOR' if findings else 'ORCHESTRATOR',
                   'action': 'REVIEW_DISCREPANCIES' if findings else 'REVIEW_CANDIDATE',
                   'status': 'HELD' if findings else 'OBSERVED', 'authority': 'NONE',
                   'dispatch': 'PROPOSAL_ONLY', 'registry': 'runtime; separate from historical docs/bezoar.md'}
        return self.records.publish('bezoar', 'routing', payload, links=[observation])

    def reconcile_captured(self, raw, *, expected_sha256, scratch):
        """Retain recomputed captured-corpus discrepancies; no repair or authority."""
        from .bezoar_reconciliation import publish_reconciliation
        return publish_reconciliation(self.records, raw, expected_sha256=expected_sha256, scratch=scratch)

    def read_reconciliation(self, handle, *, scratch):
        """Reopen retained sources and recompute the exact typed reconciliation."""
        from .bezoar_reconciliation import read_reconciliation
        return read_reconciliation(self.records, handle, scratch=scratch)

    def propose_reconciliation_route(self, current, *, expected_source_sha256, previous=None, scratch):
        """Retain inert exact-held-node routing; never dispatch, repair or clear findings."""
        from .bezoar_routing import publish_route
        return publish_route(self.records, current, expected_source_sha256=expected_source_sha256,
                             previous=previous, scratch=scratch)

    def read_reconciliation_route(self, handle, *, scratch):
        """Recompute a stored routing proposal from its retained evidence."""
        from .bezoar_routing import read_route
        return read_route(self.records, handle, scratch=scratch)
