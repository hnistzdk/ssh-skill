import json
import sys


def add_reporting_arguments(parser):
    parser.add_argument('--json', action='store_true', help='Force machine-readable JSON output')
    parser.add_argument('--quiet', action='store_true', help='Suppress non-essential progress text')
    parser.add_argument('--verbose', action='store_true', help='Include additional reporting details')



def emit_json(payload, args=None, stream=None, ensure_ascii=True):
    if stream is None:
        stream = sys.stdout

    json_kwargs = {
        'ensure_ascii': ensure_ascii,
    }
    if args is None or (not getattr(args, 'json', False) and not getattr(args, 'quiet', False)):
        json_kwargs['indent'] = 2

    print(json.dumps(payload, **json_kwargs), file=stream)



def progress_enabled(args, default=True):
    return default and not getattr(args, 'quiet', False) and not getattr(args, 'no_progress', False)



def advisory_enabled(args):
    return not getattr(args, 'quiet', False)



def verbose_details(args, **details):
    if not getattr(args, 'verbose', False):
        return None
    return {key: value for key, value in details.items() if value is not None}
