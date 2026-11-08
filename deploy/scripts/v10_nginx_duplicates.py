"""Remove shadowed literal server names in Nginx's actual include order."""
from copy import deepcopy
import fnmatch
import ipaddress
from pathlib import Path, PurePosixPath
import re

import dev_config as D
import proxy_stats as W
import release as R


def http_servers(loaded, contents=None):
    sections = W.loaded_sections(D, loaded)
    if contents is not None:
        D.require(set(contents) == set(sections)
                  and all(sections[name].strip() == text.strip() for name, text in contents.items()),
                  'Captured Nginx file bytes differ from its configuration dump')
        sections = contents
    parsed = {name: W.parse(D, text) for name, text in sections.items()}
    master = '/etc/nginx/nginx.conf'
    D.require(master in parsed, 'Expected the loaded Nginx master configuration')
    visited = set()
    result = []

    def walk(filename, nodes, context, stack):
        for node in nodes:
            args = W.values(node)
            if args[0] == 'include':
                D.require(len(args) == 2 and not re.search(r'[$\s]', args[1])
                          and '**' not in args[1], 'Dynamic Nginx includes require review')
                pattern = str(PurePosixPath(master).parent / args[1])
                D.require('..' not in PurePosixPath(pattern).parts, 'Relative parent includes require review')
                parts = PurePosixPath(pattern).parts
                matches = sorted(name for name in parsed
                    if len(PurePosixPath(name).parts) == len(parts)
                    and all(fnmatch.fnmatchcase(a, b) for a, b in zip(PurePosixPath(name).parts, parts)))
                D.require(matches or any(c in pattern for c in '*?['), 'An included Nginx file was not captured')
                for name in matches:
                    D.require(name not in stack, 'Cyclic Nginx includes require review')
                    walk(name, parsed[name], context, (*stack, name))
            elif args == ['server'] and context == ('http',):
                identity = filename, node['start'], node['end']
                D.require(identity not in visited, 'A server configuration is included more than once; review its include directives')
                visited.add(identity)
                D.require(not W.directives(node, 'include'), 'Includes inside a server require a separate routing review')
                result.append((filename, node))
            elif node['children'] is not None:
                walk(filename, node['children'], (*context, args[0]), stack)

    walk(master, parsed[master], (), (master,))
    D.require(result, 'No HTTP server blocks were found in the effective configuration')
    return sections, result


def listener(directive):
    args = W.values(directive)[1:]
    value, options = args[0], args[1:]
    D.require('ipv6only=off' not in options and all(
        option in ('ssl', 'http2', 'default_server', 'default') for option in options),
        'Nonstandard listener options require a separate socket review')
    if value.isdigit():
        address, port = '0.0.0.0', value
    elif value.startswith('['):
        match = re.fullmatch(r'\[([0-9A-Fa-f:]+)\]:(\d+)', value)
        D.require(match is not None, 'Unexpected IPv6 listener')
        address, port = str(ipaddress.IPv6Address(match[1])), match[2]
    else:
        address, separator, port = value.rpartition(':')
        D.require(separator and port.isdigit(), 'Non-numeric Nginx listener requires review')
        address = '0.0.0.0' if address == '*' else str(ipaddress.IPv4Address(address))
    D.require(0 < int(port) <= 65535, 'Unexpected Nginx listen port')
    return (address, int(port)), tuple(sorted(set(options) - {'default', 'default_server'}))


def plan(loaded, contents=None):
    sections, servers = http_servers(loaded, contents)
    claims, socket_options, edits = {}, {}, {}
    removed_servers = removed_names = 0
    changes = []
    for ordinal, (filename, server) in enumerate(servers):
        listen = W.directives(server, 'listen')
        D.require(listen, 'Implicit Nginx listeners require review')
        sockets = dict(listener(item) for item in listen)
        D.require(len(sockets) == len(listen), 'Repeated listeners in a server require review')
        directives = W.directives(server, 'server_name')
        names = [name for item in directives for name in W.values(item)[1:]]
        D.require(names and all(re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]*', name)
                               for name in names), 'Implicit, regex or wildcard server names require review')
        unique = list(dict.fromkeys(name.lower() for name in names))
        shadowed = []
        for name in unique:
            matches = [(socket, name) in claims for socket in sockets]
            D.require(all(matches) or not any(matches),
                      'A name conflicts on only some listener addresses; a separate routing review is required')
            if all(matches):
                shadowed.append(name)
        for socket, options in sockets.items():
            if socket in socket_options:
                D.require(socket_options[socket] == options,
                          'Conflicting server listener protocols require review')
            else:
                socket_options[socket] = options
        if len(shadowed) == len(unique):
            D.require(not any(set(W.values(item)[2:]) & {'default', 'default_server'} for item in listen),
                      'A shadowed server is an explicit default; preserve it for separate review')
            edits.setdefault(filename, []).append((server['start'], server['end'], ''))
            removed_servers += 1
            removed_names += len(names)
            changes.append({'configuration': filename, 'server_ordinal': ordinal,
                            'removed_server': True, 'removed_name_count': len(names)})
            continue
        if names[0].lower() in shadowed:
            D.require(not any(re.search(r'\$(?:server_name\b|\{server_name\})|\bserver_name_in_redirect\s+on\b', text)
                              for text in sections.values()),
                      'Removing the primary alias could change server-name variables or redirects; review it separately')
        seen, count = set(shadowed), 0
        for item in directives:
            kept = []
            original = W.values(item)[1:]
            for name in original:
                if name.lower() in seen:
                    count += 1
                else:
                    kept.append(name)
                    seen.add(name.lower())
            if kept != original:
                text = 'server_name ' + ' '.join(kept) + ';' if kept else ''
                edits.setdefault(filename, []).append((item['start'], item['end'], text))
        if count:
            removed_names += count
            changes.append({'configuration': filename, 'server_ordinal': ordinal,
                            'removed_server': False, 'removed_name_count': count})
        for socket in sockets:
            for name in unique:
                if name not in shadowed:
                    claims[socket, name] = ordinal
    rewritten = deepcopy(sections)
    for filename, changes_in_file in edits.items():
        for start, end, replacement in sorted(changes_in_file, reverse=True):
            rewritten[filename] = rewritten[filename][:start] + replacement + rewritten[filename][end:]
    return {name: text for name, text in rewritten.items() if text != sections[name]}, {
        'format': 'deriw-nginx-duplicate-server-review-v1',
        'removed_server_blocks': removed_servers, 'removed_server_names': removed_names,
        'first_effective_server_preserved': True, 'changes': changes}


def freeze(nginx, directory):
    sections = W.loaded_sections(D, nginx['loaded'])
    contents = {item['target']: Path(item['source']).read_text()
                for item in nginx['files'] if item['target'] in sections}
    changes, review = plan(nginx['loaded'], contents)
    D.require(changes, 'No supported duplicate server names remain; no proxy replacement is needed')
    selected = deepcopy(nginx['files'])
    for number, item in enumerate(selected):
        if item['target'] not in changes:
            continue
        raw = changes[item['target']].encode()
        source = directory / ('nginx-repaired-' + str(number) + '.private')
        R.write_new(source, raw)
        item.update(source=str(source), sha256=R.digest(raw), runtime_overlay=True)
    return dict(nginx, forward_files=selected, duplicate_server_review=review)
