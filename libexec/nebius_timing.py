"""Durable per-operation stage timings and shared terminal formatting."""
import datetime as dt

LABELS = {'checking': 'Check request', 'preflight': 'Check configuration', 'disk': 'Boot disk',
          'instance': 'Create VM', 'boot': 'VM running and address', 'ssh': 'SSH login',
          'start': 'Start VM', 'stop': 'Stop VM', 'delete': 'Delete resources',
          'project': 'Create project', 'network': 'Network ready'}


def seconds(start, end):
    try:
        return max(0.0, (dt.datetime.fromisoformat(end) - dt.datetime.fromisoformat(start)).total_seconds())
    except (TypeError, ValueError):
        return 0.0


def advance(previous, phase, stage, now):
    timeline = [dict(row) for row in previous.get('timeline', [])]
    started = previous.get('started_at') or now
    if timeline and not timeline[-1].get('finished_at'):
        if timeline[-1]['stage'] != stage or phase != 'running':
            timeline[-1].update(finished_at=now, elapsed_seconds=seconds(timeline[-1]['started_at'], now),
                                outcome='error' if phase == 'error' and timeline[-1]['stage'] == stage else 'ready')
    if phase == 'running' and (not timeline or timeline[-1]['stage'] != stage or timeline[-1].get('finished_at')):
        timeline.append({'stage': stage, 'started_at': now if timeline else started})
    result = {'started_at': started, 'timeline': timeline, 'elapsed_seconds': seconds(started, now)}
    if phase != 'running':
        result['finished_at'] = now
    return result


def duration(value):
    value = max(0, float(value))
    return f'{value:.1f}s' if value < 60 else f'{int(value // 60)}m {value % 60:04.1f}s'


def elapsed(operation):
    if operation.get('phase') == 'running' and operation.get('started_at'):
        return seconds(operation['started_at'], dt.datetime.now(dt.timezone.utc).isoformat())
    return operation.get('elapsed_seconds', 0)


def headline(operation):
    if not operation.get('timeline'):
        return 'Stage timings were not recorded for this operation.'
    label = 'Total to SSH ready' if operation.get('ssh_ready') else 'Total elapsed'
    return f'{label}: {duration(elapsed(operation))}'


def lines(operation):
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    rows = []
    for row in operation.get('timeline', []):
        value = row.get('elapsed_seconds', seconds(row['started_at'], now))
        suffix = ' · failed' if row.get('outcome') == 'error' else ' · waiting' if not row.get('finished_at') else ''
        rows.append(f"{LABELS.get(row['stage'], row['stage'].replace('_', ' ').capitalize())}: {duration(value)}{suffix}")
    return rows


def text(operation):
    return '\n'.join([headline(operation), '', *lines(operation)])
