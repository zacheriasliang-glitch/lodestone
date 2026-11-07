"""Explicit JSON commands for INGEST/EAT/MUTATE, Watcher, and Bezoar."""
import argparse
import json
import sys
import time
from pathlib import Path
import hashlib
from .bezoar import Plumber
from .bezoar_lessons import prepare_lesson, publish_lesson, read_lesson
from .organs.watcher import observe
from .organs._kernel import strict_load
from .support_review import read_review_file


def _capture_roots(items):
    result = {}
    for value in items:
        handle, separator, path = value.partition('=')
        if not separator or not handle or not path or handle in result:
            raise ValueError('Use one distinct --capture-root HANDLE=PATH per source binding')
        result[handle] = path
    return result


def main(argv=None, *, watcher_only=False):
    parser = argparse.ArgumentParser(prog='lodestone watcher' if watcher_only else 'lodestone bezoar')
    commands = parser.add_subparsers(dest='command', required=True)
    names = ['observe'] if watcher_only else ['ingest', 'eat-request', 'eat', 'mutate', 'watch', 'hold', 'show', 'export-profile',
                                             'lesson-request', 'lesson', 'lesson-show']
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
            p.add_argument('--capture-root', action='append', default=[], metavar='HANDLE=PATH')
        elif name == 'lesson-request':
            p.add_argument('--held', action='append', required=True)
            p.add_argument('--lesson', required=True)
            p.add_argument('--producer', required=True)
            p.add_argument('--checker', required=True)
            p.add_argument('--finding-key')
        elif name == 'lesson':
            p.add_argument('--request', required=True)
            p.add_argument('--review', required=True)
        else:
            p.add_argument('--handle', required=True)
            if name == 'export-profile':
                p.add_argument('--output', required=True)
    args = parser.parse_args(argv)
    try:
        plumber = Plumber(args.workspace)
        if args.command in ('watch', 'observe'):
            capture_roots = _capture_roots(args.capture_root)
            observations, attention = [], False
            for i in range(args.polls):
                if i:
                    time.sleep(args.interval)
                handle = observe(plumber.records, args.handle, capture_roots=capture_roots)
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
        elif args.command == 'lesson-request':
            handle = prepare_lesson(plumber.records, args.held, lesson=args.lesson, producer=args.producer,
                                            checker=args.checker, finding_key=args.finding_key)
        elif args.command == 'lesson':
            handle = publish_lesson(plumber.records, args.request, read_review_file(args.review, max_bytes=262144))
        elif args.command == 'lesson-show':
            payload = read_lesson(plumber.records, args.handle)
            print(json.dumps({'handle': args.handle, 'lesson': payload}, indent=2, ensure_ascii=False))
            return 0
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
        return 2 if record['payload'].get('status') in ('HELD', 'REVIEWED_FAIL', 'REVIEWED_UNKNOWN') else 0
    except (ValueError, OSError) as exc:
        print(json.dumps({'error': type(exc).__name__, 'message': str(exc)[:1500]}), file=sys.stderr)
        return 2
