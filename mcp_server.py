"""Bound-state stdio MCP. No token/vault/clipboard imports or worker execution."""
import json
import sys
from agent_control import Channel, ControlError, COMMANDS


def definitions():
    empty = {'type': 'object', 'properties': {}, 'additionalProperties': False}
    tools = [
        {'name': 'get_status', 'description': 'Read live GUI phase, approved scope, known-inventory progress and credential-present boolean. Never returns a token. Disabled GUI reveals no inventory.', 'inputSchema': empty},
        {'name': 'list_entries', 'description': 'Paginated known queue/inventory, not proof of full cloud coverage. Requires live GUI opt-in.', 'inputSchema': {'type': 'object', 'properties': {'view': {'type': 'string', 'enum': ['all','pending','downloaded','failed','folders']}, 'offset': {'type': 'integer', 'minimum': 0}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 200}}, 'additionalProperties': False}},
        {'name': 'get_command', 'description': 'Read command acknowledgement. completed means GUI applied the command, not that all downloads succeeded; inspect get_status/list_entries.', 'inputSchema': {'type': 'object', 'properties': {'request_id': {'type': 'string'}}, 'required': ['request_id'], 'additionalProperties': False}},
    ]
    for name in sorted(COMMANDS):
        tools.append({'name': name, 'description': {
            'start': 'Queue download run for the exact GUI-approved profile. Does not change roots/account/destination. Already-active is not a second worker.',
            'scan': 'Queue metadata-only inventory for the GUI-approved profile; no payload writes.',
            'pause': 'Request cooperative pause; outstanding network request may finish first. Inspect phase for paused acknowledgement.',
            'resume': 'Resume paused existing worker; never silently creates a new run.',
            'stop': 'Request cooperative stop; does not delete partials, credentials or cloud data.',
            'retry': 'Reset unresolved retry cycle while preserving history. Does not start downloads.',
            'rescan': 'Reset inventory pagination while preserving history/files. Does not start network calls.',
            'export': 'Export private CSV reports to a NEW subdirectory under this state/agent-exports, never a caller-supplied path.',
        }[name] + ' Required request_id is a canonical UUID idempotency key. Queue ACK is not completion.', 'inputSchema': {'type': 'object', 'properties': {'request_id': {'type': 'string', 'format': 'uuid'}}, 'required': ['request_id'], 'additionalProperties': False}})
    return tools


def invoke(channel, name, args):
    if not isinstance(args, dict):
        raise ControlError('invalid_arguments')
    allowed = {'get_status': set(), 'list_entries': {'view','offset','limit'}, 'get_command': {'request_id'}}
    if name in COMMANDS:
        if set(args) != {'request_id'}:
            raise ControlError('invalid_arguments')
        return channel.submit(name, args['request_id'])
    if name not in allowed or set(args) - allowed[name]:
        raise ControlError('invalid_arguments')
    if name == 'get_status':
        return channel.status()
    if name == 'get_command':
        if not isinstance(args.get('request_id'), str) or len(args['request_id']) > 100:
            raise ControlError('invalid_request_id')
        return channel.command(args['request_id'])
    return channel.inventory(args.get('view', 'pending'), args.get('offset', 0), args.get('limit', 100))


def serve(store):
    channel = Channel(store)
    sys.stdout.reconfigure(encoding='utf-8')
    initialized = False
    while True:
        line = sys.stdin.buffer.readline(65538)
        if not line:
            break
        if len(line) > 65536:
            return  # No unbounded reads/output or accidental partial-frame replay.
        rid = None
        try:
            request = json.loads(line)
            if not isinstance(request, dict) or request.get('jsonrpc') != '2.0':
                raise ValueError()
            rid = request.get('id')
            if rid is None:
                continue
            if type(rid) not in (int, str) or isinstance(rid, str) and len(rid) > 100:
                raise ValueError()
            method = request.get('method')
            if method == 'initialize':
                version = request.get('params', {}).get('protocolVersion')
                version = version if version in ('2024-11-05', '2025-03-26', '2025-06-18') else '2024-11-05'
                initialized = True
                result = {'protocolVersion': version, 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'yandex-disk-rescue', 'version': '0.2.0'},
                          'instructions': 'Local GUI must be open with explicitly approved profile. Credentials stay in GUI. Mutation ACK is queued; poll get_command then status. No download worker lives in this MCP process. Reuse request_id for retry, never replay ambiguous outcomes.'}
            elif method == 'ping':
                result = {}
            elif not initialized:
                print(json.dumps({'jsonrpc': '2.0', 'id': rid, 'error': {'code': -32002, 'message': 'Initialize first'}}), flush=True)
                continue
            elif method == 'tools/list':
                result = {'tools': definitions()}
            elif method == 'tools/call':
                try:
                    params = request['params']
                    value = invoke(channel, params['name'], params.get('arguments', {}))
                    result = {'content': [{'type': 'text', 'text': json.dumps(value, ensure_ascii=False)}], 'isError': False}
                except ControlError as exc:
                    result = {'content': [{'type': 'text', 'text': str(exc)}], 'isError': True}
                except Exception:
                    result = {'content': [{'type': 'text', 'text': 'local_control_error'}], 'isError': True}
            else:
                print(json.dumps({'jsonrpc': '2.0', 'id': rid, 'error': {'code': -32601, 'message': 'Method not found'}}), flush=True)
                continue
            print(json.dumps({'jsonrpc': '2.0', 'id': rid, 'result': result}, ensure_ascii=False), flush=True)
        except Exception:
            print(json.dumps({'jsonrpc': '2.0', 'id': None, 'error': {'code': -32600, 'message': 'Invalid request'}}), flush=True)
