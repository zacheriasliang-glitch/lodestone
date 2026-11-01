"""Explicit JSON commands for INGEST/EAT/MUTATE, Watcher, and Bezoar."""
import argparse
import json
import sys
import time
from pathlib import Path
import hashlib
from .bezoar import Plumber
from .organs._kernel import strict_load


def main(argv=None, *, watcher_only=False):
    parser = argparse.ArgumentParser(prog='lodestone watcher' if watcher_only else 'lodestone bezoar')
    commands = parser.add_subparsers(dest='command', required=True)
    names = ['observe'] if watcher_only else ['ingest', 'eat-request', 'eat', 'mutate', 'watch', 'hold', 'show', 'export-profile']
    for name in names:
        p = commands.add_parser(name)
        p.add_argument('--workspace', required=True)
        if name == 'ingest':
            p.add_argument('--root', required=True)
            p.add_argument('--source', action='append', required=True)
        elif name == 'eat-request':
            p.add_argument('--ingestion', required=True)
            p.add_argument('--question', required=True)
        elif name == 'eat':
            p.add_argument('--request', required=True)
            p.add_argument('--proposal', required=True)
        elif name == 'mutate':
            p.add_argument('--parent', required=True)
            p.add_argument('--mutagen', required=True)
        elif name in ('watch', 'observe'):
            p.add_argument('--handle', action='append', required=True)
            p.add_argument('--polls', type=int, choices=range(1, 11), default=1)
            p.add_argument('--interval', type=int, choices=range(1, 61), default=5)
        else:
            p.add_argument('--handle', required=True)
            if name == 'export-profile':
                p.add_argument('--output', required=True)
    args = parser.parse_args(argv)
    try:
        plumber = Plumber(args.workspace)
        if args.command in ('watch', 'observe'):
            observations, attention = [], False
            for i in range(args.polls):
                if i:
                    time.sleep(args.interval)
                handle = plumber.watch(args.handle)
                report = plumber.records.read(handle)['payload']
                observations.append({'handle': handle, 'report': report})
                attention |= report['status'] == 'ATTENTION'
            print(json.dumps({'observations': observations}, indent=2))
            return 2 if attention else 0
        if args.command == 'ingest':
            handle = plumber.ingest(args.root, args.source)
        elif args.command == 'eat-request':
            handle = plumber.eat_request(args.ingestion, args.question)
        elif args.command == 'eat':
            handle = plumber.eat(args.request, strict_load(args.proposal))
        elif args.command == 'mutate':
            handle = plumber.mutate(args.parent, strict_load(args.mutagen))
        elif args.command == 'hold':
            handle = plumber.hold(args.handle)
        else:
            handle = args.handle
        record = plumber.records.read(handle)
        if args.command == 'export-profile':
            if (record['owner'], record['kind']) not in (('eat', 'kernel_blueprint'), ('mutate', 'kernel_variant')):
                raise ValueError('Export requires EAT blueprint or MUTATE variant')
            plumber.records.verify_links([handle])
            profile = record['payload'].get('profile')
            if type(profile) is not dict or profile.get('schema') != 'kernel-profile/v1':
                raise ValueError('Invalid export profile')
            raw = (json.dumps(profile, indent=2, ensure_ascii=False, allow_nan=False) + '\n').encode('utf-8')
            with Path(args.output).open('xb') as f:
                f.write(raw)
            print(json.dumps({'source_handle': handle, 'path': str(Path(args.output).resolve()),
                              'sha256': hashlib.sha256(raw).hexdigest(), 'activation': 'NONE'}))
            return 0
        print(json.dumps({'handle': handle, 'record': record}, indent=2, ensure_ascii=False))
        return 2 if record['payload'].get('status') == 'HELD' else 0
    except (ValueError, OSError) as exc:
        print(json.dumps({'error': type(exc).__name__, 'message': str(exc)[:1500]}), file=sys.stderr)
        return 2
